"""
agents.py — agent run, issue auditor, draft, scaffold, and tool execution routes.
"""

import time
import asyncio
import traceback
import json
import re
from pathlib import Path
from fastapi import APIRouter, HTTPException, BackgroundTasks
from .. import main

class ConfigProxy:
    def __getattr__(self, name): return getattr(main.config, name)
config = ConfigProxy()

class SupabaseProxy:
    def __getattr__(self, name): return getattr(main.supabase_vector, name)
supabase_vector = SupabaseProxy()

class InferenceProxy:
    def __getattr__(self, name): return getattr(main.inference, name)
inference = InferenceProxy()

class LogProxy:
    def __getattr__(self, name): return getattr(main.log, name)
log = LogProxy()

class MwProxy:
    def __getattr__(self, name): return getattr(main.mw, name)
mw = MwProxy()

class ArtifactStoreProxy:
    def __getattr__(self, name): return getattr(main.artifact_store, name)
artifact_store = ArtifactStoreProxy()

class MetricsProxy:
    def __getattr__(self, name): return getattr(main.metrics, name)
metrics = MetricsProxy()

class ToolRegistryProxy:
    def __getattr__(self, name): return getattr(main.tool_registry, name)
tool_registry = ToolRegistryProxy()

class AgentRunnerProxy:
    def __getattr__(self, name): return getattr(main.agent_runner, name)
agent_runner = AgentRunnerProxy()
def collect_agent_evidence(*a, **k): return main.collect_agent_evidence(*a, **k)
def findings_from_evidence(*a, **k): return main.findings_from_evidence(*a, **k)
def insufficient_evidence_findings(*a, **k): return main.insufficient_evidence_findings(*a, **k)
def _issue_numbers(*a, **k): return main._issue_numbers(*a, **k)
def build_context(*a, **k): return main.build_context(*a, **k)
def safe_resolve(*a, **k): return main.safe_resolve(*a, **k)
def read_file(*a, **k): return main.read_file(*a, **k)
from ..models import (
    DraftRequest, DraftResponse, ScaffoldRequest, ScaffoldResponse, ScaffoldFileResult,
    ToolCallRequest, AgentRunRequest, AgentRunResponse, IssueAuditRequest, IssueAuditResponse
)

router = APIRouter()


# ── Draft ──────────────────────────────────────────────────────────────────────

@router.post("/draft", response_model=DraftResponse)
async def draft(req: DraftRequest):
    """
    Two-tier agent: Claude (SR dev) plans → qwen (JR dev) generates → Claude reviews and applies.

    Reads the target file + any context_files, builds a prompt, and generates
    code using qwen2.5-coder. Returns the draft as text — Claude owns all writes.

    mode="create" → generate a new file from scratch
    mode="edit"   → read the existing file and apply the described change
    """
    t0 = time.monotonic()
    log.debug("POST /draft file=%s mode=%s task=%r", req.file, req.mode, req.task)

    # Read target file (if editing an existing one)
    existing_content = ""
    if req.mode == "edit":
        try:
            f = safe_resolve(req.file)
            if f.exists() and f.is_file():
                existing_content = read_file(f)
        except Exception as e:
            log.debug("draft: could not read target file %s: %s", req.file, e)

    # Read context files
    context_blocks: list[str] = []
    for cf in req.context_files[:5]:
        try:
            f = safe_resolve(cf)
            content = read_file(f)
            if content:
                context_blocks.append(f"// {cf}\n{content[:2000]}")
        except Exception:
            pass

    context_section = "\n\n".join(context_blocks)

    if req.mode == "edit" and existing_content:
        prompt = f"""Task: {req.task}
File: {req.file}
{("References:" + chr(10) + context_section) if context_section else ""}
Existing:
{existing_content[:config.MAX_TOTAL_CHARS]}

Return the complete updated file only. No explanation."""
    else:
        prompt = f"""Task: {req.task}
File: {req.file}
{("References:" + chr(10) + context_section) if context_section else ""}

Return the complete new file only. No explanation."""

    code = await inference.generate(prompt)

    # Strip accidental markdown fences the model may add despite instructions
    code = re.sub(r"^```[a-z]*\n?", "", code.strip(), flags=re.MULTILINE)
    code = re.sub(r"\n?```$", "", code.strip(), flags=re.MULTILINE)

    written = mw.write_draft(file=req.file, task=req.task, mode=req.mode, code=code)

    asyncio.create_task(supabase_vector.store_artifact(written))
    log.debug("POST /draft done mode=%s code_len=%d dur=%.2fs", req.mode, len(code), time.monotonic() - t0)
    return DraftResponse(file=req.file, mode=req.mode, code=code, written_to=written)


