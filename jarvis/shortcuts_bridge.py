"""
Shortcuts bridge - local Siri/Shortcuts-friendly HTTP routes.

These helpers are intentionally small and auth-first: Shortcuts can call the
FastAPI server over localhost or the tailnet, but every route requires a bearer
token supplied by the chair through environment or config.
"""

from __future__ import annotations

import os
import re
from ipaddress import ip_address
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

DEFAULT_BRIEFS_DIR = "~/Documents/ai/obsidian-vault/dvc/yennefer/briefs"


def install_shortcuts_routes(
    app: FastAPI,
    config: dict,
    ask_yennefer: Callable[[str], Awaitable[str]],
    playbooks,
) -> None:
    @app.post("/ask")
    async def ask(request: Request):
        _require_bearer(request, config)
        question = await _question_from_request(request)
        if not question:
            raise HTTPException(status_code=400, detail="question is required")
        reply = limit_voice_reply(await ask_yennefer(question))
        return JSONResponse({"reply": reply})

    @app.get("/ask/local", response_class=PlainTextResponse)
    async def ask_local(request: Request):
        _require_bearer_or_loopback(request, config)
        question = str(request.query_params.get("question") or request.query_params.get("q") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="question is required")
        reply = limit_voice_reply(await ask_yennefer(question))
        return PlainTextResponse(reply or "Yennefer did not return an answer.")

    @app.get("/brief/today", response_class=PlainTextResponse)
    async def brief_today(request: Request):
        _require_bearer(request, config)
        brief = latest_brief_text(config)
        if not brief:
            raise HTTPException(status_code=404, detail="no strategist brief found")
        return PlainTextResponse(brief)

    @app.get("/brief/focus", response_class=PlainTextResponse)
    async def brief_focus(request: Request):
        _require_bearer_or_loopback(request, config)
        reply = brief_focus_reply(config, "focus")
        if not reply:
            raise HTTPException(status_code=404, detail="no strategist focus found")
        return PlainTextResponse(reply)

    @app.get("/pm/next")
    async def pm_next(request: Request):
        _require_bearer(request, config)
        return JSONResponse({"pm": _pm_state(playbooks)})

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
            result = {"status": "idle", "reply": "No active Yennefer hand-up."}
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
        return latest.read_text(encoding="utf-8")
    except Exception:
        return ""


def brief_focus_reply(config: dict, question: str) -> str:
    if not _looks_like_focus_question(question):
        return ""
    one_thing = _section_text(latest_brief_text(config), "THE ONE THING")
    if not one_thing:
        return ""
    return limit_voice_reply(one_thing, max_sentences=1)


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
    if _bearer_authorized(request, config):
        return
    raise HTTPException(status_code=401, detail="unauthorized")


def _require_bearer_or_loopback(request: Request, config: dict) -> None:
    if _bearer_authorized(request, config) or _loopback_shortcut_allowed(request, config):
        return
    raise HTTPException(status_code=401, detail="unauthorized")


def _bearer_authorized(request: Request, config: dict) -> bool:
    token = _token(config)
    header = request.headers.get("authorization", "")
    return bool(token) and header == f"Bearer {token}"


def _token(config: dict) -> str:
    sc = (config or {}).get("shortcuts", {}) or {}
    return (
        os.environ.get("YENNEFER_SHORTCUTS_TOKEN")
        or str(sc.get("bearer_token") or "")
    ).strip()


def _loopback_shortcut_allowed(request: Request, config: dict) -> bool:
    sc = (config or {}).get("shortcuts", {}) or {}
    if sc.get("allow_unauthenticated_loopback") is False:
        return False
    host = getattr(request.client, "host", "") if request.client else ""
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


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
        data = playbooks.state.load() or {}
        if data.get("pm"):
            return data["pm"]
        if data.get("review"):
            return data["review"]
        last_error = str(data.get("last_error") or "").strip()
        if last_error:
            return {
                "status": "linear_unavailable" if "Linear unavailable" in last_error else "unavailable",
                "error": last_error,
            }
        return {"status": "unknown"}
    except Exception:
        return {"status": "unavailable"}


def _expand_path(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def _looks_like_focus_question(question: str) -> bool:
    text = re.sub(r"\s+", " ", (question or "").lower()).strip()
    if not text:
        return False
    return any(
        needle in text
        for needle in (
            "focus",
            "what should i do",
            "what should i work on",
            "next priority",
            "prioritize",
            "priority",
            "brief me",
            "status",
        )
    )


def _section_text(markdown: str, heading: str) -> str:
    if not markdown:
        return ""
    pattern = rf"(?ms)^##\s+{re.escape(heading)}\s*(.+?)(?:^##\s+|\Z)"
    match = re.search(pattern, markdown)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()
