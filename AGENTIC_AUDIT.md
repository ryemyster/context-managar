# Repo Agent Audit: context-manager

## Classification
Agentic workflow

Confidence: High

One-sentence verdict:
This repo implements a real but narrow read-only repository-analysis agent loop alongside several deterministic LLM-assisted endpoints, so it is more than an LLM wrapper but not a full autonomous AI agent platform.

## Evidence Summary
| Capability | Present? | Evidence | Notes |
|---|---:|---|---|
| Model calls | Yes | `context-engine/app/ollama_client.py:33`, `context-engine/app/ollama_client.py:68`, `context-engine/app/ollama_client.py:129` | Uses Ollama generate, reasoning generate, embeddings, and chat-with-tools. |
| Tool use | Yes | `context-engine/app/tool_registry.py:268`, `context-engine/app/main.py:1408` | Five repo-analysis tools are registered and callable directly. |
| Model-selected tools | Yes | `context-engine/app/agent_runner.py:102`, `context-engine/app/agent_runner.py:115`, `context-engine/app/agent_runner.py:129` | The model receives tool schemas and chooses tool calls. |
| Planning | Partial | `context-engine/app/agent_runner.py:20`, `context-engine/app/agent_runner.py:79` | The prompt gives a recommended call order, but there is no explicit plan object or plan update mechanism. |
| Observation loop | Yes | `context-engine/app/agent_runner.py:122`, `context-engine/app/agent_runner.py:135` | Tool results are fed back as `tool` messages. |
| Multi-step autonomy | Yes | `context-engine/app/agent_runner.py:91`, `context-engine/app/config.py:22` | Iterates up to configured iteration and time budgets. |
| Durable memory | Partial | `context-engine/app/artifact_store.py:45`, `context-engine/app/artifact_store.py:89`, `context-engine/app/supabase_vector.py:211` | Runs and artifacts are persisted; generic agent loop does not automatically retrieve prior memories. |
| Context retrieval | Yes | `context-engine/app/context_builder.py:170`, `context-engine/app/context_builder.py:202`, `context-engine/app/tool_registry.py:191` | Deterministic grep, vector search, and model-callable code search/read tools. |
| External mutation | Partial | `context-engine/app/artifact_store.py:64`, `context-engine/app/supabase_vector.py:126` | Mutates output artifacts and Supabase vector table only; repo mount is read-only. |
| HITL / permissions | Partial | `context-engine/app/main.py:73`, `context-engine/app/repo_reader.py:14`, `docker-compose.yml:35` | Optional API key, path traversal guard, read-only repo volume. No per-action human approval gates. |
| Error recovery | Partial | `context-engine/app/agent_runner.py:104`, `context-engine/app/tool_registry.py:287`, `context-engine/app/supabase_vector.py:7` | Errors are returned to the model or degraded, but no structured retry classification or compensating behavior. |
| Evaluation/reflection | Partial | `context-engine/app/diff_reviewer.py:26`, `context-engine/app/issue_auditor.py:112` | Diff review and deterministic issue-audit classification exist; no agent self-critique loop. |

## Execution Topology
```mermaid
flowchart TD
  C[Client: REST, scripts, MCP editor] --> API[FastAPI app main.py]
  MCP[MCP stdio adapter] --> API

  API --> DET[Deterministic endpoints: scan/find/context/diff/draft/scaffold]
  DET --> O1[Ollama generate/reason/embed]
  DET --> FSRO[Repo files under REPO_ROOT, read-only in Docker]
  DET --> ART[Output artifacts: JSON records, markdown, events.jsonl]
  DET --> VEC[Supabase pgvector index]

  API --> AR[POST /agents/run]
  AR --> BG[Background task]
  BG --> LOOP[agent_runner.run_agent]
  LOOP --> CHAT[Ollama /api/chat with tool schemas]
  CHAT --> TC{Model tool_calls?}
  TC -->|yes| REG[tool_registry.execute_tool]
  REG --> FSRO
  REG --> OBS[tool result as observation]
  OBS --> LOOP
  TC -->|no| FINAL[final_answer]
  FINAL --> ART
  ART --> VEC
  API --> STATUS[GET /agents/run/status/{run_id}]
```

