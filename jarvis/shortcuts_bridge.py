"""
Shortcuts bridge - local Siri/Shortcuts-friendly HTTP routes.

These helpers are intentionally small and auth-first: Shortcuts can call the
FastAPI server over localhost or the tailnet, but every route requires a bearer
token supplied by the chair through environment or config.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .resource_conductor import ResourceConductor, merge_resources_line

DEFAULT_BRIEFS_DIR = "~/Documents/ai/obsidian-vault/dvc/jarvis/briefs"


def install_shortcuts_routes(
    app: FastAPI,
    config: dict,
    ask_jarvis: Callable[[str], Awaitable[str]],
    playbooks,
) -> None:
    @app.post("/ask")
    async def ask(request: Request):
        _require_bearer(request, config)
        question = await _question_from_request(request)
        if not question:
            raise HTTPException(status_code=400, detail="question is required")
        reply = limit_voice_reply(await ask_jarvis(question))
        return JSONResponse({"reply": reply})

    @app.get("/brief/today", response_class=PlainTextResponse)
    async def brief_today(request: Request):
        _require_bearer(request, config)
        brief = latest_brief_text(config)
        if not brief:
            raise HTTPException(status_code=404, detail="no strategist brief found")
        return PlainTextResponse(brief)

    @app.get("/pm/next")
    async def pm_next(request: Request):
        _require_bearer(request, config)
        return JSONResponse({"pm": _pm_state(playbooks)})

    @app.get("/pm/backlog-review")
    async def pm_backlog_review(request: Request):
        _require_bearer(request, config)
        return JSONResponse({"backlog_curator": _backlog_curator_state(playbooks)})

    @app.post("/handup/ack")
    async def handup_ack(request: Request):
        _require_bearer(request, config)
        action = await _ack_action_from_request(request)
        if action in {"later", "snooze"}:
            result = await playbooks.handle_chat("later")
        elif action in {"approve", "approved", "go_ahead", "go ahead"}:
            result = await playbooks.handle_chat("go ahead")
        else:
            result = {
                "status": "acknowledged",
                "reply": "Acknowledged.",
                "active": _active_hand_up(playbooks),
            }
        if result is None:
            result = {"status": "idle", "reply": "No active Jarvis hand-up."}
        return JSONResponse(result)


def latest_brief_text(config: dict) -> str:
    sc = (config or {}).get("shortcuts", {}) or {}
    briefs_dir = _expand_path(str(sc.get("briefs_dir") or DEFAULT_BRIEFS_DIR))
    if not briefs_dir.exists():
        return ""
    try:
        latest = sorted(briefs_dir.glob("*-strategist-brief.md"))[-1]
    except IndexError:
        return ""
    try:
        brief = latest.read_text(encoding="utf-8")
    except Exception:
        return ""
    sc = (config or {}).get("shortcuts", {}) or {}
    if sc.get("include_resources_line", True):
        line = ResourceConductor(config).resources_line()
        return merge_resources_line(brief, line)
    return brief


def limit_voice_reply(text: str, max_sentences: int = 3) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return ""
    sentences = re.findall(r"[^.!?]+[.!?]?", text)
    clipped = " ".join(sentence.strip() for sentence in sentences[:max_sentences]).strip()
    return clipped or text


def token_configured(config: dict) -> bool:
    return bool(_token(config))


def _require_bearer(request: Request, config: dict) -> None:
    token = _token(config)
    header = request.headers.get("authorization", "")
    if not token or header != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="unauthorized")


def _token(config: dict) -> str:
    sc = (config or {}).get("shortcuts", {}) or {}
    return (
        os.environ.get("JARVIS_SHORTCUTS_TOKEN")
        or str(sc.get("bearer_token") or "")
    ).strip()


async def _question_from_request(request: Request) -> str:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        if isinstance(body, dict):
            return str(body.get("question") or body.get("message") or body.get("text") or "").strip()
    return (await request.body()).decode("utf-8", "replace").strip()


async def _ack_action_from_request(request: Request) -> str:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        if isinstance(body, dict):
            return str(body.get("action") or body.get("ack") or "ack").strip().lower()
    text = (await request.body()).decode("utf-8", "replace").strip().lower()
    return text or "ack"


def _active_hand_up(playbooks) -> dict | None:
    try:
        return (playbooks.state.load() or {}).get("active")
    except Exception:
        return None


def _pm_state(playbooks) -> dict:
    try:
        return (playbooks.state.load() or {}).get("pm") or {"status": "unknown"}
    except Exception:
        return {"status": "unavailable"}


def _backlog_curator_state(playbooks) -> dict:
    try:
        return (playbooks.state.load() or {}).get("backlog_curator") or {"status": "unknown"}
    except Exception:
        return {"status": "unavailable"}


def _expand_path(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))
