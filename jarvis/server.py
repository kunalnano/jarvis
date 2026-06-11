"""
Yennefer chat server - a web chat box with hands.

FastAPI backend that serves a chat UI and runs an LLM tool-calling loop against
LM Studio. Yennefer can answer, and she can invoke registered tools/agents on
the Mac. Safe tools auto-run; side-effectful ones return a confirmation request
the UI must approve before they execute.

Run:  python -m jarvis.server     (then open the printed URL)
"""

import json
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .brain import Brain, YENNEFER_SYSTEM_PROMPT, extract_speakable
from .voice import Voice
from .config import load_config
from . import tools

CONFIG = load_config()
BRAIN = Brain(CONFIG)
VOICE = Voice(CONFIG)
WEB = Path(__file__).parent / "web"

SYSTEM = (
    YENNEFER_SYSTEM_PROMPT
    + "\n\nYou have hands: tools to act on the user's Mac (check status, open apps or "
    "URLs, inspect repos, run registered agents). When the user asks you to DO "
    "something, call the right tool instead of describing how. If nothing fits, just "
    "answer. Keep replies short."
)

HISTORY = [{"role": "system", "content": SYSTEM}]
app = FastAPI(title="Yennefer")


class ChatIn(BaseModel):
    message: str | None = None
    speak: bool = False
    confirm: dict | None = None
    decline: bool = False


def _convo():
    return [m for m in HISTORY if m["role"] in ("system", "user", "assistant")]


def _text(msg):
    return extract_speakable(msg)


async def _complete(messages, with_tools=True):
    payload = {"model": BRAIN.model, "messages": messages,
               "temperature": BRAIN.temperature, "max_tokens": BRAIN.max_tokens, "stream": False}
    if with_tools:
        payload["tools"] = tools.openai_tools()
        payload["tool_choice"] = "auto"
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{BRAIN.api_base}/chat/completions", json=payload, timeout=120.0)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


async def _summarise_action(name, args, result, speak):
    follow = _convo() + [{"role": "user", "content":
        f"(You ran {name}({json.dumps(args)}). Result:\n{result[:1500]}\nSummarise for the user, briefly and in character.)"}]
    reply = _text(await _complete(follow, with_tools=False)) or "Done."
    HISTORY.append({"role": "assistant", "content": reply})
    if speak:
        await VOICE.speak(reply)
    return reply


@app.on_event("startup")
async def _startup():
    await VOICE.initialize()
    await BRAIN.initialize()


@app.get("/", response_class=HTMLResponse)
async def index():
    return (WEB / "chat.html").read_text(encoding="utf-8")


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
        reply = "As you wish. I'll leave it."
        HISTORY.append({"role": "assistant", "content": reply})
        return JSONResponse({"reply": reply, "actions": actions})

    HISTORY.append({"role": "user", "content": body.message or ""})
    msg = await _complete(_convo(), with_tools=True)
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

    reply = _text(msg) or "..."
    HISTORY.append({"role": "assistant", "content": reply})
    if body.speak:
        await VOICE.speak(reply)
    return JSONResponse({"reply": reply, "actions": actions})


def main():
    import uvicorn
    port = int((CONFIG.get("server", {}) or {}).get("port", 4343))
    print(f"Yennefer chat box -> http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