## Arcade Pattern Inventory
| Category | Pattern | Status | Evidence | Notes |
|---|---|---:|---|---|
| Tool | Tool | Present | `context-engine/app/tool_registry.py:268` | Registry maps tool names to schemas and executors. |
| Tool | Query Tool | Present | `context-engine/app/tool_registry.py:175`, `context-engine/app/tool_registry.py:191`, `context-engine/app/tool_registry.py:208`, `context-engine/app/tool_registry.py:230` | Tools are read-only repo inspection/search tools. |
| Tool | Command Tool | Partial | `context-engine/app/main.py:1175`, `context-engine/app/supabase_vector.py:126` | Indexing and artifact upserts mutate storage, but model-callable tools do not expose mutations. |
| Tool | Discovery Tool | Present | `context-engine/app/main.py:1421`, `context-engine/mcp_server.py:119` | `/agents/tools` and MCP `tools/list` reveal available schemas. |
| Tool Interface | Tool Description | Present | `context-engine/app/tool_registry.py:46`, `context-engine/app/tool_registry.py:71`, `context-engine/app/tool_registry.py:101` | Descriptions include when to call and expected result. |
| Tool Interface | Constrained Input | Partial | `context-engine/app/tool_registry.py:54`, `context-engine/app/models.py:198` | JSON schemas and Pydantic models exist, but few enums/ranges and limited validation. |
| Tool Interface | Smart Defaults | Present | `context-engine/app/tool_registry.py:193`, `context-engine/app/models.py:200` | Optional path defaults and empty tools list enables all tools. |
| Tool Interface | Natural Identifier | Partial | `context-engine/app/tool_registry.py:51`, `context-engine/app/repo_reader.py:14` | Human-readable repo-relative paths are resolved internally. |
| Tool Interface | Mutual Exclusivity | Absent |  | No exactly-one-of constraints found. |
| Tool Interface | Performance Hint | Present | `context-engine/app/tool_registry.py:48`, `context-engine/app/tool_registry.py:75`, `context-engine/app/tool_registry.py:136` | Descriptions guide efficient scan/find/grep use. |
| Tool Interface | Parameter Coercion | Present | `context-engine/app/agent_runner.py:39`, `tests/test_agent_runner.py:168` | JSON-string tool arguments are normalized. |
| Tool Discovery | Tool Registry | Present | `context-engine/app/tool_registry.py:268`, `context-engine/app/tool_registry.py:279` | Central registry returns tool definitions. |
| Tool Discovery | Schema Explorer | Partial | `context-engine/mcp_server.py:58`, `context-engine/mcp_server.py:119` | Tool schemas can be listed, but no layered drill-down beyond the manifest. |
| Tool Discovery | Dependency Hint | Present | `context-engine/app/tool_registry.py:47`, `context-engine/app/tool_registry.py:103` | Tool descriptions recommend call order. |
| Tool Discovery | Capability Matching | Partial | `context-engine/app/tool_registry.py:279` | Callers may request a subset by exact tool name; no intent-based matching. |
| Tool Discovery | Health Check | Present | `context-engine/app/tool_registry.py:156`, `context-engine/app/main.py:164` | Health endpoint and model-callable health tool exist. |
| Tool Composition | Abstraction Ladder | Present | `context-engine/app/main.py:856`, `context-engine/app/main.py:1130`, `context-engine/app/main.py:1175`, `context-engine/app/tool_registry.py:175` | Low-level scan/find/read plus higher-level `/context` and `/index`. |
| Tool Composition | Task Bundle | Present | `context-engine/app/context_builder.py:137`, `context-engine/app/main.py:1326` | `/context` and `/scaffold` bundle multi-step work. |
| Tool Composition | Batch Operation | Present | `context-engine/app/main.py:1203`, `context-engine/app/main.py:1357` | Index and scaffold loop over multiple paths/files. |
| Tool Composition | Operation Mode | Partial | `context-engine/app/models.py:58`, `context-engine/app/models.py:64` | Draft/scaffold support create/edit modes without enum enforcement. |
| Tool Composition | Tool Chain | Present | `context-engine/app/agent_runner.py:91`, `context-engine/app/agent_runner.py:122` | Agent chains model-selected tool calls. |
| Tool Composition | Scatter-Gather Tool | Partial | `context-engine/app/context_builder.py:145`, `context-engine/app/context_builder.py:170`, `context-engine/app/context_builder.py:202` | Gathers path scans, grep, and optional vector hits into one context bundle. |
| Tool Execution | Synchronous Execution | Present | `context-engine/app/main.py:1408`, `context-engine/app/tool_registry.py:285` | Direct tool calls are request/response. |
| Tool Execution | Async Job | Present | `context-engine/app/main.py:1502`, `context-engine/app/main.py:1537` | Agent and issue-auditor runs return run IDs and polling endpoints. |
| Tool Execution | Idempotent Operation | Partial | `context-engine/app/supabase_vector.py:88`, `context-engine/app/supabase_vector.py:92`, `context-engine/app/supabase_vector.py:134` | Chunk hashes and merge-duplicates make vector indexing partly retry-safe. |
| Tool Execution | Transactional Boundary | Absent |  | Multi-step artifact/vector writes are not all-or-nothing. |
| Tool Execution | Compensation Handler | Absent |  | No undo/rollback handlers found. |
| Tool Execution | Timeout Boundary | Present | `context-engine/app/config.py:16`, `context-engine/app/config.py:23`, `context-engine/app/agent_runner.py:95`, `context-engine/mcp_server.py:37` | Model, agent, and MCP tool-call timeouts exist. |
| Tool Output | Response Shaper | Present | `context-engine/app/tool_registry.py:187`, `context-engine/app/tool_registry.py:204`, `context-engine/mcp_server.py:131` | Raw data is converted into compact JSON/text/MCP content. |
| Tool Output | Token-Efficient Response | Present | `context-engine/app/tool_registry.py:33`, `context-engine/app/config.py:25` | Tool results are truncated with paging hints. |
| Tool Output | Paginated Result | Partial | `context-engine/app/tool_registry.py:115` | `read_file` supports offset/limit; no cursor pagination broadly. |
| Tool Output | Progressive Detail | Present | `context-engine/app/tool_registry.py:101`, `context-engine/app/tool_registry.py:115` | Read file can page for more detail after summaries/search. |
| Tool Output | GUI URL | Absent |  | No GUI links returned. |
| Tool Output | Partial Success | Present | `context-engine/app/main.py:1354`, `context-engine/app/supabase_vector.py:211` | Scaffold collects per-file errors; vector record indexing returns warnings. |
| Tool Context | Identity Anchor | Partial | `context-engine/app/main.py:94`, `context-engine/app/main.py:73` | Request IDs and optional API key; no user identity model. |
| Tool Context | Resource Reference | Present | `context-engine/app/artifact_store.py:81`, `context-engine/app/main.py:1562` | Responses reference record, markdown, and event-log paths. |
| Tool Context | Context Injection | Present | `context-engine/app/context_builder.py:271`, `context-engine/app/main.py:1282`, `context-engine/app/main.py:1342` | Relevant repo snippets/context files are injected into model prompts. |
| Tool Context | Context Boundary | Present | `context-engine/app/models.py:30`, `context-engine/app/repo_reader.py:14`, `docker-compose.yml:35` | Scoped path validation and read-only repo mount constrain context/actions. |
| Tool Resilience | Recovery Guide | Present | `context-engine/app/tool_registry.py:178`, `context-engine/app/tool_registry.py:217`, `context-engine/app/main.py:1195` | Error strings tell callers expected formats or next setup steps. |
| Tool Resilience | Error Classification | Partial | `context-engine/app/agent_runner.py:104`, `context-engine/app/main.py:1543` | Stop reasons distinguish timeout/model_error/max_iterations, but tool errors are plain strings. |
| Tool Resilience | Confirmation Request | Absent |  | Ambiguous inputs are rejected or defaulted; no clarification protocol. |
| Tool Resilience | Fuzzy Match Threshold | Partial | `context-engine/app/models.py:49`, `context-engine/app/supabase_vector.py:144` | Vector similarity threshold exists; not used for confirmation/fuzzy entity resolution. |
| Tool Resilience | Graceful Degradation | Present | `context-engine/app/supabase_vector.py:7`, `context-engine/app/main.py:1142`, `context-engine/app/main.py:1151` | Vector/Supabase failures return empty results rather than crashing. |
| Tool Resilience | Fallback Tool | Partial | `context-engine/app/tool_registry.py:75`, `context-engine/app/tool_registry.py:136` | Descriptions suggest alternatives, but fallback is not automated. |
| Tool Security | Secret Injection | Present | `context-engine/app/config.py:41`, `docker-compose.yml:29` | Supabase credentials come from environment/config. |
| Tool Security | Permission Gate | Present | `context-engine/app/main.py:73`, `context-engine/app/repo_reader.py:14` | Optional API key and path traversal guard. |
| Tool Security | Scope Declaration | Absent |  | No OAuth/API scope declarations per tool. |
| Tool Security | Audit Trail | Present | `context-engine/app/artifact_store.py:70`, `context-engine/app/logger.py`, `context-engine/app/agent_runner.py:127` | Event log, request IDs, and tool-call traces are stored/logged. |
| Compositional | Tool Gateway | Present | `context-engine/mcp_server.py:3`, `context-engine/app/main.py:1408` | REST and MCP expose a unified facade over tool backends. |
| Compositional | Tool Adapter | Present | `context-engine/mcp_server.py:58`, `context-engine/mcp_server.py:80` | MCP adapter wraps REST tools for editors. |
| Compositional | Canonical Tool Model | Partial | `context-engine/app/tool_registry.py:40`, `context-engine/mcp_server.py:67` | OpenAI/Ollama-like schemas are converted to MCP; no versioned shared contract object. |
| Compositional | Tool Versioning | Absent |  | No coexistence of multiple tool versions found. |

