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
    "run_in_background: false",
    "ONE message",
]


def test_playbook_mentions_every_command() -> None:
    text = PLAYBOOK.read_text(encoding="utf-8")
    missing = [needle for needle in REQUIRED_TEXT if needle not in text]
    assert not missing, f"playbook.md is missing: {missing}"


def test_playbook_logs_unfilled_pending_fills_after_init() -> None:
    """I7: fills still pending after init are logged and reported in the final reply."""
    text = PLAYBOOK.read_text(encoding="utf-8")
    for needle in (
        "fills.missing_price",
        "fills.still_pending",
        'ma log "pending fills not filled: <symbols>"',
    ):
        assert needle in text, needle


def test_playbook_state_init_rerun_is_safe() -> None:
    text = PLAYBOOK.read_text(encoding="utf-8")
    assert '"resumed": true' in text
