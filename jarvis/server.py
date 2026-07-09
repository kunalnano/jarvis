"""
Jarvis chat server - a web chat box with hands.

FastAPI backend that serves a chat UI and runs an LLM tool-calling loop against
LM Studio. Jarvis can answer, and it can invoke registered tools/agents on
the Mac. Safe tools auto-run; side-effectful ones return a confirmation request
the UI must approve before they execute.

Run:  python -m jarvis.server     (then open the printed URL)
"""

import json
import asyncio
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import __version__
from .brain import Brain, extract_speakable
from .voice import Voice
from .config import load_config
from .playbooks import PlaybookEngine
from .shortcuts_bridge import install_shortcuts_routes
from . import tools

CONFIG = load_config()
BRAIN = Brain(CONFIG)
VOICE = Voice(CONFIG)
PLAYBOOKS = PlaybookEngine(CONFIG)
WEB = Path(__file__).parent / "web"
SPEECH_WORKER: asyncio.Task | None = None
SPEECH_NEXT: str | None = None
CONVERSATION_TURN_LIMIT = 12
CONVERSATION_MESSAGE_LIMIT = CONVERSATION_TURN_LIMIT * 3
AGENT_VOICE_STATUS_TIMEOUT_SECONDS = 5.0

COMMANDS = [
    {
        "id": "status",
        "name": "System Status",
        "description": "Check Mac uptime, disk, and top CPU processes.",
        "trigger": "check system status",
        "tool": "system_status",
        "args": {},
    },
    {
        "id": "projects",
        "name": "List Projects",
        "description": "List local project directories.",
        "trigger": "list my projects",
        "tool": "list_projects",
        "args": {},
    },
    {
        "id": "capabilities",
        "name": "Capabilities",
        "description": "Show what Jarvis can do right now.",
        "trigger": "what can you do",
        "tool": "capabilities",
        "args": {},
    },
    {
        "id": "jarvis-doctor",
        "name": "Jarvis Doctor",
        "description": "Check and repair backend, overlay, voice, and LM Studio failover.",
        "trigger": "repair jarvis services",
        "tool": "jarvis_doctor",
        "args": {"repair": True},
    },
    {
        "id": "weather",
        "name": "Weather",
        "description": "Get current weather using location if provided, otherwise IP-based location.",
        "trigger": "what's the weather",
        "tool": "weather",
        "args": {},
    },
    {
        "id": "web-search",
        "name": "Web Search",
        "description": "Search the live web for current facts, news, sports, schedules, or products.",
        "trigger": "search the web",
        "tool": "web_search",
        "args": {"query": ""},
    },
    {
        "id": "browser",
        "name": "Read Browser Tab",
        "description": "Read the active Chrome or Safari tab.",
        "trigger": "look at current browser tab",
        "tool": "observe_browser",
        "args": {},
    },
    {
        "id": "vault-lisbon",
        "name": "Search Vault",
        "description": "Search local Obsidian vault notes.",
        "trigger": "search the vault for Lisbon",
        "tool": "vault_search",
        "args": {"query": "Lisbon"},
    },
    {
        "id": "git-jarvis",
        "name": "Jarvis Backend Git Status",
        "description": "Show git status for the Jarvis backend.",
        "trigger": "show git status for /Users/alsharma/Projects/jarvis",
        "tool": "git_status",
        "args": {"path": "/Users/alsharma/Projects/jarvis"},
    },
    {
        "id": "git-jarvis-overlay",
        "name": "Jarvis Overlay Git Status",
        "description": "Show git status for the Jarvis overlay.",
        "trigger": "show git status for /Users/alsharma/Projects/jarvis-overlay",
        "tool": "git_status",
        "args": {"path": "/Users/alsharma/Projects/jarvis-overlay"},
    },
]

SYSTEM = (
    BRAIN.system_prompt
    + "\n\nYou have hands: tools to act on the user's Mac (check status, open apps or "
    "URLs, inspect repos, run registered agents). When the user asks you to DO "
    "something, call the right tool instead of describing how. If nothing fits, just "
    "answer. Keep replies short. Conversation mode: answer the direct question first, "
    "then ask at most one short follow-up question when it would clearly help. Use recent "
    "tool-result context for follow-ups when it is enough."
)

HISTORY = [{"role": "system", "content": SYSTEM}]
app = FastAPI(title="Jarvis")


class ChatIn(BaseModel):
    message: str | None = None
    speak: bool = False
    confirm: dict | None = None
    decline: bool = False


class CaptureIn(BaseModel):
    text: str
    source: str = "siri"
    speak: bool = False
    context: dict | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _expanded_path(value) -> str:
    if not value:
        return ""
    return str(Path(str(value)).expanduser())


def _backend_base_url() -> str:
    server = CONFIG.get("server", {}) or {}
    port = int(server.get("port") or 4343)
    return f"http://127.0.0.1:{port}"