# ── Scaffold ───────────────────────────────────────────────────────────────────

@router.post("/scaffold", response_model=ScaffoldResponse)
async def scaffold(req: ScaffoldRequest):
    """
    Multi-file code generation for context window preservation.

    Use when Claude has broken a feature into files and wants to delegate
    the mechanical generation of each one — preserving its own context window
    for orchestration, review, and decisions rather than typing.

    Each file is generated sequentially (memory constraint — one model at a time).
    Claude reviews the full batch via the artifacts dir scaffold-*.md before applying anything.
    Claude owns all writes.
    """
    t0 = time.monotonic()
    log.debug("POST /scaffold task=%r files=%d", req.task, len(req.files))

    # Read shared context files once — passed to every generation
    shared_context_blocks: list[str] = []
    for cf in req.context_files[:5]:
        try:
            f = safe_resolve(cf)
            content = read_file(f)
            if content:
                shared_context_blocks.append(f"// {cf}\n{content[:1500]}")
        except Exception:
            pass
    shared_context = "\n\n".join(shared_context_blocks)

    results: list[ScaffoldFileResult] = []
    errors: list[str] = []

    for sf in req.files:
        try:
            existing_content = ""
            if sf.mode == "edit":
                try:
                    f = safe_resolve(sf.file)
                    if f.exists():
                        existing_content = read_file(f)
                except Exception:
                    pass

            if sf.mode == "edit" and existing_content:
                prompt = f"""Task: {req.task}
File job: {sf.spec}
File: {sf.file}
{("References:" + chr(10) + shared_context) if shared_context else ""}
Existing:
{existing_content[:config.MAX_TOTAL_CHARS]}

Return the complete updated file only. No explanation."""
            else:
                prompt = f"""Task: {req.task}
File job: {sf.spec}
File: {sf.file}
{("References:" + chr(10) + shared_context) if shared_context else ""}

Return the complete new file only. No explanation."""

            code = await inference.generate(prompt)

            code = re.sub(r"^```[a-z]*\n?", "", code.strip(), flags=re.MULTILINE)
            code = re.sub(r"\n?```$", "", code.strip(), flags=re.MULTILINE)

            written = mw.write_scaffold_file(
                file=sf.file, task=req.task, spec=sf.spec, mode=sf.mode, code=code
            )
            asyncio.create_task(supabase_vector.store_artifact(written))
            results.append(ScaffoldFileResult(file=sf.file, mode=sf.mode, code=code, written_to=written))
            log.debug("scaffold file=%s done code_len=%d", sf.file, len(code))

        except Exception as e:
            log.error("scaffold file=%s error: %s", sf.file, e)
            errors.append(f"{sf.file}: {e}")

    log.debug("POST /scaffold done files=%d errors=%d dur=%.2fs", len(results), len(errors), time.monotonic() - t0)
    return ScaffoldResponse(task=req.task, files=results, total=len(results), errors=errors)


# ── Generic Agent Run ──────────────────────────────────────────────────────────

