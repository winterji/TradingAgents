"""The orchestrator playbook names every command and the sub-agent wrapper."""

from pathlib import Path

import desk_runner

PLAYBOOK = Path(desk_runner.__file__).parent / "playbook.md"

REQUIRED_TEXT = [
    "ma init",
    "ma memory pending",
    "ma memory settle",
    "ma state init",
    "ma next",
    "ma prompt",
    "ma apply",
    "--freetext",
    "ma tool",
    "ma finalize",
    "ma log",
    'model: "sonnet"',
    'model: "opus"',
    "## Sub-agent wrapper",
]


def test_playbook_mentions_every_command() -> None:
    text = PLAYBOOK.read_text(encoding="utf-8")
    missing = [needle for needle in REQUIRED_TEXT if needle not in text]
    assert not missing, f"playbook.md is missing: {missing}"
