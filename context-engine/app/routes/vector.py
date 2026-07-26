"""
vector.py — vector search and indexing routes.
"""

import time
from fastapi import APIRouter
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

def safe_resolve(*a, **k): return main.safe_resolve(*a, **k)
def walk_repo(*a, **k): return main.walk_repo(*a, **k)
def read_file(*a, **k): return main.read_file(*a, **k)
def rel_path(*a, **k): return main.rel_path(*a, **k)
def chunk_text(*a, **k): return main.chunk_text(*a, **k)
from ..models import IndexRequest, VectorSearchRequest
from ..response_shaper import limits_for, reference, shaped_response

router = APIRouter()


@router.post("/vector-search")
async def vector_search(req: VectorSearchRequest):
    """
    Semantic vector search via Supabase pgvector.
    Embeds query using nomic-embed-text, searches existing index.
    Degrades gracefully if vectors not available.
    Writes: /output/vector-{slug}.md
    """
    t0 = time.monotonic()
    log.debug("POST /vector-search query=%r limit=%d", req.query, req.limit)
    detail, max_results, max_chars = limits_for(req)
    available = await supabase_vector.is_available()

    if not available:
        log.debug("POST /vector-search skipped — vector store not available")
        full_payload = {
            "query":      req.query,
            "matches":    [],
            "written_to": "",
            "available":  False,
        }
        return shaped_response(
            detail=detail,
            results=[],
            full_payload=full_payload,
            max_results=max_results,
            max_chars=max_chars,
            available=False,
        )

    embedding = await inference.embed(req.query)
    if embedding is None:
        log.warning("POST /vector-search embed failed query=%r", req.query)
        full_payload = {
            "query":      req.query,
            "matches":    [],
            "written_to": "",
            "available":  False,
        }
        return shaped_response(
            detail=detail,
            results=[],
            full_payload=full_payload,
            max_results=max_results,
            max_chars=max_chars,
            available=False,
        )

    matches  = await supabase_vector.search(embedding, limit=max_results + 1, threshold=req.threshold)
    written  = mw.write_vector_results(req.query, matches)

    log.debug("POST /vector-search done matches=%d dur=%.2fs", len(matches), time.monotonic() - t0)
    full_payload = {
        "query":      req.query,
        "matches":    matches,
        "written_to": written,
        "available":  True,
    }
    refs = [
        reference(
            kind="vector_match",
            path=m.get("path", ""),
            title=m.get("path", ""),
            summary=m.get("chunk", ""),
            score=m.get("similarity"),
            suffix=str(i),
        )
        for i, m in enumerate(matches)
    ]
    return shaped_response(
        detail=detail,
        results=refs,
        full_payload=full_payload,
        max_results=max_results,
        max_chars=max_chars,
        written_to=written,
        available=True,
    )


@router.post("/index")
async def index(req: IndexRequest):
    """
    Index code chunks into Supabase pgvector.

    ONLY uses nomic-embed-text (embed model). No qwen calls.
    This keeps memory pressure low and avoids model swaps during indexing.

    Run this before a coding session, not during one.
    On 8GB RAM CPU-only: ~1-2s per file. 80 files ≈ 2-3 minutes.
    """
    available = await supabase_vector.is_available()
    if not available:
        log.warning("POST /index skipped — vector store not available (migration not run?)")
        return {
            "paths":      req.paths,
            "indexed":    0,
            "skipped":    0,
            "errors":     0,
            "available":  False,
            "reason":     "vector store not ready — run the Supabase migration (supabase/migrations/) to create the code_embeddings table, then retry",
            "written_to": "",
        }

    indexed = 0
    skipped = 0
    errors  = 0

    for path in req.paths:
        try:
            base  = safe_resolve(path) if path and path != "." else config.REPO_ROOT
            files = walk_repo(base)

            for f in files[:config.MAX_FILES_PER_SCAN]:
                content = read_file(f)
                if not content.strip():
                    continue

                rp = rel_path(f)

                # Chunk into ~500-char pieces with 50-char overlap
                chunks = chunk_text(content, chunk_size=500, overlap=50)

                for chunk in chunks:
                    if not chunk.strip():
                        continue

                    h = supabase_vector.chunk_hash(rp, chunk)

                    if not req.force and await supabase_vector.already_indexed(h):
                        skipped += 1
                        continue

                    embedding = await inference.embed(f"{rp}\n{chunk}")
                    if embedding is None:
                        errors += 1
                        continue

                    ok = await supabase_vector.upsert_chunk(rp, chunk, embedding)
                    if ok:
                        indexed += 1
                    else:
                        errors += 1

        except Exception as e:
            log.warning("index error path=%s: %s", path, e)
            errors += 1

    log.debug("POST /index done indexed=%d skipped=%d errors=%d", indexed, skipped, errors)
    written = mw.write_index_report(req.paths, indexed, skipped, errors)

    return {
        "paths":      req.paths,
        "indexed":    indexed,
        "skipped":    skipped,
        "errors":     errors,
        "available":  True,
        "reason":     "",
        "written_to": written,
    }