Collapsed absent patterns: Mutual Exclusivity, Transactional Boundary, Compensation Handler, GUI URL, Confirmation Request, Scope Declaration, Tool Versioning.

## Pattern Score
- Tool: 2
- Tool Interface: 2
- Tool Discovery: 2
- Tool Composition: 2
- Tool Execution: 2
- Tool Output: 2
- Tool Context: 2
- Tool Resilience: 2
- Tool Security: 2
- Compositional: 2

Total: 20/30

Interpretation:
Solid agentic tool design.

## Agenticity Score
- Goal representation: 1
- Planning: 1
- Dynamic tool selection: 3
- Observation/action loop: 3
- Persistent memory: 2
- Context retrieval: 3
- Autonomy: 2
- Error recovery: 1
- External action capability: 1
- Evaluation/reflection: 1

Total: 18/30

Interpretation:
Agentic workflow.

## What Makes It Agentic
- `run_agent()` represents the user task as a message history, gives the model available tool definitions, and lets the model decide whether to call tools or answer (`context-engine/app/agent_runner.py:79`, `context-engine/app/agent_runner.py:102`, `context-engine/app/agent_runner.py:115`).
- Tool results are observed by the model in later iterations through appended `tool` messages (`context-engine/app/agent_runner.py:122`, `context-engine/app/agent_runner.py:135`).
- The loop has autonomous continuation with iteration and wall-clock stopping conditions (`context-engine/app/agent_runner.py:91`, `context-engine/app/agent_runner.py:95`, `context-engine/app/config.py:23`).
- Tool schemas are exposed both to the internal Ollama chat API and external MCP clients (`context-engine/app/tool_registry.py:42`, `context-engine/app/main.py:1421`, `context-engine/mcp_server.py:119`).
- Agent runs persist full run metadata and tool-call traces as JSON/Markdown artifacts (`context-engine/app/main.py:1440`, `context-engine/app/main.py:1464`, `context-engine/app/artifact_store.py:89`).

