"""Yennefer tool bridge helpers."""

import asyncio
from pathlib import Path

from jarvis import tools


def run(coro):
    return asyncio.run(coro)


def test_linear_counts_uses_local_bridge(monkeypatch):
    calls = []

    async def fake_bridge(method, path, cfg, *, params=None, payload=None):
        calls.append((method, path, params, payload, cfg))
        return {
            "scope": {"states": ["Todo", "In Review"], "active_only": True},
            "counts": {"Todo": 7, "In Review": 3},
        }

    monkeypatch.setattr(tools, "_bridge_request", fake_bridge)

    result = run(tools.execute("linear_counts", {"states": "Todo,In Review"}, {"playbooks": {}}))

    assert "Todo: 7" in result
    assert "In Review: 3" in result
    assert calls[0][0:2] == ("GET", "/linear/counts")
    assert calls[0][2] == {"states": "Todo,In Review"}


def test_linear_write_tools_are_confirmation_gated():
    assert tools.is_safe("linear_counts") is True
    assert tools.is_safe("screen_context") is True
    assert tools.is_safe("linear_comment") is False
    assert tools.is_safe("linear_move_issue") is False


def test_linear_move_issue_sends_approval_to_bridge(monkeypatch):
    calls = []

    async def fake_bridge(method, path, cfg, *, params=None, payload=None):
        calls.append((method, path, params, payload))
        return {"issue": {"identifier": "DAR-80", "state": "In Progress"}}

    monkeypatch.setattr(tools, "_bridge_request", fake_bridge)

    result = run(
        tools.execute(
            "linear_move_issue",
            {
                "issue_id": "DAR-80",
                "state": "In Progress",
                "reason": "Selected by zero-ticket loop",
            },
            {"playbooks": {}},
        )
    )

    assert "Moved DAR-80 to In Progress" in result
    assert calls[0][0:2] == ("POST", "/linear/issues/DAR-80/state")
    assert calls[0][3]["approved_by"] == "Hank via Yennefer UI confirmation"
    assert calls[0][3]["reason"] == "Selected by zero-ticket loop"


def test_screen_context_reports_foreground_and_saves_capture(monkeypatch, tmp_path):
    async def fake_run(cmd, timeout=60.0, cwd=None):
        if isinstance(cmd, list) and cmd and cmd[0] == "screencapture":
            Path(cmd[-1]).write_bytes(b"png")
            return "(no output)"
        return "Ghostty\nCodex - Yennefer"

    monkeypatch.setattr(tools, "_run", fake_run)

    result = run(
        tools.execute(
            "screen_context",
            {"include_screenshot": True, "directory": str(tmp_path)},
            {},
        )
    )

    assert "Front app: Ghostty" in result
    assert "Window title: Codex - Yennefer" in result
    assert "Screenshot saved:" in result
    assert list(tmp_path.glob("screen-*.png"))