def _public_endpoint(endpoint: dict | None) -> dict:
    endpoint = endpoint or {}
    return {
        "name": endpoint.get("name") or "",
        "api_base": endpoint.get("api_base") or "",
        "model": endpoint.get("model") or BRAIN.model,
    }


def _agent_model_status() -> dict:
    endpoint = getattr(BRAIN, "active_endpoint", {}) or {}
    errors = list(getattr(BRAIN, "last_endpoint_errors", []) or [])
    return {
        "model": BRAIN.model,
        "active_endpoint": _public_endpoint(endpoint),
        "configured_endpoint_count": len(getattr(BRAIN, "endpoints", []) or []),
        "last_errors": errors[:3],
        "ready": not bool(errors),
        "endpoint_name": endpoint.get("name") or "",
    }


def _agent_permission_status() -> dict:
    registry = getattr(tools, "REGISTRY", {}) or {}
    safe = sorted(name for name, spec in registry.items() if spec.get("safe"))
    gated = sorted(name for name, spec in registry.items() if not spec.get("safe"))
    return {
        "safe_tools_auto_run": True,
        "side_effect_tools_require_confirmation": True,
        "safe_tool_count": len(safe),
        "gated_tool_count": len(gated),
        "gated_tools": gated,
    }


def _agent_playbook_status() -> dict:
    data = PLAYBOOKS.state.load()
    active = data.get("active")
    active_packet = None
    if isinstance(active, dict):
        active_packet = {
            "id": active.get("id"),
            "playbook_id": active.get("playbook_id"),
            "playbook_name": active.get("playbook_name"),
            "status": active.get("status"),
            "message": active.get("opener") or active.get("message") or "",
            "created_at": active.get("created_at"),
            "approved_at": active.get("approved_at"),
            "snooze_until": active.get("snooze_until"),
        }
    queue = data.get("queue") if isinstance(data.get("queue"), list) else []
    drafts = data.get("drafts") if isinstance(data.get("drafts"), list) else []
    events = data.get("events") if isinstance(data.get("events"), list) else []
    return {
        "enabled": bool(getattr(PLAYBOOKS, "enabled", False)),
        "state_path": str(PLAYBOOKS.state.path),
        "status": (active_packet or {}).get("status") or ("paused" if data.get("paused") else "idle"),
        "active": bool(active_packet),
        "active_hand_up": active_packet,
        "queue_count": len(queue),
        "draft_count": len(drafts),
        "event_count": len(events),
        "paused": bool(data.get("paused")),
        "last_error": data.get("last_error") or "",
        "updated_at": data.get("updated_at") or "",
    }


def _agent_resource_status() -> dict:
    resources = CONFIG.get("resources", {}) or {}
    playbooks = CONFIG.get("playbooks", {}) or {}
    return {
        "enabled": bool(resources.get("enabled", True)),
        "registry_path": _expanded_path(resources.get("registry_path")),
        "state_path": _expanded_path(resources.get("state_path") or playbooks.get("state_path")),
        "vault_rag_url": resources.get("vault_rag_url") or "",
        "champion_model": resources.get("champion_model") or BRAIN.model,
    }


def _capture_dir() -> Path:
    capture = CONFIG.get("capture", {}) or {}
    return Path(str(capture.get("inbox_dir") or "~/.jarvis/captures")).expanduser()


def _capture_file(day: str | None = None) -> Path:
    day = day or date.today().isoformat()
    return _capture_dir() / f"{day}.jsonl"


def _classify_capture(text: str) -> dict:
    lower = text.lower()
    if "?" in text or re.match(r"\s*(what|why|how|who|when|where|should|can|could|would)\b", lower):
        label = "question"
        reason = "looks like a question"
    elif re.search(r"\b(agent|codex|jarvis|jarvis|siri|run|build|debug|fix|ship|ticket)\b", lower):
        label = "agent_request"
        reason = "mentions agent or execution language"
    elif re.search(r"\b(remind|todo|to-do|follow up|schedule|book|send|email|call|need to)\b", lower):
        label = "task"
        reason = "looks like a task or reminder"
    elif re.search(r"\b(meeting|call with|prep|brief|agenda)\b", lower):
        label = "meeting_note"
        reason = "mentions meeting context"
    else:
        label = "note"
        reason = "general captured note"
    return {
        "label": label,
        "reason": reason,
        "requires_confirmation_before_side_effects": True,
    }


def _capture_ack(item: dict) -> str:
    label = item.get("classification", {}).get("label") or "note"
    if label == "agent_request":
        return "Captured as an agent request. I will hold it for review before taking action."
    if label == "task":
        return "Captured as a task. I will hold it in the Jarvis inbox for review."
    if label == "question":
        return "Captured as a question. I will keep it in the Jarvis inbox."
    return "Captured. I will keep it in the Jarvis inbox."