@router.post("/tools/call")
async def tools_call(req: ToolCallRequest):
    """
    Execute a single tool by name. Used by the MCP wrapper and any caller that
    wants direct tool access without going through the full agent loop.

    Available tools: scan_directory, find_in_code, read_file, grep, health_check, search_memory, update_plan
    Returns: {"name": str, "result": str, "ok": bool, "error_type": str | null}
    """
    result = await tool_registry.execute_tool(req.name, req.arguments)
    return {"name": req.name, "result": result.data, "ok": result.ok, "error_type": result.error_type}


@router.get("/agents/tools")
async def agents_tools():
    """
    Tool manifest — returns JSON schemas and metadata for all tools the agent can call.
    Any calling agent (Claude, Codex, Qwen, etc.) hits this to discover capabilities.
    Schema format is OpenAI/Ollama/MCP-compatible.
    Each entry includes 'scopes' and 'side_effects' from ToolMeta.
    """
    tools = []
    for schema in tool_registry.get_tool_definitions():
        name = schema["function"]["name"]
        meta = tool_registry.get_tool_metadata(name)
        tools.append({
            **schema,
            "scopes":       meta.scopes if meta else [],
            "side_effects": meta.side_effects if meta else False,
        })
    return {"tools": tools}


async def _run_agent_background(run_id: str, req: AgentRunRequest) -> None:
    """Background worker for POST /agents/run."""
    # Defensive guard only. Individual model turns are bounded inside the agent.
    _wall_limit = config.OLLAMA_AGENT_TIMEOUT + config.OLLAMA_AGENT_CALL_TIMEOUT
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            agent_runner.run_agent(
                task=req.task,
                tools=req.tools or None,
                system_prompt=req.system_prompt,
                max_iterations=req.max_iterations,
                allowed_scopes=req.allowed_scopes,
                required_paths=req.required_paths,
            ),
            timeout=_wall_limit,
        )
    except asyncio.TimeoutError:
        metrics.record_agent_run(
            kind="agents/run",
            duration_ms=(time.monotonic() - started) * 1000,
            outcome="timeout",
        )
        log.error("agent/run background wall-clock timeout run_id=%s limit=%.0fs", run_id, _wall_limit)
        artifact_store.write_record(
            event_id=run_id,
            tool="agents/run",
            request=req.model_dump(),
            response={
                "run_id": run_id, "status": "timeout", "task": req.task,
                "final_answer": "[agent timed out — wall-clock limit exceeded]",
                "tool_calls_made": [], "iterations": 0,
                "stopped_reason": "timeout",
                "warnings": [f"wall-clock limit of {_wall_limit:.0f}s exceeded"],
            },
            status="timeout",
        )
        return
    except Exception:
        metrics.record_agent_run(
            kind="agents/run",
            duration_ms=(time.monotonic() - started) * 1000,
            outcome="error",
        )
        log.error("agent/run background error run_id=%s\n%s", run_id, traceback.format_exc())
        artifact_store.write_record(
            event_id=run_id,
            tool="agents/run",
            request=req.model_dump(),
            response={"run_id": run_id, "status": "error", "task": req.task,
                      "final_answer": "", "tool_calls_made": [], "iterations": 0,
                      "stopped_reason": "error", "warnings": ["background task failed — check logs"]},
            status="error",
        )
        return
    try:
        warnings = []
        if result.stopped_reason != "final_answer":
            warnings.append(
                f"agent stopped with {result.stopped_reason}; final_answer is diagnostic, not a verified answer"
            )
        if result.stopped_reason == "final_answer" and result.verification.get("passed") is not True:
            warnings.append("final_answer was not positively verified")
        if not result.evidence_coverage.get("complete", True):
            warnings.append(
                "required source files were not read: "
                + ", ".join(result.evidence_coverage.get("missing_paths", []))
            )
        if result.unknown_tools:
            warnings.append(
                f"ignored unknown tools: {result.unknown_tools}; see GET /agents/tools for valid names"
            )

        written = mw.write_agent_run(
            task=req.task,
            final_answer=result.final_answer,
            tool_calls_made=result.tool_calls_made,
            iterations=result.iterations,
            stopped_reason=result.stopped_reason,
            warnings=warnings,
        )
        markdown_content = ""
        try:
            markdown_content = Path(written).read_text(encoding="utf-8")
        except Exception:
            pass

        if result.stopped_reason == "final_answer" and result.verification.get("passed") is True:
            work_status = "verified"
        elif result.stopped_reason in {"timeout", "model_error", "error"} and not result.tool_calls_made:
            work_status = "failed"
        elif result.tool_calls_made:
            work_status = "partial"
        else:
            work_status = "blocked"

        response_payload = {
            "run_id":               run_id,
            "status":               work_status,
            "task":                 req.task,
            "final_answer":         result.final_answer,
            "tool_calls_made":      result.tool_calls_made,
            "iterations":           result.iterations,
            "stopped_reason":       result.stopped_reason,
            "warnings":             warnings,
            "memory_context_used":  result.memory_context_used,
            "memory_hits":          result.memory_hits,
            "plan_state":           result.plan_state,
            "verification":         result.verification,
            "evidence_coverage":    result.evidence_coverage,
        }
        artifacts = artifact_store.write_record(
            event_id=run_id,
            tool="agents/run",
            request=req.model_dump(),
            response=response_payload,
            markdown=markdown_content or None,
            status=response_payload["status"],
        )
        response_payload["artifacts"] = artifacts

        vector_warnings = await supabase_vector.store_artifact_record(artifacts["record"], source_type="agent_run")
        if vector_warnings:
            response_payload["warnings"] = vector_warnings
            artifact_store.write_record(
                event_id=run_id,
                tool="agents/run",
                request=req.model_dump(),
                response=response_payload,
                markdown=markdown_content or None,
                status=response_payload["status"],
            )

        asyncio.create_task(supabase_vector.store_artifact(written))
        metrics.record_agent_run(
            kind="agents/run",
            duration_ms=(time.monotonic() - started) * 1000,
            outcome=response_payload["status"],
        )
        log.debug("agent/run background done run_id=%s stopped=%s iter=%d tool_calls=%d",
                  run_id, result.stopped_reason, result.iterations, len(result.tool_calls_made))
    except Exception:
        metrics.record_agent_run(
            kind="agents/run",
            duration_ms=(time.monotonic() - started) * 1000,
            outcome="error",
        )
        log.error("agent/run background error run_id=%s\n%s", run_id, traceback.format_exc())
        artifact_store.write_record(
            event_id=run_id,
            tool="agents/run",
            request=req.model_dump(),
            response={"run_id": run_id, "status": "error", "task": req.task,
                      "final_answer": "", "tool_calls_made": [], "iterations": 0,
                      "stopped_reason": "error", "warnings": ["background task failed — check logs"]},
            status="error",
        )


