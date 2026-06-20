"""
Tools - Yennefer's hands. A registry of named actions she can invoke on the Mac.

Safe-by-default: read-only / benign tools auto-run. Anything side-effectful
(arbitrary shell, registered agents) is GATED - the server returns a
confirmation request and only runs on explicit user approval. This keeps her
Siri-like (she just does things) without letting a local model execute whatever
it hallucinates.
"""

import asyncio
import os
import shlex
from pathlib import Path


async def _run(cmd, timeout: float = 60.0, cwd=None) -> str:
    """Run a command (str=shell, list=exec), capture combined output."""
    if isinstance(cmd, str):
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=cwd,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=cwd,
        )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"(timed out after {timeout:.0f}s)"
    text = out.decode("utf-8", "replace").strip()
    return text[:4000] if text else "(no output)"


def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


# ---- handlers ----------------------------------------------------------

async def system_status(_args, _cfg):
    return await _run(
        "echo '== uptime =='; uptime; echo; echo '== disk =='; df -h / | tail -2; "
        "echo; echo '== top cpu =='; ps aux | sort -nrk 3 | head -6"
    )

async def git_status(args, _cfg):
    path = _expand(str(args.get("path", "")))
    if not path or not Path(path, ".git").exists():
        return f"Not a git repo: {path or '(none given)'}"
    q = shlex.quote(path)
    return await _run(f"git -C {q} status -sb && echo '--- recent ---' && git -C {q} log -3 --oneline")

async def list_projects(_args, _cfg):
    return await _run(
        "echo '~/Projects:'; ls -1 ~/Projects 2>/dev/null | head -40; "
        "echo; echo '~/Documents/ai:'; ls -1 ~/Documents/ai 2>/dev/null | head -40"
    )

async def open_app(args, _cfg):
    name = str(args.get("name", "")).strip()
    if not name:
        return "No app name given."
    await _run(["open", "-a", name], timeout=10)
    return f"Opened {name}."

async def open_url(args, _cfg):
    url = str(args.get("url", "")).strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return f"Refusing to open non-http URL: {url}"
    await _run(["open", url], timeout=10)
    return f"Opened {url}."

async def run_agent(args, cfg):
    name = str(args.get("name", "")).strip()
    agents = (cfg or {}).get("agents", {}) or {}
    if name not in agents:
        avail = ", ".join(agents.keys()) or "(none registered)"
        return f"Unknown agent '{name}'. Registered: {avail}"
    spec = agents[name]
    cmd = spec["command"] if isinstance(spec, dict) else str(spec)
    cwd = _expand(spec["cwd"]) if isinstance(spec, dict) and spec.get("cwd") else None
    timeout = float(spec.get("timeout", 120)) if isinstance(spec, dict) else 120
    extra = str(args.get("args", "")).strip()
    return await _run(f"{cmd} {extra}".strip(), timeout=timeout, cwd=cwd)

async def run_shell(args, _cfg):
    cmd = str(args.get("command", "")).strip()
    if not cmd:
        return "No command given."
    return await _run(cmd, timeout=120)


# ---- DAR-128: skill dispatch ------------------------------------------
# Yennefer runs curated DVC Claude Code skills via `claude -p "/<id>"`.
# The registry is code-level so it works regardless of which YAML config is
# loaded; per-skill overrides (command/cwd/timeout/aliases) may be supplied in
# config 'skills', and any directory under ~/.claude/skills is also runnable.

DEFAULT_SKILLS = {
    "dvc-eod-done-sweep": {
        "aliases": ["done sweep", "run the done sweep", "eod sweep",
                    "end of day sweep", "validate the done lane"],
    },
    "twitter-bookmark-review": {
        "aliases": ["bookmark review", "bookmark pipeline",
                    "process my bookmarks", "run my twitter bookmark review"],
    },
    "chat-vault-pipeline": {
        "aliases": ["chat ingestion", "ingest my chats", "vault ingest",
                    "run the chat ingestion pipeline"],
    },
    "meeting-prep": {
        "aliases": ["meeting prep", "prep my meeting", "prep my next meeting",
                    "get me ready for my meeting"],
    },
}


def _skill_table(cfg) -> dict:
    """Built-in skills + ~/.claude/skills dirs + config 'skills' overrides.

    Config entries win on conflict so a skill can be re-pointed or given a
    custom command/cwd/timeout without code changes."""
    table = {k: dict(v) for k, v in DEFAULT_SKILLS.items()}
    skills_dir = Path.home() / ".claude" / "skills"
    if skills_dir.is_dir():
        for child in skills_dir.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                table.setdefault(child.name, {"aliases": []})
    for name, spec in ((cfg or {}).get("skills", {}) or {}).items():
        entry = table.get(name, {})
        if isinstance(spec, dict):
            entry.update(spec)
        else:
            entry["command"] = str(spec)
        table[name] = entry
    return table