## What Makes It A Wrapper
- Many endpoints are fixed application workflows with one prompt-response model call after deterministic preprocessing, such as `/find`, `/context`, `/diff-summary`, `/draft`, and `/scaffold` (`context-engine/app/main.py:681`, `context-engine/app/context_builder.py:258`, `context-engine/app/diff_reviewer.py:26`, `context-engine/app/main.py:1258`).
- Planning is mostly prompt guidance and ordinary code flow; there is no explicit plan data structure, replanning step, or task decomposition object.
- The model-callable tool surface is read-only and narrow: scan, find, read, grep, health (`context-engine/app/tool_registry.py:268`).
- Stored artifacts can be indexed, but the generic agent loop does not automatically search prior run memory before acting.
- External mutations are mostly local artifact writes and Supabase vector upserts, not arbitrary action execution or repo changes.

## Pattern Gaps
- Missing pattern: Confirmation Request
  - Why it matters: The agent currently returns errors or defaults broadly when inputs are ambiguous.
  - Where it would fit: `tool_registry.execute_tool()` and path/query tools.
  - Minimal implementation suggestion: Return a structured `needs_confirmation` result with candidate paths or interpretations, and have `agent_runner` stop or ask the caller instead of guessing.

- Missing pattern: Transactional Boundary
  - Why it matters: Agent runs write markdown, JSON records, event-log rows, and vector entries in separate steps.
  - Where it would fit: `_run_agent_background()` and `_run_issue_audit_background()`.
  - Minimal implementation suggestion: Write to temp files first, commit JSON/Markdown together, then append the event log as the final durable marker.

