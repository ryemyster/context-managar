# Managing Runpod Ollama Infrastructure through MCP

This guide describes how to manage remote model infrastructure from Codex or
Claude without repeatedly using the Runpod console.

The recommended architecture keeps Context Engine, its MCP server, repository
access, embeddings, and Supabase integration local. Runpod supplies remote GPU
generation only.

```text
Codex / Claude
   |-- Context Engine MCP (local :8089)
   |      `-- Context Engine REST (local :8088)
   |             |-- Runpod model endpoint (generation)
   |             |-- local Ollama (nomic-embed-text)
   |             `-- Supabase vectors
   |
   `-- Runpod API MCP (local stdio)
          `-- Runpod control plane
                 |-- create/start/stop Pods
                 |-- create/manage templates
                 `-- create/manage Serverless endpoints
```

These are separate MCP servers:

- **Context Engine MCP** performs repository investigation.
- **Runpod API MCP** manages Runpod infrastructure.

The Runpod MCP server does not proxy inference requests and does not replace
Context Engine MCP.

## Install the Runpod MCP server

Runpod publishes the `@runpod/mcp-server` npm package.

Codex:

```bash
codex mcp add runpod \
  --env RUNPOD_API_KEY=<RUNPOD_MANAGEMENT_API_KEY> \
  -- npx -y @runpod/mcp-server@latest
```

Claude Code:

```bash
claude mcp add runpod --scope user \
  -e RUNPOD_API_KEY=<RUNPOD_MANAGEMENT_API_KEY> \
  -- npx -y @runpod/mcp-server@latest
```

Verify the connection through the client's MCP status command, then try:

```text
List my Runpod Pod templates.
List all running Pods and their exposed ports.
Start the Pod created from my private Context Engine Ollama template.
Stop that Pod after confirming no inference jobs are active.
```

Official reference:
[Runpod MCP servers](https://docs.runpod.io/get-started/mcp-servers).

## What to automate once

The Runpod MCP manages infrastructure resources. Ollama runtime configuration
should be captured in a **private reusable Pod template or custom image** so
each Pod starts consistently.

The template/image needs to define:

- Ollama installation.
- `OLLAMA_HOST=0.0.0.0`.
- HTTP port `11434`.
- A startup command that runs `ollama serve`.
- Required generation models.
- Persistent storage for the Ollama model cache.
- An authentication proxy in front of Ollama.
- Health checking for `/api/tags`.

Runpod documents the basic unauthenticated setup here:
[Set up Ollama on a Pod](https://docs.runpod.io/tutorials/pods/run-ollama).

Runpod templates can capture the image, ports, environment variables, startup
command, storage, and credentials:
[Manage Pod templates](https://docs.runpod.io/pods/templates/manage-templates).

Once this template exists, the MCP workflow becomes:

1. Ask Runpod MCP to create a Pod from the private template.
2. Ask it for the Pod ID and status.
3. Wait until Ollama's health endpoint responds.
4. Configure Context Engine with the authenticated inference URL.
5. Reinstall/restart Context Engine.
6. Stop or delete the Pod through Runpod MCP when no longer needed.

Runpod's proxied Ollama URL has this form:

```text
https://<POD_ID>-11434.proxy.runpod.net
```

## Security model

There are three independent credentials.

### 1. Runpod management credential

`RUNPOD_API_KEY` is used by the Runpod MCP server. It can manage infrastructure
and must not be reused as an inference credential.

Create a dedicated restricted key and grant only the Pod, template, volume, or
endpoint permissions required by the MCP workflow. Runpod recommends restricted
keys with minimum permissions:
[Manage Runpod API keys](https://docs.runpod.io/get-started/api-keys).

Treat this as a high-impact credential: an MCP-capable agent may be able to
create billable resources or delete infrastructure within the key's scope.
Require user confirmation for create, resize, stop, and delete actions.

### 2. Inference credential

An Ollama Pod exposed through Runpod's HTTP proxy is publicly reachable.
Runpod explicitly requires applications on exposed HTTP or TCP ports to
implement authentication:
[Expose ports securely](https://docs.runpod.io/pods/configuration/expose-ports).

Ollama does not provide the required public-edge authentication by itself.
Place an authenticating reverse proxy in front of it or use a deployment method
that provides authenticated endpoint access.

Context Engine can send a bearer token:

```env
INFERENCE_GENERATION_PROVIDER=ollama
INFERENCE_GENERATION_ENDPOINT=https://<POD_ID>-11434.proxy.runpod.net
INFERENCE_GENERATION_API_KEY=<DEDICATED_INFERENCE_TOKEN>
```

This is secure only if the remote proxy validates that bearer token. Merely
setting the local variable does not secure a raw Ollama port.

For less custom security work, prefer a Runpod Serverless vLLM endpoint. Runpod
authenticates those requests with its API key and provides an OpenAI-compatible
URL:

```env
INFERENCE_GENERATION_PROVIDER=openai_compatible
INFERENCE_GENERATION_ENDPOINT=https://api.runpod.ai/v2/<ENDPOINT_ID>/openai/v1
INFERENCE_GENERATION_API_KEY=<RESTRICTED_ENDPOINT_KEY>
```

### 3. Context Engine credential

`CONTEXT_ENGINE_API_KEY` protects Context Engine REST endpoints with
`X-API-Key`. It is independent of Runpod authentication.

Current local state:

```text
Context Engine API authentication: disabled
Context Engine MCP bind address: 127.0.0.1
Context Engine REST bind address: 0.0.0.0
```

Because REST currently binds to all interfaces, either:

1. enable `CONTEXT_ENGINE_API_KEY`, or
2. change the native service to bind REST to `127.0.0.1`.

`/health`, `/healthcheck`, and `/setup` intentionally bypass Context Engine API
authentication.

There is currently an installation gap: `mcp_server.py` supports forwarding
`CONTEXT_ENGINE_API_KEY`, but `scripts/install-mcp.sh` does not write that value
into the MCP launchd plist. Fix that installer before enabling Context Engine
authentication for the persistent MCP service.

## Local Context Engine configuration

Remote generation with existing local embeddings:

```env
# Remote generation
INFERENCE_GENERATION_PROVIDER=ollama
INFERENCE_GENERATION_ENDPOINT=https://<AUTHENTICATED_OLLAMA_ENDPOINT>
INFERENCE_GENERATION_API_KEY=<DEDICATED_INFERENCE_TOKEN>