@router.post("/agents/run", response_model=AgentRunResponse)
async def agents_run(req: AgentRunRequest, background_tasks: BackgroundTasks):
    """
    Delegate any task to the local junior agent.

    The agent autonomously decides what files to read/scan/grep, loops until it has
    enough evidence, and returns a final answer with a full tool-call trace.

    Returns immediately with run_id and status "running".
    Poll GET /agents/run/status/{run_id} until status is no longer "running".

    Body:
      task            — what to accomplish (required)
      tools           — tool names to enable; empty = all tools
      max_iterations  — default 10
      system_prompt   — override the default system prompt

    GET /agents/tools to see available tool definitions and schemas.
    """
    pending = artifact_store.write_pending_record(
        tool="agents/run",
        request=req.model_dump(),
    )
    run_id = pending["event_id"]
    background_tasks.add_task(_run_agent_background, run_id, req)

    log.debug("POST /agents/run queued run_id=%s task_len=%d", run_id, len(req.task))
    return AgentRunResponse(
        run_id=run_id,
        status="running",
        task=req.task,
        artifacts=pending,
    )


@router.get("/agents/run/status/{run_id}")
async def agents_run_status(run_id: str):
    """
    Poll the status of a queued agent run.

    Same shape as POST /agents/run once complete.
    Status values: "running" | "complete" | "final_answer" | "max_iterations" | "timeout" | "model_error" | "error"
    """
    if not all(c.isalnum() or c == "-" for c in run_id):
        raise HTTPException(status_code=400, detail="invalid run_id format")
    record_path = config.OUTPUT_DIR / "records" / f"{run_id}.json"
    if not record_path.exists():
        raise HTTPException(status_code=404, detail=f"run_id {run_id!r} not found")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    resp = dict(record.get("response") or {})
    resp["run_id"]         = run_id
    resp["status"]         = record.get("status", "unknown")
    resp.setdefault("task",            "")
    resp.setdefault("final_answer",    "")
    resp.setdefault("tool_calls_made", [])
    resp.setdefault("iterations",      0)
    resp.setdefault("stopped_reason",  "")
    resp.setdefault("warnings",        [])
    markdown_path = config.OUTPUT_DIR / "markdown" / f"{run_id}.md"
    resp["artifacts"] = {
        "event_id":  run_id,
        "record":    str(record_path),
        "markdown":  str(markdown_path) if markdown_path.exists() else None,
        "event_log": str(config.OUTPUT_DIR / "events.jsonl"),
    }
    return resp


