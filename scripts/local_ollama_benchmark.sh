#!/bin/zsh
# Edited by Antigravity on 2026-06-16T13:49:18-06:00 (Optimized script using Ollama HTTP API)

set -u

OUT_DIR="./ollama-benchmarks"
RUN_ID=$(date '+%Y%m%d-%H%M%S')

CSV="${OUT_DIR}/benchmark-${RUN_ID}.csv"
LOG_DIR="${OUT_DIR}/logs-${RUN_ID}"

mkdir -p "$OUT_DIR"
mkdir -p "$LOG_DIR"

MODELS=(
    "qwen2.5-coder:3b"
    "qwen3:4b"
    "qwen3.5:9b"
)

PROMPTS=(
    "latency|Reply only with the word OK."
    "small|Explain the advantages and disadvantages of MCP servers versus direct API integration."
    "medium|Review this architecture: Claude Code calls a local context-engine API which scans repositories, builds markdown context bundles, stores embeddings in Supabase pgvector, and uses Ollama for local summarization. Identify bottlenecks, scaling limits, failure modes and performance risks."
    "large|Design a production-ready local AI development architecture using Claude Code, Ollama, FastAPI, Supabase pgvector, repository indexing and local embeddings. Include model routing, caching, retries, fallback logic and timeout recommendations."
    "reasoning|A product organization repeatedly experiences the same coordination failures across engineering, product and operations. Analyze governance failures, ownership gaps, incentive misalignment and organizational learning failures. Recommend structural fixes."
)

RUNS_PER_PROMPT=5
TIMEOUT_SECONDS=900
SAVE_RESPONSES="${SAVE_RESPONSES:-0}"

sanitize_jsonl() {
    # Ollama can occasionally emit raw control bytes in generated text. Those
    # bytes are illegal inside JSON strings and make jq reject the whole run.
    LC_ALL=C tr -d '\000-\011\013\014\016-\037'
}

parse_jsonl() {
    local source_file="$1"
    local sanitized_file="$2"
    local jq_filter="$3"

    if jq -r "$jq_filter" "$source_file" 2>/dev/null; then
        return 0
    fi

    sanitize_jsonl < "$source_file" > "$sanitized_file"
    jq -r "$jq_filter" "$sanitized_file"
}

parse_jsonl_join() {
    local source_file="$1"
    local sanitized_file="$2"
    local jq_filter="$3"

    if jq -rj "$jq_filter" "$source_file" 2>/dev/null; then
        return 0
    fi

    sanitize_jsonl < "$source_file" > "$sanitized_file"
    jq -rj "$jq_filter" "$sanitized_filef"
}

# Check if Ollama is running
if ! curl -s -f http://localhost:11434/api/tags > /dev/null; then
    echo "ERROR: Ollama is not running on http://localhost:11434. Please start Ollama."
    exit 1
fi

echo "run_id,timestamp,model,prompt_type,run,status,wall_sec,user_sec,sys_sec,output_chars,output_words,tokens_est,tokens_per_sec,log_file" > "$CSV"