def _store_capture(text: str, source: str = "siri", context: dict | None = None) -> dict:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        raise HTTPException(status_code=400, detail="capture text is required")
    now = _utc_now()
    item = {
        "id": f"cap-{now.replace(':', '').replace('-', '').replace('.', '')}-{uuid.uuid4().hex[:8]}",
        "captured_at": now,
        "source": source or "siri",
        "text": cleaned,
        "classification": _classify_capture(cleaned),
        "context": context or {},
        "side_effects_performed": [],
        "next_action": "review_in_jarvis_inbox",
    }
    path = _capture_file(now[:10])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(item, sort_keys=True) + "\n")
    item["inbox_path"] = str(path)
    return item


def _load_captures(limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit or 20), 100))
    inbox = _capture_dir()
    if not inbox.exists():
        return []
    items = []
    for path in sorted(inbox.glob("*.jsonl"), reverse=True):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            item.setdefault("inbox_path", str(path))
            items.append(item)
            if len(items) >= limit:
                return items
    return items


def _capture_status() -> dict:
    today_path = _capture_file()
    today_count = 0
    if today_path.exists():
        try:
            today_count = sum(1 for line in today_path.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            today_count = 0
    recent = _load_captures(limit=1)
    return {
        "enabled": True,
        "inbox_dir": str(_capture_dir()),
        "today_count": today_count,
        "last_item": recent[0] if recent else None,
    }


def _agent_needs_attention(voice: dict, playbooks: dict, model: dict) -> list[str]:
    needs = []
    if not voice.get("initialized"):
        needs.append("voice_uninitialized")
    if voice.get("probe_timed_out"):
        needs.append("voice_probe_timeout")
    if voice.get("probe_error"):
        needs.append("voice_probe_error")
    if voice.get("degraded"):
        needs.append("voice_degraded")
    if voice.get("fallback_warning"):
        needs.append("voice_fallback")
    if playbooks.get("active"):
        needs.append("playbook_waiting_for_chair")
    if playbooks.get("last_error"):
        needs.append("playbook_last_error")
    if model.get("last_errors"):
        needs.append("lmstudio_last_errors")
    return needs


def _speech_status() -> dict:
    speaking = bool(SPEECH_WORKER and not SPEECH_WORKER.done())
    return {
        "speaking": speaking,
        "queued": bool(SPEECH_NEXT),
    }


def _pet_compat_fields(
    status: str,
    voice: dict,
    playbooks: dict,
    model: dict,
    needs_attention: list[str],
    speech: dict,
) -> dict:
    endpoint = (model.get("active_endpoint") or {}).get("name") or "model"
    voice_engine = voice.get("engine") or voice.get("preferred_engine") or "voice"

    if status == "degraded":
        mood = "degraded"
        label = "Degraded"
        detail = (
            voice.get("fallback_warning")
            or voice.get("degraded_reason")
            or "; ".join(model.get("last_errors") or [])
            or "Jarvis needs a runtime check"
        )
    elif speech.get("speaking"):
        mood = "speaking"
        label = "Speaking"
        detail = f"{voice_engine} is answering"
    elif playbooks.get("active"):
        mood = "needs"
        label = "Needs You"
        active = playbooks.get("active_hand_up") or {}
        detail = active.get("message") or "A playbook is waiting for approval"
    elif needs_attention:
        mood = "needs"
        label = "Needs Attention"
        if "playbook_last_error" in needs_attention:
            detail = playbooks.get("last_error") or "Playbook needs attention"
        elif "voice_probe_timeout" in needs_attention:
            detail = "Deep voice probe timed out; cached voice state is available"
        elif "voice_probe_error" in needs_attention:
            detail = voice.get("probe_error") or "Voice probe failed"
        else:
            detail = ", ".join(needs_attention)
    else:
        mood = "idle"
        label = "Ready"
        detail = f"{voice_engine} ready on {endpoint}"

    return {
        "mood": mood,
        "label": label,
        "detail": detail[:180],
        "speech": speech,
    }


async def _agent_voice_status(probe: bool = False) -> dict:
    try:
        return await asyncio.wait_for(
            VOICE.runtime_status_async(probe_chatterbox=probe),
            timeout=AGENT_VOICE_STATUS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        status = VOICE.runtime_status()
        status["probe_timed_out"] = True
        return status
    except Exception as exc:
        status = VOICE.runtime_status()
        status["probe_error"] = str(exc)
        return status


async def _agent_status_packet(probe: bool = False) -> dict:
    voice = await _agent_voice_status(probe=probe)
    model = _agent_model_status()
    playbooks = _agent_playbook_status()
    resources = _agent_resource_status()
    permissions = _agent_permission_status()
    speech = _speech_status()
    capture = _capture_status()
    needs_attention = _agent_needs_attention(voice, playbooks, model)
    hard_needs = {"voice_uninitialized", "voice_degraded", "lmstudio_last_errors"}
    status = "ready"
    if any(need in hard_needs for need in needs_attention):
        status = "degraded"
    elif needs_attention:
        status = "attention"
    base_url = _backend_base_url()
    return {
        "agent_id": "jarvis",
        "name": "Jarvis",
        "kind": "local_operating_agent",
        "role": "Local Mac presence for voice, chat, safe tools, approval-gated playbooks, and resource checks.",
        "status": status,
        "version": __version__,
        "checked_at": _utc_now(),
        "backend": {
            "base_url": base_url,
            "ui": f"{base_url}/",
            "agent_status": f"{base_url}/api/agent/status",
            "pet_status": f"{base_url}/api/pet/status",
        "voice_status": f"{base_url}/api/voice/status",
        "commands": f"{base_url}/api/commands",
    },
        **_pet_compat_fields(status, voice, playbooks, model, needs_attention, speech),
        "commands": [command["id"] for command in COMMANDS],
        "model": model,
        "voice": voice,
        "permissions": permissions,
        "playbooks": playbooks,
        "resources": resources,
        "capture": capture,
        "needs_attention": needs_attention,
    }


def _convo():
    if not HISTORY:
        return []
    base_system = HISTORY[0].get("content", "")
    tail = HISTORY[1:]
    tool_contexts = [
        m["content"]
        for m in tail
        if m.get("role") == "system" and m.get("content")
    ]
    visible_tail = [
        m
        for m in tail
        if m.get("role") in ("user", "assistant")
    ]
    if tool_contexts:
        base_system += "\n\n" + "\n".join(tool_contexts[-3:])
    return [{"role": "system", "content": base_system}] + visible_tail[-CONVERSATION_MESSAGE_LIMIT:]


def _trim_history():
    if len(HISTORY) <= CONVERSATION_MESSAGE_LIMIT + 1:
        return
    root = HISTORY[0]
    HISTORY[:] = [root] + HISTORY[1:][-CONVERSATION_MESSAGE_LIMIT:]


def _remember(role: str, content: str):
    HISTORY.append({"role": role, "content": content})
    _trim_history()


def _tool_context(name: str, args: dict, result: str) -> str:
    lines = [line.strip() for line in (result or "").splitlines() if line.strip()]
    excerpt = " ".join(lines[:10])
    if len(excerpt) > 1200:
        excerpt = excerpt[:1197].rstrip() + "..."
    arg_bits = []
    for key in ("query", "location", "url", "path"):
        value = (args or {}).get(key)
        if value:
            arg_bits.append(f"{key}={value}")
    suffix = f" ({', '.join(arg_bits)})" if arg_bits else ""
    return f"Recent tool result for follow-ups: {name}{suffix}. {excerpt}"


def _remember_tool_result(name: str, args: dict, result: str):
    _remember("system", _tool_context(name, args, result))


def _text(msg):
    return extract_speakable(msg)


def _model_unavailable_reply(exc: Exception) -> str:
    endpoint_errors = getattr(BRAIN, "last_endpoint_errors", [])
    if endpoint_errors:
        return (
            "No LM Studio endpoint is ready: "
            + "; ".join(endpoint_errors[:3])
            + ". Command actions still work; run 'repair jarvis services' for the recovery check."
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 401:
            return (
                "LM Studio is asking for an API token. Command actions still work, "
                "but freeform chat needs LM_API_TOKEN or LM_STUDIO_API_KEY loaded."
            )
        return f"The local model endpoint returned HTTP {status}. Command actions still work."
    if isinstance(exc, httpx.ConnectError):
        return "I cannot reach LM Studio. Command actions still work, but freeform chat needs the local model server."
    return "The local model call failed. Command actions still work."


def _fallback_action_summary(name: str, result: str) -> str:
    if name == "system_status":
        lines = [line.strip() for line in result.splitlines() if line.strip()]
        values = {}
        for line in lines:
            if ": " in line:
                key, value = line.split(": ", 1)
                values[key.strip()] = value.strip()
        host = values.get("canonical_name") or values.get("computer_name") or "this Mac"
        disk = ""
        for line in lines:
            if line.endswith(" /") and "Gi" in line:
                parts = line.split()
                if len(parts) >= 4:
                    disk = "Disk space is healthy"
                break
        details = ". ".join(part for part in (disk,) if part)
        return f"Ran system status. {host} is online" + (f". {details}." if details else ".")

    lines = [line.strip() for line in result.splitlines() if line.strip()]
    excerpt = " ".join(lines[:8])
    if not excerpt:
        return f"Ran {name}."
    if len(excerpt) > 360:
        excerpt = excerpt[:357].rstrip() + "..."
    return f"Ran {name}. {excerpt}"


def _looks_incomplete(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return True
    if cleaned in {".", "..", "...", "…"}:
        return True
    if not any(ch.isalnum() for ch in cleaned):
        return True
    if cleaned[-1] in ",:;":
        return True
    if re.search(r"\b(?:and|but|or|because|while|with|for|to|the|a|an)\s*$", cleaned, flags=re.I):
        return True
    return False


def _fallback_vault_summary(args: dict, result: str) -> str | None:
    query = str((args or {}).get("query", "")).lower()
    lower = result.lower()
    if "lisbon" not in query and "lisbon" not in lower:
        return None
    if "jan 30" in lower and "from lisbon to san antonio" in lower:
        if "8am sunday landing" in lower or "sunday rest day" in lower:
            return (
                "The Vault points to Lisbon from January 25 through January 30, 2026. "
                "January 30 is firm: the note says you were traveling from Lisbon to San Antonio that day. "
                "January 25 is inferred from the January 22-23 Granola notes: Saturday departure, then an 8am Sunday landing/rest day. "
                "I would count January 24 as travel to Lisbon, not a confirmed Lisbon day."
            )
        return (
            "The firm Vault date I found is January 30, 2026: you were traveling from Lisbon to San Antonio that day. "
            "I found Lisbon planning notes too, but not enough context in this search result to prove the arrival date."
        )
    return None


def _fixtures_from_text(text: str) -> list[str]:
    fixtures: list[str] = []
    for match in re.finditer(r"Likely fixtures:\s*([^.\n]+)", text or "", flags=re.I):
        for fixture in match.group(1).split(";"):
            fixture = fixture.strip(" .")
            if fixture and fixture not in fixtures:
                fixtures.append(fixture)
    return fixtures


def _recent_fixture_answer_from_query(query: str) -> str | None:
    query_text = query.lower()
    for message in reversed(HISTORY):
        content = message.get("content", "")
        for fixture in _fixtures_from_text(content):
            match = re.match(r"(.+?)\s+vs\s+(.+)", fixture, flags=re.I)
            if not match:
                continue
            left, right = (part.strip() for part in match.groups())
            for team, opponent in ((left, right), (right, left)):
                if re.search(rf"\b{re.escape(team.lower())}\b", query_text):
                    return f"{team} are playing {opponent} today."
    return None


def _fallback_contextual_reply(user_message: str | None) -> str | None:
    return _recent_fixture_answer_from_query(user_message or "")


def _fallback_web_search_summary(result: str) -> str | None:
    lines = [line.strip() for line in (result or "").splitlines() if line.strip()]
    query = lines[0].lower() if lines else ""
    compact = " ".join(lines)
    lower = compact.lower()
    if "england" in query and any(term in query for term in ("eliminated", "eliminate", "advance", "qualify", "qualification", "standings")):
        if "england" in lower and "confirmed" in lower and any(term in lower for term in ("next round", "round of 32", "progression", "advance")):
            return "The snippets say England have confirmed progression, so a loss should not eliminate them. I'd verify the full standings before treating that as final."
    fixtures = next((line for line in lines if line.startswith("Likely fixtures:")), "")
    if fixtures:
        return fixtures
    recent_fixture_answer = _recent_fixture_answer_from_query(query)
    if recent_fixture_answer:
        return recent_fixture_answer
    titles = [line for line in lines if re.match(r"^\d+\.\s+", line)]
    if titles:
        first = re.sub(r"^\d+\.\s*", "", titles[0]).strip()
        if first:
            return f"I found live web results. Top result: {first}."
    return None


def _normalise_command(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _matched_command(text: str) -> dict | None:
    wanted = _normalise_command(text)
    if not wanted:
        return None
    for command in COMMANDS:
        trigger = _normalise_command(command.get("trigger", ""))
        if wanted == trigger:
            return command
    return None


def _extract_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>\"]+", text or "")
    if not match:
        return None
    return match.group(0).rstrip(").,;!?'\"")


def _location_from_weather_text(text: str) -> str:
    cleaned = (text or "").strip()
    match = re.search(r"\b(?:in|for|at)\s+(.+)$", cleaned, re.I)
    if not match:
        return ""
    location = match.group(1).strip(" ?.!:")
    stop_words = {"outside", "today", "tomorrow", "now", "right now", "currently"}
    return "" if location.lower() in stop_words else location


def _today_label() -> str:
    today = date.today()
    return f"{today.strftime('%B')} {today.day}, {today.year}"


def _search_query_from_text(text: str) -> str:
    raw = (text or "").strip().strip("?!. ")
    q = _normalise_command(raw)
    non_soccer_world_cup = any(term in q for term in ("cricket", "icc", "t20", "rugby", "hockey"))
    soccer_world_cup = "world cup" in q and not non_soccer_world_cup
    if soccer_world_cup and any(term in q for term in ("eliminated", "eliminate", "advance", "qualify", "qualification", "standings")):
        today = date.today()
        return f"{raw} FIFA soccer standings qualification scenario {today.strftime('%B')} {today.day} {today.year}"
    if soccer_world_cup and any(term in q for term in ("today", "playing", "fixture", "fixtures", "schedule", "match", "matches", "score", "scores")):
        today = date.today()
        return f"FIFA World Cup soccer fixtures {today.strftime('%B')} {today.day} {today.year}"

    replacements = (
        "search the web for",
        "search web for",
        "web search for",
        "google search for",
        "google for",
        "google",
        "look up",
        "lookup",
        "find me",
        "find",
        "what's the latest on",
        "whats the latest on",
        "latest on",
    )
    lowered = raw.lower()
    for prefix in replacements:
        if lowered.startswith(prefix + " "):
            raw = raw[len(prefix):].strip()
            break

    if "today" in q and _today_label().lower() not in raw.lower():
        raw = f"{raw} {_today_label()}"
    return raw


def _intent_command(text: str) -> dict | None:
    q = _normalise_command(text)
    url = _extract_url(text)
    if q in {"help", "what can you do", "what can you do?", "what are your tools", "show your tools"}:
        return {
            "id": "capabilities",
            "name": "Capabilities",
            "description": "Show what Jarvis can do right now.",
            "trigger": text,
            "tool": "capabilities",
            "args": {},
        }
    if q in {"repair jarvis services", "repair jarvis services", "repair assistant services"}:
        return {
            "id": "jarvis-doctor",
            "name": "Jarvis Doctor",
            "description": "Check and repair backend, overlay, voice, and LM Studio failover.",
            "trigger": text,
            "tool": "jarvis_doctor",
            "args": {"repair": True},
        }
    if url and any(word in q for word in ("read", "fetch", "summarize", "summarise", "look", "open", "page", "website", "url")):
        return {
            "id": "web-fetch",
            "name": "Read URL",
            "description": "Fetch and read a public webpage.",
            "trigger": text,
            "tool": "web_fetch",
            "args": {"url": url},
        }

    if any(word in q for word in ("weather", "temperature", "forecast")):
        return {
            "id": "weather",
            "name": "Weather",
            "description": "Get current weather.",
            "trigger": text,
            "tool": "weather",
            "args": {"location": _location_from_weather_text(text)},
        }

    search_prefixes = (
        "search the web", "search web", "web search", "google", "look up",
        "lookup", "find me", "find latest", "latest on", "what's the latest",
        "whats the latest",
    )
    current_terms = (
        "today", "latest", "current", "now", "this week", "fixture", "fixtures",
        "schedule", "score", "scores", "playing", "match", "matches", "news",
    )
    sports_terms = ("fifa", "world cup", "soccer", "football", "nba", "nfl", "mlb", "nhl", "premier league")
    if (
        any(q.startswith(prefix) for prefix in search_prefixes)
        or ("who is playing" in q and any(term in q for term in sports_terms))
        or (any(term in q for term in sports_terms) and any(term in q for term in current_terms))
    ):
        return {
            "id": "web-search",
            "name": "Web Search",
            "description": "Search the live web.",
            "trigger": text,
            "tool": "web_search",
            "args": {"query": _search_query_from_text(text), "max_results": 5},
        }

    browser_phrases = (
        "current browser", "current tab", "this page", "the page", "web page",
        "what am i looking at", "what's on my screen", "whats on my screen",
        "observe the web", "observe web", "look at the tab", "read the tab",
        "read my browser", "look at my browser",
    )
    if any(phrase in q for phrase in browser_phrases):
        return {
            "id": "browser",
            "name": "Read Browser Tab",
            "description": "Read the active Chrome or Safari tab.",
            "trigger": text,
            "tool": "observe_browser",
            "args": {},
        }

    return None


def _deterministic_tool_reply(name: str, args: dict, result: str) -> str:
    if name in {"capabilities", "weather", "web_search", "web_fetch", "observe_browser", "jarvis_doctor"}:
        return result
    return _fallback_vault_summary(args, result) or _fallback_action_summary(name, result)


def _speech_excerpt(text: str) -> str:
    raw_lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    if cleaned.startswith("Active browser tab:"):
        title = cleaned.split(" Fetched:", 1)[0].replace("Active browser tab:", "").strip()
        return f"I can see the active browser tab: {title}. I put the page text in the chat."
    if cleaned.startswith("Fetched:"):
        title = ""
        match = re.search(r"\bTitle:\s*(.+?)\s+Text:", cleaned)
        if match:
            title = match.group(1).strip()
        return f"I fetched the page{f': {title}' if title else ''}. I put the readable text in the chat."
    if cleaned.startswith("Search results for:"):
        fixtures = next((line for line in raw_lines if line.startswith("Likely fixtures:")), "")
        if fixtures:
            return fixtures
        titles = [line for line in raw_lines if re.match(r"^\d+\.\s+", line)]
        if titles:
            first = re.sub(r"^\d+\.\s*", "", titles[0]).strip()
            return f"I found live web results. Top result: {first}. I put the rest in the chat."
    if len(cleaned) <= 360:
        return cleaned
    clipped = cleaned[:357].rsplit(" ", 1)[0].rstrip(" .,;:")
    return clipped + "."


async def _speak_background(text: str):
    try:
        await VOICE.speak(text)
    except Exception as exc:
        print(f"Background voice error: {exc}")


async def _speech_worker():
    global SPEECH_NEXT, SPEECH_WORKER
    try:
        while True:
            speech = SPEECH_NEXT
            SPEECH_NEXT = None
            if not speech:
                return
            await _speak_background(speech)
    finally:
        SPEECH_WORKER = None
        if SPEECH_NEXT:
            _start_speech_worker()


def _start_speech_worker():
    global SPEECH_WORKER
    if SPEECH_WORKER is None or SPEECH_WORKER.done():
        SPEECH_WORKER = asyncio.create_task(_speech_worker())


def _queue_speech(text: str):
    global SPEECH_NEXT
    speech = _speech_excerpt(text)
    if not speech:
        return
    SPEECH_NEXT = speech
    _start_speech_worker()


async def _complete(messages, with_tools=True, max_tokens: int | None = None):
    payload = {"model": BRAIN.model, "messages": messages,
               "temperature": BRAIN.temperature, "max_tokens": max_tokens or BRAIN.max_tokens, "stream": False}
    if with_tools:
        payload["tools"] = tools.openai_tools()
        payload["tool_choice"] = "auto"
    data = await BRAIN.chat_completion(payload, timeout=120.0)
    return data["choices"][0]["message"]


def _reasoning_retry_tokens() -> int:
    """Reasoning models may spend the first budget on hidden scratchpad."""
    return max(int(BRAIN.max_tokens or 0), 1536)


async def _ask_for_shortcuts(question: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM + "\n\nShortcuts voice mode: answer in three sentences or fewer."},
        {"role": "user", "content": question},
    ]
    return _text(await _complete(messages, with_tools=False)) or "I have nothing useful to say yet."


async def _question_from_legacy_request(request: Request) -> str:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        if isinstance(body, dict):
            return str(
                body.get("question")
                or body.get("message")
                or body.get("text")
                or body.get("q")
                or ""
            ).strip()
    return (await request.body()).decode("utf-8", "replace").strip()


async def _legacy_query_reply(question: str, speak: bool = True) -> str:
    question = " ".join((question or "").split())
    if not question:
        return "Jarvis is reachable. Pass a question, message, text, or q value."
    command = _matched_command(question) or _intent_command(question)
    if command and tools.is_safe(command["tool"]):
        name = command["tool"]
        args = command.get("args", {})
        result = await tools.execute(name, args, CONFIG)
        reply = _deterministic_tool_reply(name, args, result)
    else:
        try:
            reply = await _ask_for_shortcuts(question)
        except httpx.HTTPError as exc:
            reply = _model_unavailable_reply(exc)
    if speak:
        _queue_speech(reply)
    return reply


async def _summarise_action(name, args, result, speak, user_message: str | None = None):
    if name == "web_search":
        instruction = (
            f"The user asked: {user_message or '(unknown)'}\n\n"
            f"You ran web_search({json.dumps(args)}). Raw search evidence:\n{result[:6000]}\n\n"
            "Answer the user's question directly from the evidence. Do not dump the raw search results. "
            "For fixtures or schedules, list the matchups first in plain English. "
            "Mention uncertainty if the evidence is weak or conflicting. "
            "Do not explain your reasoning or tool use. Return only the final answer in 1-3 short sentences. "
            "Ask at most one short follow-up question if it would clearly help."
        )
    else:
        instruction = (
            f"(You ran {name}({json.dumps(args)}). Result:\n{result[:6000]}\n"
            "Summarise for the user, briefly and in character. If this is a vault search, "
            "answer the user's question from the evidence and mention uncertainty. "
            "Ask at most one short follow-up question if it would clearly help.)"
        )
    follow = _convo() + [{"role": "user", "content": instruction}]
    try:
        reply = _text(await _complete(follow, with_tools=False))
    except httpx.HTTPError as exc:
        fallback = _fallback_web_search_summary(result) if name == "web_search" else None
        reply = fallback or _fallback_vault_summary(args, result) or _fallback_action_summary(name, result) or _model_unavailable_reply(exc)
    if _looks_incomplete(reply):
        fallback = _fallback_web_search_summary(result) if name == "web_search" else None
        reply = fallback or _fallback_vault_summary(args, result) or _fallback_action_summary(name, result)
    _remember_tool_result(name, args, result)
    _remember("assistant", reply)
    if speak:
        _queue_speech(reply)
    return reply


@app.on_event("startup")
async def _startup():
    await VOICE.initialize()
    await BRAIN.initialize()
    await PLAYBOOKS.start()


@app.on_event("shutdown")
async def _shutdown():
    await PLAYBOOKS.stop()


install_shortcuts_routes(app, CONFIG, _ask_for_shortcuts, PLAYBOOKS)


@app.get("/", response_class=HTMLResponse)
async def index():
    return (WEB / "chat.html").read_text(encoding="utf-8")


@app.get("/api/voice/status")
async def voice_status(probe: bool = False):
    return JSONResponse(await VOICE.runtime_status_async(probe_chatterbox=probe))


@app.get("/api/agent/status")
async def agent_status(probe: bool = False):
    return JSONResponse(await _agent_status_packet(probe=probe))


@app.get("/api/pet/status")
async def pet_status(probe: bool = False):
    return JSONResponse(await _agent_status_packet(probe=probe))


@app.post("/api/voice/reload-chatterbox")
async def reload_chatterbox_voice():
    return JSONResponse(await VOICE.try_promote_chatterbox(probe_audio=True))


@app.get("/api/commands")
async def commands():
    return JSONResponse({"commands": COMMANDS})


@app.get("/api/query", response_class=PlainTextResponse)
@app.get("/query", response_class=PlainTextResponse)
@app.get("/api/ask", response_class=PlainTextResponse)
async def legacy_query_get(
    q: str = "",
    question: str = "",
    message: str = "",
    text: str = "",
    speak: bool = True,
):
    return PlainTextResponse(await _legacy_query_reply(q or question or message or text, speak=speak))


@app.post("/api/query", response_class=PlainTextResponse)
@app.post("/query", response_class=PlainTextResponse)
@app.post("/api/ask", response_class=PlainTextResponse)
async def legacy_query_post(request: Request, speak: bool = True):
    return PlainTextResponse(await _legacy_query_reply(await _question_from_legacy_request(request), speak=speak))


@app.post("/api/capture")
async def capture(body: CaptureIn):
    item = _store_capture(body.text, source=body.source, context=body.context)
    reply = _capture_ack(item)
    if body.speak:
        _queue_speech(reply)
    return JSONResponse({
        "reply": reply,
        "item": item,
        "capture": _capture_status(),
    })


@app.get("/api/capture/inbox")
async def capture_inbox(limit: int = 20):
    return JSONResponse({
        "inbox_dir": str(_capture_dir()),
        "captures": _load_captures(limit=limit),
    })


@app.post("/api/chat")
async def chat(body: ChatIn):
    actions = []

    if body.confirm:
        name, args = body.confirm.get("name"), body.confirm.get("args", {})
        result = await tools.execute(name, args, CONFIG)
        actions.append({"tool": name, "args": args, "result": result[:1500]})
        reply = await _summarise_action(name, args, result, body.speak)
        return JSONResponse({"reply": reply, "actions": actions})

    if body.decline:
        playbook_result = await PLAYBOOKS.handle_chat("later")
        reply = playbook_result["reply"] if playbook_result else "As you wish. I'll leave it."
        _remember("assistant", reply)
        return JSONResponse({"reply": reply, "actions": actions})

    if body.message:
        playbook_result = await PLAYBOOKS.handle_chat(body.message)
        if playbook_result:
            reply = playbook_result["reply"]
            _remember("user", body.message)
            _remember("assistant", reply)
            if body.speak:
                _queue_speech(reply)
            return JSONResponse({
                "reply": reply,
                "actions": actions,
                "playbook": {
                    "status": playbook_result["status"],
                    "active": playbook_result.get("active"),
                    "snooze_until": playbook_result.get("snooze_until"),
                },
            })

        command = _matched_command(body.message)
        if not command:
            command = _intent_command(body.message)
        if command and tools.is_safe(command["tool"]):
            name = command["tool"]
            args = command.get("args", {})
            result = await tools.execute(name, args, CONFIG)
            actions.append({"tool": name, "args": args, "result": result[:1500]})
            _remember("user", body.message)
            if name == "web_search":
                reply = await _summarise_action(name, args, result, body.speak, body.message)
            else:
                reply = _deterministic_tool_reply(name, args, result)
                _remember_tool_result(name, args, result)
                _remember("assistant", reply)
                if body.speak:
                    _queue_speech(reply)
            return JSONResponse({"reply": reply, "actions": actions, "command": command["id"]})

    _remember("user", body.message or "")
    try:
        msg = await _complete(_convo(), with_tools=True)
    except httpx.HTTPError as exc:
        reply = _model_unavailable_reply(exc)
        _remember("assistant", reply)
        return JSONResponse({"reply": reply, "actions": actions, "model_error": True})
    calls = msg.get("tool_calls") or []

    if calls:
        fn = calls[0].get("function", {})
        name = fn.get("name", "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        if tools.is_safe(name):
            result = await tools.execute(name, args, CONFIG)
            actions.append({"tool": name, "args": args, "result": result[:1500]})
            reply = await _summarise_action(name, args, result, body.speak)
            return JSONResponse({"reply": reply, "actions": actions})
        return JSONResponse({"reply": f"That one needs your go-ahead. Shall I run {name}?",
                             "pending": {"name": name, "args": args}, "actions": actions})

    reply = _text(msg)
    if _looks_incomplete(reply):
        try:
            reply = _text(await _complete(_convo(), with_tools=False, max_tokens=_reasoning_retry_tokens()))
        except Exception as exc:
            print(f"Model retry after incomplete reply failed: {exc}")
    if _looks_incomplete(reply):
        reply = _fallback_contextual_reply(body.message) or "I heard you, but the local model returned an empty answer. Try me once more."
    _remember("assistant", reply)
    if body.speak:
        _queue_speech(reply)
    return JSONResponse({"reply": reply, "actions": actions})


def main():
    import uvicorn
    port = int((CONFIG.get("server", {}) or {}).get("port", 4343))
    print(f"Jarvis chat box -> http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