# ── Issue Auditor ──────────────────────────────────────────────────────────────

async def _run_issue_audit_background(
    run_id: str,
    req: IssueAuditRequest,
    scoped_paths: list[str],
    task: str,
) -> None:
    """Background worker for /agents/issue-auditor/run. Overwrites the pending record when done."""
    started = time.monotonic()
    try:
        result = await build_context(
            task=task,
            paths=scoped_paths,
            focus=req.focus + req.requirements,
            use_vector=req.use_vector,
            force_issue_audit=True,
        )
        evidence_matrix = collect_agent_evidence(scoped_paths)
        deterministic_findings = findings_from_evidence(evidence_matrix, _issue_numbers(task, req.focus))

        written = mw.write_context(
            task=task,
            files=result["files"],
            summary=result["summary"],
            risks=result["risks"],
            suggested_files=result["suggested_files"],
            vector_hits=result["vector_hits"],
            audit_table=deterministic_findings or result.get("audit_table", []),
            warnings=result.get("warnings", []),
            dropped_candidates=result.get("dropped_candidates", []),
        )
        markdown_content = ""
        try:
            markdown_content = Path(written).read_text(encoding="utf-8")
        except Exception:
            pass

        if deterministic_findings:
            findings = deterministic_findings
        elif scoped_paths:
            findings = insufficient_evidence_findings(_issue_numbers(task, req.focus))
        else:
            findings = result.get("audit_table", [])
        run_status = "complete" if findings else "insufficient_evidence"
        warnings = list(result.get("warnings", []))

        response_payload = {
            "run_id":          run_id,
            "status":          run_status,
            "findings":        findings,
            "evidence_matrix": evidence_matrix,
            "suggested_files": result["suggested_files"],
            "warnings":        warnings,
        }
        artifacts = artifact_store.write_record(
            event_id=run_id,
            tool="agents/issue-auditor",
            request=req.model_dump(),
            response=response_payload,
            markdown=markdown_content or None,
            status=run_status,
        )

        # Await so upsert failures surface in the stored record's warnings
        vector_warnings = await supabase_vector.store_artifact_record(artifacts["record"], source_type="agent_run")
        if vector_warnings:
            warnings.extend(vector_warnings)
            response_payload["warnings"] = warnings
            artifact_store.write_record(
                event_id=run_id,
                tool="agents/issue-auditor",
                request=req.model_dump(),
                response=response_payload,
                markdown=markdown_content or None,
                status=run_status,
            )

        asyncio.create_task(supabase_vector.store_artifact(written))
        metrics.record_agent_run(
            kind="agents/issue-auditor",
            duration_ms=(time.monotonic() - started) * 1000,
            outcome=run_status,
        )
        log.debug("issue-auditor background done run_id=%s findings=%d warnings=%d",
                  run_id, len(findings), len(warnings))
    except Exception:
        metrics.record_agent_run(
            kind="agents/issue-auditor",
            duration_ms=(time.monotonic() - started) * 1000,
            outcome="error",
        )
        log.error("issue-auditor background error run_id=%s\n%s", run_id, traceback.format_exc())
        artifact_store.write_record(
            event_id=run_id,
            tool="agents/issue-auditor",
            request=req.model_dump(),
            response={"run_id": run_id, "status": "error", "findings": [], "evidence_matrix": [],
                      "suggested_files": [], "warnings": ["background task failed — check logs"]},
            status="error",
        )


