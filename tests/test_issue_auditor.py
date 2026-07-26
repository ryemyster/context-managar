import tempfile
import unittest
from pathlib import Path

from app import config
from app.issue_auditor import collect_agent_evidence, findings_from_evidence, insufficient_evidence_findings


def write_file(root: Path, rel_path: str, content: str) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class IssueAuditorTests(unittest.TestCase):
    def test_collect_agent_evidence_classifies_complete_partial_and_docs_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_root = config.REPO_ROOT
            config.REPO_ROOT = root
            try:
                write_file(
                    root,
                    "ryemyster/ShaleYeah/agents/geologist/src/agent/index.ts",
                    "export const manifest = {}; export async function runTask() { return {}; }\n",
                )
                write_file(
                    root,
                    "ryemyster/ShaleYeah/agents/geologist/tests/agent.test.ts",
                    "test('agent', () => expect(true).toBe(true));\n",
                )
                write_file(
                    root,
                    "ryemyster/ShaleYeah/agents/geologist/tests/mcp-client.test.ts",
                    "test('mcp client', () => expect(true).toBe(true));\n",
                )

                write_file(
                    root,
                    "ryemyster/ShaleYeah/agents/economist/src/agent/index.ts",
                    "export async function runTask() { return {}; }\n",
                )
                write_file(
                    root,
                    "ryemyster/ShaleYeah/agents/economist/tests/agent.test.ts",
                    "test('agent', () => expect(true).toBe(true));\n",
                )

                write_file(
                    root,
                    "ryemyster/ShaleYeah/agents/planner/README.md",
                    "# Planner\nstub migration notes only\n",
                )

                evidence = collect_agent_evidence(["ryemyster/ShaleYeah/agents"])
                by_agent = {row["agent"]: row for row in evidence}

                self.assertEqual(by_agent["ryemyster/ShaleYeah/agents/geologist"]["status"], "complete")
                self.assertEqual(by_agent["ryemyster/ShaleYeah/agents/geologist"]["recommendation"], "close")

                economist = by_agent["ryemyster/ShaleYeah/agents/economist"]
                self.assertEqual(economist["status"], "partial")
                self.assertIn("tests/mcp-client.test.ts", economist["missing_evidence"])
                self.assertEqual(
                    economist["recommendation"],
                    "keep open; missing tests/mcp-client.test.ts",
                )

                planner = by_agent["ryemyster/ShaleYeah/agents/planner"]
                self.assertEqual(planner["status"], "stub/docs only")
                self.assertEqual(planner["recommendation"], "keep open; implementation and tests are missing")
            finally:
                config.REPO_ROOT = original_root

    def test_findings_from_evidence_uses_rule_based_recommendations(self):
        evidence = [
            {
                "agent": "ryemyster/ShaleYeah/agents/geologist",
                "implementation_file": "ryemyster/ShaleYeah/agents/geologist/src/agent/index.ts",
                "agent_test_file": "ryemyster/ShaleYeah/agents/geologist/tests/agent.test.ts",
                "mcp_client_test_file": "ryemyster/ShaleYeah/agents/geologist/tests/mcp-client.test.ts",
                "readme_file": None,
                "missing_evidence": [],
                "status": "complete",
                "recommendation": "close",
            }
        ]

        findings = findings_from_evidence(evidence, [363])

        self.assertEqual(findings[0]["issue"], 363)
        self.assertEqual(findings[0]["status"], "complete")
        self.assertEqual(findings[0]["recommendation"], "close")
        self.assertEqual(
            findings[0]["evidence_files"],
            [
                "ryemyster/ShaleYeah/agents/geologist/src/agent/index.ts",
                "ryemyster/ShaleYeah/agents/geologist/tests/agent.test.ts",
                "ryemyster/ShaleYeah/agents/geologist/tests/mcp-client.test.ts",
            ],
        )

    def test_insufficient_evidence_findings_do_not_close(self):
        findings = insufficient_evidence_findings([123])

        self.assertEqual(findings[0]["issue"], 123)
        self.assertEqual(findings[0]["status"], "insufficient evidence")
        self.assertEqual(
            findings[0]["recommendation"],
            "keep open; no concrete agent package evidence found in requested paths",
        )


if __name__ == "__main__":
    unittest.main()
