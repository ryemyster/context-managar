import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import artifact_store, config
from app import supabase_vector


class ArtifactStoreTests(unittest.TestCase):
    def test_write_record_creates_json_event_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_output = config.OUTPUT_DIR
            config.OUTPUT_DIR = Path(tmp)
            try:
                meta = artifact_store.write_record(
                    tool="agents/issue-auditor",
                    request={"repo": "ryemyster/ShaleYeah"},
                    response={"status": "complete"},
                    markdown="# Audit",
                    status="complete",
                )

                record_path = Path(meta["record"])
                markdown_path = Path(meta["markdown"])
                event_log = Path(meta["event_log"])

                self.assertTrue(record_path.exists())
                self.assertTrue(markdown_path.exists())
                self.assertTrue(event_log.exists())

                record = json.loads(record_path.read_text(encoding="utf-8"))
                self.assertEqual(record["tool"], "agents/issue-auditor")
                self.assertEqual(record["response"]["status"], "complete")

                event = json.loads(event_log.read_text(encoding="utf-8").strip())
                self.assertEqual(event["event_id"], meta["event_id"])
                self.assertEqual(event["record"], str(record_path))
            finally:
                config.OUTPUT_DIR = original_output

    def test_store_artifact_record_uses_typed_vector_path(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmp:
                record_path = Path(tmp) / "record.json"
                record_path.write_text(
                    json.dumps(
                        {
                            "event_id": "context-123",
                            "tool": "context",
                            "status": "complete",
                            "request": {"task": "test"},
                            "response": {"summary": "ok"},
                        }
                    ),
                    encoding="utf-8",
                )

                upserts = []

                async def fake_upsert(path, chunk, embedding):
                    upserts.append((path, chunk, embedding))
                    return True

                with (
                    patch("app.supabase_vector.is_available", AsyncMock(return_value=True)),
                    patch("app.supabase_vector.already_indexed", AsyncMock(return_value=False)),
                    patch("app.ollama_client.embed", AsyncMock(return_value=[0.1, 0.2])),
                    patch("app.supabase_vector.upsert_chunk", fake_upsert),
                ):
                    await supabase_vector.store_artifact_record(str(record_path), source_type="context_record")

                self.assertTrue(upserts)
                self.assertEqual(upserts[0][0], "context_record://context/context-123")
                self.assertIn('"source_type": "context_record"', upserts[0][1])

        import asyncio

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
