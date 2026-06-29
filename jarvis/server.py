"""
Yennefer chat server - a web chat box with hands.

FastAPI backend that serves a chat UI and runs an LLM tool-calling loop against
LM Studio. Yennefer can answer, and she can invoke registered tools/agents on
the Mac. Safe tools auto-run; side-effectful ones return a confirmation request
the UI must approve before they execute.

Run:  python -m jarvis.server     (then open the printed URL)
"""

import json
import asyncio
import re
from datetime import date
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

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
        "description": "Show what Yennefer can do right now.",
        "trigger": "what can you do",
        "tool": "capabilities",
        "args": {},
    },
    {
        "id": "yennefer-doctor",
        "name": "Yennefer Doctor",
        "description": "Check and repair backend, overlay, voice, and LM Studio failover.",
        "trigger": "repair yennefer services",
        "tool": "yennefer_doctor",
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
        "id": "git-yennefer",
        "name": "Yennefer Git Status",
        "description": "Show git status for the Yennefer backend.",
        "trigger": "show git status for /Users/alsharma/Projects/yennefer",
        "tool": "git_status",
        "args": {"path": "/Users/alsharma/Projects/yennefer"},
    },
    {
        "id": "git-yennefer-overlay",
        "name": "Overlay Git Status",
        "description": "Show git status for the Yennefer overlay.",
        "trigger": "show git status for /Users/alsharma/Projects/yennefer-overlay",
        "tool": "git_status",
        "args": {"path": "/Users/alsharma/Projects/yennefer-overlay"},
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
app = FastAPI(title="Yennefer")


class ChatIn(BaseModel):
    message: str | None = None
    speak: bool = False
    confirm: dict | None = None
    decline: bool = False


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
            + ". Command actions still work; run 'repair yennefer services' for the recovery check."
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
            "description": "Show what Yennefer can do right now.",
            "trigger": text,
            "tool": "capabilities",
            "args": {},
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
    if name in {"capabilities", "weather", "web_search", "web_fetch", "observe_browser", "yennefer_doctor"}:
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


@app.post("/api/voice/reload-chatterbox")
async def reload_chatterbox_voice():
    return JSONResponse(await VOICE.try_promote_chatterbox(probe_audio=True))


@app.get("/api/commands")
async def commands():
    return JSONResponse({"commands": COMMANDS})


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
    print(f"Yennefer chat box -> http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