for model in "${MODELS[@]}"; do
    # Verify if model is available in Ollama
    if ! curl -s http://localhost:11434/api/tags | jq --arg m "$model" -e '.models[].name | select(. == $m or . == ($m + ":latest"))' > /dev/null; then
        echo "WARNING: Model '$model' is not loaded in Ollama. Attempting to pull..."
        curl -X POST http://localhost:11434/api/pull -d "{\"name\": \"$model\"}"
        if [ $? -ne 0 ]; then
            echo "ERROR: Failed to pull model '$model'. Skipping."
            continue
        fi
    fi

    echo "=== Benchmarking model: $model ==="
    echo "Warming up $model..."
    # Disable thinking for qwen3 models to avoid <think> tokens skewing benchmarks
    if [[ "$model" == qwen3* ]]; then
        warmup_payload=$(jq -n --arg m "$model" '{model: $m, prompt: "Reply only with OK.", stream: false, think: false}')
    else
        warmup_payload=$(jq -n --arg m "$model" '{model: $m, prompt: "Reply only with OK.", stream: false}')
    fi
    curl -s -X POST http://localhost:11434/api/generate \
        -H "Content-Type: application/json" \
        -d "$warmup_payload" > /dev/null

    for prompt_entry in "${PROMPTS[@]}"; do
        prompt_type="${prompt_entry%%|*}"
        prompt="${prompt_entry#*|}"
        echo "  Prompt type: $prompt_type : $prompt "

        for ((run = 1; run <= RUNS_PER_PROMPT; run++)); do
            timestamp=$(date '+%Y-%m-%d %H:%M:%S')
            safe_model="${model//[:\/]/_}"
            log_file="${LOG_DIR}/${safe_model}_${prompt_type}_run${run}.txt"
            response_file="${LOG_DIR}/${safe_model}_${prompt_type}_run${run}.jsonl"
            sanitized_file="${LOG_DIR}/${safe_model}_${prompt_type}_run${run}.sanitized.jsonl"
            parse_error_file="${LOG_DIR}/${safe_model}_${prompt_type}_run${run}.parse_error.txt"
            # Run query via HTTP API
            # Disable thinking for qwen3 models to avoid <think> tokens skewing benchmarks
            if [[ "$model" == qwen3* ]]; then
                generate_payload=$(jq -n --arg m "$model" --arg p "$prompt" '{model: $m, prompt: $p, stream: true, think: false}')
            else
                generate_payload=$(jq -n --arg m "$model" --arg p "$prompt" '{model: $m, prompt: $p, stream: true}')
            fi
            http_code=$(curl -s -o "$response_file" -w "%{http_code}" --max-time "$TIMEOUT_SECONDS" -X POST http://localhost:11434/api/generate \
                -H "Content-Type: application/json" \
                -d "$generate_payload")
            curl_exit=$?

            if [[ "$curl_exit" -eq 0 && "$http_code" -eq 200 ]]; then
                metrics_filter='select(.done == true) | [.total_duration, .load_duration, .prompt_eval_count, .prompt_eval_duration, .eval_count, .eval_duration] | @tsv'
                metrics=$(parse_jsonl "$response_file" "$sanitized_file" "$metrics_filter" 2>"$parse_error_file")
                parse_exit=$?

                if [[ "$parse_exit" -eq 0 && -n "$metrics" ]]; then
                    run_status="ok"
                    read -r wall_ns load_ns prompt_tokens prompt_ns eval_tokens eval_ns <<< "$metrics"

                    # Default nulls/empty values to 0
                    [[ "$wall_ns" == "null" || -z "$wall_ns" ]] && wall_ns=0
                    [[ "$load_ns" == "null" || -z "$load_ns" ]] && load_ns=0
                    [[ "$prompt_tokens" == "null" || -z "$prompt_tokens" ]] && prompt_tokens=0
                    [[ "$prompt_ns" == "null" || -z "$prompt_ns" ]] && prompt_ns=0
                    [[ "$eval_tokens" == "null" || -z "$eval_tokens" ]] && eval_tokens=0
                    [[ "$eval_ns" == "null" || -z "$eval_ns" ]] && eval_ns=0

                    wall_sec=$(( wall_ns / 1000000000.0 ))
                    eval_sec=$(( eval_ns / 1000000000.0 ))

                    tokens_est="$eval_tokens"
                    output_chars=0
                    output_words=0

                    if [[ "$SAVE_RESPONSES" == "1" ]]; then
                        response_filter='select(.response != null) | .response'
                        parse_jsonl_join "$response_file" "$sanitized_file" "$response_filter" 2>>"$parse_error_file" > "$log_file"
                        output_chars=$(wc -c < "$log_file" | tr -d ' ')
                        output_words=$(wc -w < "$log_file" | tr -d ' ')
                    else
                        echo "Response text not saved. Set SAVE_RESPONSES=1 to capture it." > "$log_file"
                        echo "Raw JSONL response: $response_file" >> "$log_file"
                    fi

                    if (( eval_sec > 0 )); then
                        tokens_per_sec=$(printf "%.2f" $(( eval_tokens / eval_sec )))
                    else
                        tokens_per_sec="0.00"
                    fi
                else
                    run_status="parse_error"
                    wall_sec=0
                    tokens_per_sec="0.00"
                    output_chars=0
                    output_words=0
                    tokens_est=0
                    {
                        echo "ERROR: Failed to parse Ollama JSONL response."
                        echo "Raw response: $response_file"
                        echo "Sanitized response: $sanitized_file"
                        echo "Parse errors: $parse_error_file"
                    } > "$log_file"
                fi
            else
                if [[ "$curl_exit" -eq 28 ]]; then
                    run_status="timeout"
                else
                    run_status="error_${curl_exit}_http_${http_code}"
                fi
                wall_sec="$TIMEOUT_SECONDS"
                tokens_per_sec="0.00"
                output_chars=0
                output_words=0
                tokens_est=0
                echo "ERROR: Curl Exit=$curl_exit, HTTP Code=$http_code" > "$log_file"
                echo "Raw response: $response_file" >> "$log_file"
            fi

            # Map user_sec / sys_sec to 0 for compatibility
            user_sec=0.0
            sys_sec=0.0

            echo "$RUN_ID,$timestamp,$model,$prompt_type,$run,$run_status,$wall_sec,$user_sec,$sys_sec,$output_chars,$output_words,$tokens_est,$tokens_per_sec,$log_file" >> "$CSV"
            echo "    Run $run: $run_status | Wall: ${wall_sec}s | TPS: $tokens_per_sec"

            sleep 2
        done
    done
done

echo ""
echo "Benchmark complete. Summary results:"
awk -F',' '
NR > 1 {
  model=$3
  status=$6
  if (status == "ok") {
    total_sec[model]+=$7
    total_tps[model]+=$13
    count[model]++
  }
}
END {
  printf "\n%-25s %-10s %-10s\n", "MODEL", "AVG_SEC", "AVG_TPS"
  printf "%-25s %-10s %-10s\n", "-----", "-------", "-------"
  for (m in count) {
    if (count[m] > 0) {
      printf "%-25s %-10.2f %-10.2f\n", m, total_sec[m]/count[m], total_tps[m]/count[m]
    } else {
      printf "%-25s %-10s %-10s\n", m, "N/A", "N/A"
    }
  }
}
' "$CSV"