INFERENCE_FAST_MODEL=<REMOTE_MODEL>
INFERENCE_REASONING_MODEL=<REMOTE_MODEL>
INFERENCE_AGENT_MODEL=<REMOTE_MODEL>
INFERENCE_SELECTION_MODEL=<REMOTE_MODEL>
INFERENCE_VERIFICATION_MODEL=<REMOTE_MODEL>

# Preserve the existing vector corpus
INFERENCE_EMBEDDING_PROVIDER=ollama
INFERENCE_EMBEDDING_ENDPOINT=http://localhost:11434
INFERENCE_EMBEDDING_MODEL=nomic-embed-text
```

Apply changes:

```bash
bash scripts/install-native.sh
```

## Model selection by Context Engine role

Context Engine has five generation roles:

| Environment variable | Workload | Selection priority |
|---|---|---|
| `INFERENCE_FAST_MODEL` | `/scan`, `/find`, `/summarize`, `/context`, `/draft`, `/scaffold` | Low latency, code comprehension, reliable JSON |
| `INFERENCE_REASONING_MODEL` | `/diff-summary` | Risk analysis, instruction following, structured output |
| `INFERENCE_AGENT_MODEL` | Primary `/agents/run` selection and answer synthesis | Tool use, long-horizon repository reasoning, recovery |
| `INFERENCE_SELECTION_MODEL` | Native tool-call fallback | Fast and reliable function calling |
| `INFERENCE_VERIFICATION_MODEL` | Evidence-based answer verification | Schema adherence and conservative judgment |

### Current routing constraint

The service has role-specific **model names**, but only one
`INFERENCE_GENERATION_ENDPOINT`. A normal Runpod public or dedicated vLLM
endpoint serves one model. Therefore, all five role variables must currently
name a model served by the same endpoint.

Do not configure Qwen for one role and GPT-OSS for another when their Runpod
URLs differ. That requires a future service change providing role-specific
provider endpoints.

### Recommended configuration now: public GPT-OSS 120B

Use this when correctness, tool calling, and minimal deployment work matter
more than minimizing model cost:

```env
# Runpod Public Endpoint
INFERENCE_GENERATION_PROVIDER=openai_compatible
INFERENCE_GENERATION_ENDPOINT=https://api.runpod.ai/v2/gpt-oss-120b/openai/v1
INFERENCE_GENERATION_API_KEY=<RESTRICTED_RUNPOD_ENDPOINT_KEY>

