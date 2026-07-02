import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import config
from app.context_builder import (
    _issue_audit_evidence_files,
    _issue_numbers,
    _is_in_scope,
    _path_fragment_matches,
    build_context,
)


def write_file(root: Path, rel_path: str, content: str = "export const value = true;\n") -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class ContextBuilderHelperTests(unittest.TestCase):
    def test_scope_and_issue_helpers(self):
        prefixes = ["ryemyster/ShaleYeah/agents"]

        self.assertTrue(_is_in_scope("ryemyster/ShaleYeah/agents/geologist/src/agent/index.ts", prefixes))
        self.assertFalse(_is_in_scope("ascendvent/SevenSharp/.claude/settings.json", prefixes))
        self.assertEqual(_issue_numbers("audit issues #363-#376", []), list(range(363, 377)))

    def test_path_fragment_matches_keep_package_scoped_paths(self):
        all_files = [
            "ryemyster/ShaleYeah/agents/geologist/src/agent/index.ts",
            "ryemyster/ShaleYeah/agents/geologist/tests/agent.test.ts",
            "ascendvent/SevenSharp/src/agent/index.ts",
        ]

        matches = _path_fragment_matches(
            all_files,
            ["src/agent/index.ts", "agent.test.ts"],
            ["ryemyster/ShaleYeah/agents"],
        )

        self.assertEqual(
            matches,
            [
                "ryemyster/ShaleYeah/agents/geologist/src/agent/index.ts",
                "ryemyster/ShaleYeah/agents/geologist/tests/agent.test.ts",
            ],
        )

    def test_issue_audit_evidence_files_find_agent_impl_and_tests(self):
        all_files = [
            "ryemyster/ShaleYeah/agents/geologist/src/agent/index.ts",
            "ryemyster/ShaleYeah/agents/geologist/tests/agent.test.ts",
            "ryemyster/ShaleYeah/agents/geologist/tests/mcp-client.test.ts",
            "ryemyster/ShaleYeah/agents/geologist/README.md",
            "ascendvent/SevenSharp/agents/demo/tests/agent.test.ts",
        ]

        evidence = _issue_audit_evidence_files(all_files, ["ryemyster/ShaleYeah/agents"])

        self.assertEqual(
            evidence,
            [
                "ryemyster/ShaleYeah/agents/geologist/src/agent/index.ts",
                "ryemyster/ShaleYeah/agents/geologist/tests/agent.test.ts",
                "ryemyster/ShaleYeah/agents/geologist/tests/mcp-client.test.ts",
                "ryemyster/ShaleYeah/agents/geologist/README.md",
            ],
        )


class ContextBuilderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_context_filters_model_and_cross_repo_suggestions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_root = config.REPO_ROOT
            original_max_files = config.MAX_FILES_PER_SCAN
            config.REPO_ROOT = root
            config.MAX_FILES_PER_SCAN = 20
            try:
                write_file(root, "ryemyster/ShaleYeah/agents/geologist/src/agent/index.ts", "export function runTask() {}\n")
                write_file(root, "ryemyster/ShaleYeah/agents/geologist/tests/agent.test.ts", "test('agent', () => {});\n")
                write_file(root, "ryemyster/ShaleYeah/agents/geologist/tests/mcp-client.test.ts", "test('mcp', () => {});\n")
                write_file(root, "ascendvent/SevenSharp/.claude/settings.json", "{}\n")

                model_json = """{
                  "summary": "Issue audit evidence found.",
                  "risks": [],
                  "suggested_files": [
                    "src/agent/index.ts",
                    "ascendvent/SevenSharp/.claude/settings.json",
                    "ryemyster/ShaleYeah/agents/geologist/src/agent/index.ts"
                  ],
                  "audit_table": [
                    {
                      "issue": 363,
                      "status": "complete",
                      "evidence_files": ["ryemyster/ShaleYeah/agents/geologist/src/agent/index.ts"],
                      "missing_evidence": [],
                      "recommendation": "close"
                    }
                  ]
                }"""

                with patch("app.context_builder.inference.generate", AsyncMock(return_value=model_json)):
                    result = await build_context(
                        task="Read-only issue audit for issues #363-#376",
                        paths=["ryemyster/ShaleYeah/agents"],
                        focus=["issue 363", "src/agent/index.ts", "agent.test.ts", "mcp-client.test.ts"],
                        use_vector=False,
                    )

                self.assertIn(
                    "ryemyster/ShaleYeah/agents/geologist/src/agent/index.ts",
                    result["suggested_files"],
                )
                self.assertIn(
                    "ryemyster/ShaleYeah/agents/geologist/tests/agent.test.ts",
                    result["suggested_files"],
                )
                self.assertNotIn("src/agent/index.ts", result["suggested_files"])
                self.assertNotIn("ascendvent/SevenSharp/.claude/settings.json", result["suggested_files"])
                self.assertEqual(result["audit_table"][0]["issue"], 363)
                self.assertIn(
                    "ryemyster/ShaleYeah/agents/geologist/tests/mcp-client.test.ts",
                    result["audit_evidence_files"],
                )
            finally:
                config.REPO_ROOT = original_root
                config.MAX_FILES_PER_SCAN = original_max_files


if __name__ == "__main__":
    unittest.main()