def _resolve_skill(raw: str, table: dict) -> str | None:
    """Map a raw id or freeform phrase to a known skill id, else None."""
    q = (raw or "").strip().lstrip("/").strip().lower()
    if not q:
        return None
    for name in table:                       # exact id (case-insensitive)
        if name.lower() == q:
            return name
    for name, entry in table.items():        # exact alias
        if any(a.lower() == q for a in entry.get("aliases", [])):
            return name
    for name, entry in table.items():        # fuzzy alias containment
        if any(a.lower() in q or q in a.lower() for a in entry.get("aliases", [])):
            return name
    return None


def _skill_command(name: str, entry: dict, extra: str) -> str:
    """Shell command for a skill. Default drives it through Claude Code
    headless: claude -p "/<id> <extra>". A config 'command' overrides."""
    if entry.get("command"):
        base = str(entry["command"])
        return f"{base} {extra}".strip() if extra else base
    slash = f"/{name}" + (f" {extra}" if extra else "")
    return f"claude -p {shlex.quote(slash)}"


async def run_skill(args, cfg):
    raw = str(args.get("skill") or args.get("name") or "").strip()
    extra = str(args.get("args", "")).strip()
    table = _skill_table(cfg)
    name = _resolve_skill(raw, table)
    if not name:
        known = ", ".join(sorted(table)) or "(none)"
        return f"I don't have a skill called '{raw}'. I can run: {known}."
    entry = table[name]
    cwd = _expand(entry["cwd"]) if entry.get("cwd") else str(Path.home())
    timeout = float(entry.get("timeout", 900))
    out = await _run(_skill_command(name, entry, extra), timeout=timeout, cwd=cwd)
    return f"[{name}] {out}"


# ---- registry ----------------------------------------------------------
# safe=True -> auto-runs.  safe=False -> requires explicit user confirmation.

REGISTRY = {
    "system_status": {"handler": system_status, "safe": True,
        "description": "Report Mac health: uptime, disk usage, and top CPU processes.",
        "parameters": {"type": "object", "properties": {}}},
    "git_status": {"handler": git_status, "safe": True,
        "description": "Show git status and recent commits for a local repo path.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Absolute or ~ path to the repo"}}, "required": ["path"]}},
    "list_projects": {"handler": list_projects, "safe": True,
        "description": "List the user's project directories under ~/Projects and ~/Documents/ai.",
        "parameters": {"type": "object", "properties": {}}},
    "open_app": {"handler": open_app, "safe": True,
        "description": "Open a macOS application by name (e.g. Safari, Terminal, Visual Studio Code).",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    "open_url": {"handler": open_url, "safe": True,
        "description": "Open an http(s) URL in the default browser.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    "run_agent": {"handler": run_agent, "safe": False,
        "description": "Invoke a registered agent/script by name (names defined in config 'agents'). Side-effectful; the user confirms first.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "args": {"type": "string", "description": "optional extra arguments"}}, "required": ["name"]}},
    "run_skill": {"handler": run_skill, "safe": False,
        "description": "Run one of Yennefer's curated DVC skills by id or phrase via Claude Code (e.g. skill='dvc-eod-done-sweep' for 'run the done sweep'; 'bookmark review' -> twitter-bookmark-review). Side-effectful; the user confirms first. Unknown skills are reported back, never invented.",
        "parameters": {"type": "object", "properties": {"skill": {"type": "string", "description": "Skill id or freeform phrase, e.g. 'dvc-eod-done-sweep' or 'run the done sweep'"}, "args": {"type": "string", "description": "optional extra arguments passed to the skill"}}, "required": ["skill"]}},
    "run_shell": {"handler": run_shell, "safe": False,
        "description": "Run an arbitrary shell command on the Mac. Only when no specific tool fits. Always confirmed by the user first.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
}


def openai_tools():
    return [{"type": "function", "function": {"name": n, "description": s["description"], "parameters": s["parameters"]}}
            for n, s in REGISTRY.items()]


def is_safe(name: str) -> bool:
    return bool(REGISTRY.get(name, {}).get("safe", False))


async def execute(name: str, args: dict, cfg: dict) -> str:
    spec = REGISTRY.get(name)
    if not spec:
        return f"Unknown tool: {name}"
    try:
        return await spec["handler"](args or {}, cfg)
    except Exception as e:
        return f"Tool '{name}' failed: {e}"