# One endpoint serves one model, so every role uses the same identifier.
INFERENCE_FAST_MODEL=openai/gpt-oss-120b
INFERENCE_REASONING_MODEL=openai/gpt-oss-120b
INFERENCE_AGENT_MODEL=openai/gpt-oss-120b
INFERENCE_SELECTION_MODEL=openai/gpt-oss-120b
INFERENCE_VERIFICATION_MODEL=openai/gpt-oss-120b

# Preserve existing 768-dimensional vectors.
INFERENCE_EMBEDDING_PROVIDER=ollama
INFERENCE_EMBEDDING_ENDPOINT=http://localhost:11434
INFERENCE_EMBEDDING_MODEL=nomic-embed-text
```

Why:

- Runpod documents a 131,072-token context window.
- Runpod explicitly documents tool-calling support.
- The upstream model is designed for reasoning and agentic tasks.
- It supports function calling and structured outputs, which are more important
  to `/agents/run` than raw parameter count.

References:

- [Runpod public coding endpoints](https://docs.runpod.io/public-endpoints/ai-coding-tools)
- [Runpod AI SDK model capabilities](https://docs.runpod.io/public-endpoints/ai-sdk)
- [GPT-OSS 120B model card](https://huggingface.co/openai/gpt-oss-120b)

Tradeoffs:

- A 120B public model is excessive for simple `/find` synthesis.
- Every role pays the latency and price of the larger model.
- This is the most reliable configuration supported by the current
  single-generation-endpoint architecture, not the final cost-optimal design.

### Recommended dedicated endpoint: Qwen3-Coder 30B

Use this when you want a private, code-specialized endpoint with lower resource
requirements:

```env
INFERENCE_GENERATION_PROVIDER=openai_compatible
INFERENCE_GENERATION_ENDPOINT=https://api.runpod.ai/v2/<ENDPOINT_ID>/openai/v1
INFERENCE_GENERATION_API_KEY=<RESTRICTED_RUNPOD_ENDPOINT_KEY>

INFERENCE_FAST_MODEL=Qwen/Qwen3-Coder-30B-A3B-Instruct
INFERENCE_REASONING_MODEL=Qwen/Qwen3-Coder-30B-A3B-Instruct
INFERENCE_AGENT_MODEL=Qwen/Qwen3-Coder-30B-A3B-Instruct
INFERENCE_SELECTION_MODEL=Qwen/Qwen3-Coder-30B-A3B-Instruct
INFERENCE_VERIFICATION_MODEL=Qwen/Qwen3-Coder-30B-A3B-Instruct

