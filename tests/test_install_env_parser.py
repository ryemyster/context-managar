import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def parse_line(line: str) -> str:
    script = (
        "source scripts/lib/env.sh; "
        "parse_dotenv_line \"$DOTENV_LINE\""
    )
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env={"DOTENV_LINE": line},
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def test_parse_dotenv_line_strips_matching_quotes_from_output_dir():
    parsed = parse_line(
        'OUTPUT_DIR="/Users/rmcdonald/Library/Application Support/context-store/artifacts"'
    )

    assert parsed == "OUTPUT_DIR=/Users/rmcdonald/Library/Application Support/context-store/artifacts"


def test_parse_dotenv_line_strips_unquoted_inline_comment():
    parsed = parse_line("LOG_LEVEL=DEBUG     # TRACE | DEBUG | INFO")

    assert parsed == "LOG_LEVEL=DEBUG"


def test_parse_dotenv_line_preserves_hash_inside_quoted_value():
    parsed = parse_line('TOKEN="abc#123"')

    assert parsed == "TOKEN=abc#123"