- Missing pattern: Scope Declaration
  - Why it matters: Tools do not declare whether they need repo read, artifact write, vector write, network, or admin credentials.
  - Where it would fit: Tool schema metadata in `tool_registry.py`.
  - Minimal implementation suggestion: Add non-model-enforced metadata such as `required_scopes: ["repo:read"]` and enforce it in `execute_tool()`.

- Missing pattern: Tool Versioning
  - Why it matters: External MCP clients consume schemas that may change without compatibility guarantees.
  - Where it would fit: `/agents/tools`, MCP `tools/list`, and the `_REGISTRY` keys.
  - Minimal implementation suggestion: Add schema versions and stable aliases, for example `read_file.v1`, while keeping current names as compatibility aliases.

- Missing pattern: Memory Retrieval In Agent Loop
  - Why it matters: Durable records are stored and indexed, but prior results do not automatically influence later `/agents/run` behavior.
  - Where it would fit: `agent_runner.run_agent()` or a new `search_memory` tool.
  - Minimal implementation suggestion: Add a read-only `search_memory` tool over artifact records/vector hits and recommend it before repo scans for repeated tasks.

## Control Flow Assessment
- Who chooses the next action: In `/agents/run`, the model chooses among exposed tools or final answer. In most other endpoints, ordinary code chooses the sequence and the model only synthesizes text/JSON.
- Can the system continue across multiple steps without a new user request: Yes, within one background agent run until final answer, timeout, model error, or max iterations.
- Can it observe outcomes and revise its plan: Partially. It observes tool outputs and can choose another tool call, but there is no explicit plan revision data model.
- Can it write state that changes later behavior: Partially. It writes durable artifacts and vector records; later deterministic/vector endpoints can use indexed data, but `/agents/run` does not currently retrieve memory automatically.
- Are tool patterns mature enough to support reliable agency: Adequate for read-only repo scouting. They are not mature enough for high-risk mutation workflows because there are no transactional semantics, confirmation protocol, per-tool scopes, or compensation handlers.

## Final Verdict
This repo is best described as an agentic workflow because it has a genuine model-driven tool loop with observations, iteration, run IDs, durable traces, and context retrieval, but its autonomy is scoped to read-only repository analysis and much of the product remains deterministic LLM endpoint orchestration rather than a full goal-pursuing AI agent.

## Use-Case Fit And Recommendations
For this repo's stated use case, the current architectural direction is appropriate: a bounded, read-only repository-analysis agent is the right level of agency. The strongest design choice is that the model can inspect code dynamically through `scan_directory`, `find_in_code`, `read_file`, `grep`, and `health_check`, while repo writes and higher-risk decisions remain outside the junior agent loop.

This should not be pushed toward a broad mutation-capable autonomous agent yet. The safer and higher-leverage path is to harden the existing agentic workflow:

1. Add a `search_memory` tool so `/agents/run` can retrieve prior artifact records and vector-indexed run history automatically.
2. Replace plain-string tool results with structured envelopes such as `ok`, `error_type`, `retryable`, `data`, and `recovery_hint`.
3. Add tool scope metadata such as `repo:read`, `artifact:write`, and `vector:write`, then enforce those scopes in `execute_tool()`.
4. Add confirmation or clarification behavior for ambiguous paths, broad searches, and uncertain matches.
5. Version tool schemas before external MCP/editor clients depend on them heavily.

The architecture becomes materially riskier if model-selected tools are allowed to write files, run shell commands, or mutate external systems before permission gates, confirmations, transactional boundaries, compensation behavior, and audit controls are implemented.
