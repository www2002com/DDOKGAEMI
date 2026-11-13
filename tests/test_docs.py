"""CLAUDE.md와 AGENTS.md는 같은 내용이어야 한다 (Claude Code·Codex 공용 지시)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_agent_instruction_files_in_sync():
    assert (ROOT / "CLAUDE.md").read_text(encoding="utf-8") == (ROOT / "AGENTS.md").read_text(encoding="utf-8")


def test_fable_guide_blocks_present():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for heading in ("# Finishing work", "You are the owner and long-term maintainer", "Before you start, say in a line",
                    "Before reporting progress, audit each claim", "# Communicating clearly"):
        assert heading in text