@router.post("/agents/issue-auditor/run", response_model=IssueAuditResponse)
async def issue_auditor(req: IssueAuditRequest, background_tasks: BackgroundTasks):
    """
    Agentified issue audit workflow — returns immediately with run_id and status "running".

    Poll GET /agents/issue-auditor/status/{run_id} until status is no longer "running".
    The model call and evidence collection run in the background.
    Codex/Claude still owns final decisions and any GitHub writes.
    """
    scoped_paths = []
    for path in req.paths:
        clean = path.strip().strip("/")
        if clean.startswith(f"{req.repo.strip().strip('/')}/"):
            scoped_paths.append(clean)
        else:
            scoped_paths.append(f"{req.repo.strip().strip('/')}/{clean}")

    task = req.task
    if req.requirements:
        task += "\nRequirements:\n" + "\n".join(f"- {item}" for item in req.requirements)
    task += "\nReturn an issue audit table with issue, status, evidence_files, missing_evidence, recommendation."

    pending = artifact_store.write_pending_record(
        tool="agents/issue-auditor",
        request=req.model_dump(),
    )
    run_id = pending["event_id"]
    background_tasks.add_task(_run_issue_audit_background, run_id, req, scoped_paths, task)

    log.debug("POST /agents/issue-auditor/run queued run_id=%s", run_id)
    return IssueAuditResponse(
        run_id=run_id,
        status="running",
        findings=[],
        evidence_matrix=[],
        suggested_files=[],
        warnings=[],
        artifacts=pending,
    )


@router.get("/agents/issue-auditor/status/{run_id}")
async def issue_auditor_status(run_id: str):
    """
    Poll the status of a queued issue audit run.

    Returns the same shape as /agents/issue-auditor/run once complete.
    Status values: "running" | "complete" | "insufficient_evidence" | "error"
    """
    if not all(c.isalnum() or c == "-" for c in run_id):
        raise HTTPException(status_code=400, detail="invalid run_id format")
    record_path = config.OUTPUT_DIR / "records" / f"{run_id}.json"
    if not record_path.exists():
        raise HTTPException(status_code=404, detail=f"run_id {run_id!r} not found")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    resp = dict(record.get("response") or {})
    resp["run_id"] = run_id
    resp["status"] = record.get("status", "unknown")
    resp.setdefault("findings",        [])
    resp.setdefault("evidence_matrix", [])
    resp.setdefault("suggested_files", [])
    resp.setdefault("warnings",        [])
    markdown_path = config.OUTPUT_DIR / "markdown" / f"{run_id}.md"
    resp["artifacts"] = {
        "event_id":  run_id,
        "record":    str(record_path),
        "markdown":  str(markdown_path) if markdown_path.exists() else None,
        "event_log": str(config.OUTPUT_DIR / "events.jsonl"),
    }
    return resp