INFERENCE_EMBEDDING_PROVIDER=ollama
INFERENCE_EMBEDDING_ENDPOINT=http://localhost:11434
INFERENCE_EMBEDDING_MODEL=nomic-embed-text
```

Why:

- The model is specifically trained for agentic coding and tool calling.
- Its 256K native context targets repository-scale understanding.
- It has 30.5B total parameters but activates roughly 3.3B per token, making it
  materially more deployable than a dense 30B model.
- It is non-thinking, which is useful for predictable code and tool output but
  makes it less specialized for deep reasoning than GPT-OSS.

Reference:
[Qwen3-Coder 30B model card](https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct).

For a current vLLM release supporting Qwen3-Coder's native tool format, use:

```env
MODEL_NAME=Qwen/Qwen3-Coder-30B-A3B-Instruct
MAX_MODEL_LEN=32768
ENABLE_AUTO_TOOL_CHOICE=true
TOOL_CALL_PARSER=qwen3_xml
RAW_OPENAI_OUTPUT=1
```

Start at 32K context even though the model supports more. KV-cache memory grows
with context length; increase it only after measuring GPU headroom. Confirm that
the selected Runpod vLLM worker version supports `qwen3_xml` before deployment.

References:

- [vLLM Qwen3-Coder tool parser](https://docs.vllm.ai/en/stable/features/tool_calling/)
- [Runpod vLLM configuration](https://docs.runpod.io/serverless/vllm/configuration)

### Public Qwen3 32B alternative

Runpod also exposes:

```env
INFERENCE_GENERATION_ENDPOINT=https://api.runpod.ai/v2/qwen3-32b-awq/openai/v1
INFERENCE_FAST_MODEL=Qwen/Qwen3-32B-AWQ
```

It is a reasonable general code and reasoning model, but Runpod's capability
documentation explicitly calls out tool calling for GPT-OSS and does not make
the same guarantee for the public Qwen endpoint. Do not use public Qwen3 32B
for `/agents/run` until native tool calls and JSON Schema responses pass the
smoke tests below.

Reference:
[Runpod Qwen3 32B endpoint](https://docs.runpod.io/public-endpoints/models/qwen3-32b).

### Ideal role mapping after multi-endpoint routing

This is the target configuration after Context Engine supports a provider and
endpoint per role:

| Role | Recommended model | Reason |
|---|---|---|
| Fast | `Qwen/Qwen3-Coder-30B-A3B-Instruct` | Code-specialized, efficient MoE inference |
| Reasoning | `openai/gpt-oss-120b` | Stronger deliberate reasoning and structured outputs |
| Agent | `Qwen/Qwen3-Coder-Next` or Qwen3-Coder 30B | Long-horizon coding, tool use, failure recovery |
| Selection | Qwen3-Coder 30B or a validated smaller tool model | Tool-call reliability with lower latency |
| Verification | `openai/gpt-oss-120b` | Conservative evidence judgment |

`Qwen3-Coder-Next` is an 80B-total/3B-active model designed for long-horizon
coding agents, complex tool usage, and recovery from failures. It is a stronger
agent candidate but requires substantially more model storage and memory than
Qwen3-Coder 30B.

Reference:
[Qwen3-Coder-Next model card](https://huggingface.co/Qwen/Qwen3-Coder-Next).

### Selection rule

Choose in this order:

1. Tool-call and structured-output compatibility.
2. Repository/code quality.
3. Context length that fits the actual prompt budget.
4. Warm latency.
5. Cost.

A larger model that cannot emit valid tool calls is worse for Context Engine
than a smaller model with reliable function calling.

## Smoke tests

Test the authenticated remote model directly:

```bash
curl -sS \
  -H "Authorization: Bearer $INFERENCE_GENERATION_API_KEY" \
  "$INFERENCE_GENERATION_ENDPOINT/api/tags"
```

Then test Context Engine:

```bash
curl -sS http://127.0.0.1:8088/healthcheck

curl -sS -X POST http://127.0.0.1:8088/summarize \
  -H "Content-Type: application/json" \
  --data '{"file":"ryemyster/context-manager/README.md"}'

curl -sS -X POST http://127.0.0.1:8088/vector-search \
  -H "Content-Type: application/json" \
  --data '{"query":"inference service","limit":2}'
```

The health response should show remote generation models available while
`nomic-embed-text` remains available through local Ollama.

For `/agents/run`, do not stop at `/health`. Verify all required behaviors:

1. `GET /models` returns the configured model identifier.
2. `/summarize` returns parseable structured content.
3. `/agents/run` emits and executes a repository tool call.
4. Agent verification returns structured JSON.
5. `/diff-summary` returns non-empty summary, risks, and test recommendations.

Only promote a model after all five checks pass.

## Recommended implementation choice

For the lowest operational and security burden:

1. Use Runpod MCP to provision and manage a private Serverless vLLM endpoint.
2. Use its authenticated OpenAI-compatible API for generation.
3. Keep local Ollama only for embeddings.
4. Keep both MCP servers local.
5. Use separate restricted keys for infrastructure management and inference.

Use an Ollama Pod only when Ollama-specific behavior is required and an
authenticated proxy has been built into the template.
