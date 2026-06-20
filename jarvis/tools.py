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
from datetime import datetime
from pathlib import Path

import httpx

MAX_TOOL_OUTPUT_CHARS = 4000


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
    return text[:MAX_TOOL_OUTPUT_CHARS] if text else "(no output)"


def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


def _bool_arg(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _bridge_config(cfg: dict | None) -> tuple[str, str]:
    playbooks = (cfg or {}).get("playbooks", {}) or {}
    url = (
        playbooks.get("linear_bridge_url")
        or os.environ.get("YENNEFER_LINEAR_BRIDGE_URL")
        or os.environ.get("LINEAR_BRIDGE_URL")
        or ""
    )
    token = (
        playbooks.get("linear_bridge_token")
        or os.environ.get("YENNEFER_LINEAR_BRIDGE_TOKEN")
        or os.environ.get("LINEAR_BRIDGE_TOKEN")
        or ""
    )
    return str(url).rstrip("/"), str(token)


async def _bridge_request(method: str, path: str, cfg: dict | None, *, params=None, payload=None) -> dict:
    url, token = _bridge_config(cfg)
    if not url or not token:
        raise RuntimeError("Linear bridge URL/token is not configured")
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.request(
            method,
            f"{url}{path}",
            params=params or {},
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code != 200:
        raise RuntimeError(f"Linear bridge HTTP {response.status_code}: {response.text[:200]}")
    data = response.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data


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

async def linear_counts(args, cfg):
    states = str(args.get("states", "Backlog,Todo,In Progress,In Review")).strip()
    data = await _bridge_request("GET", "/linear/counts", cfg, params={"states": states})
    scope = data.get("scope", {})
    counts = data.get("counts", {})
    pieces = [f"{state}: {count}" for state, count in counts.items()]
    return (
        "Linear active issue counts "
        f"(scope: states={', '.join(scope.get('states', []))}; active_only={scope.get('active_only', True)}): "
        + ", ".join(pieces)
    )

async def linear_comment(args, cfg):
    issue_id = str(args.get("issue_id", "")).strip()
    body = str(args.get("body", "")).strip()
    if not issue_id or not body:
        return "issue_id and body are required."
    await _bridge_request(
        "POST",
        f"/linear/issues/{issue_id}/comments",
        cfg,
        payload={"body": body},
    )
    return f"Commented on {issue_id}. The Linear bridge audit log recorded the write."

async def linear_move_issue(args, cfg):
    issue_id = str(args.get("issue_id", "")).strip()
    state = str(args.get("state", "")).strip()
    reason = str(args.get("reason", "")).strip()
    approved_by = str(args.get("approved_by", "Hank via Yennefer UI confirmation")).strip()
    if not issue_id or not state:
        return "issue_id and state are required."
    data = await _bridge_request(
        "POST",
        f"/linear/issues/{issue_id}/state",
        cfg,
        payload={"state": state, "approved_by": approved_by, "reason": reason or None},
    )
    issue = data.get("issue") or {}
    return (
        f"Moved {issue.get('identifier', issue_id)} to {issue.get('state', state)}. "
        "The Linear bridge audit log recorded the write."
    )

async def screen_context(args, _cfg):
    include_screenshot = _bool_arg(args.get("include_screenshot"), True)
    front = await _run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to set frontApp to name of first application process whose frontmost is true',
            "-e",
            'tell application "System Events" to tell process frontApp to set windowTitle to name of front window',
            "-e",
            'return frontApp & "\n" & windowTitle',
        ],
        timeout=10,
    )
    lines = [line.strip() for line in front.splitlines() if line.strip()]
    app_name = lines[0] if lines else "unknown"
    window_title = lines[1] if len(lines) > 1 else "unknown"

    screenshot_line = "Screenshot capture skipped."
    if include_screenshot:
        directory = Path(_expand(str(args.get("directory") or "~/.yennefer/screenshots")))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"screen-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        capture = await _run(["screencapture", "-x", str(path)], timeout=15)
        if path.exists():
            size = path.stat().st_size
            screenshot_line = f"Screenshot saved: {path} ({size} bytes)."
        else:
            screenshot_line = f"Screenshot capture failed: {capture}"

    return (
        "Current screen context:\n"
        f"- Front app: {app_name}\n"
        f"- Window title: {window_title}\n"
        f"- {screenshot_line}\n"
        "Grounding note: this tool reports macOS foreground metadata and a captured image path; "
        "visual interpretation should cite the screenshot path."
    )

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
    "linear_counts": {"handler": linear_counts, "safe": True,
        "description": "Read scoped active Linear issue counts through the local bridge without exposing the Linear API key.",
        "parameters": {"type": "object", "properties": {"states": {"type": "string", "description": "Comma-separated Linear state names, for example Todo or Todo,In Review"}}}},
    "linear_comment": {"handler": linear_comment, "safe": False,
        "description": "Add a Linear comment through the local bridge. Requires explicit user confirmation and is audit-logged.",
        "parameters": {"type": "object", "properties": {"issue_id": {"type": "string"}, "body": {"type": "string"}}, "required": ["issue_id", "body"]}},
    "linear_move_issue": {"handler": linear_move_issue, "safe": False,
        "description": "Move a Linear issue to a state through the local bridge. Requires explicit user confirmation and is audit-logged.",
        "parameters": {"type": "object", "properties": {"issue_id": {"type": "string"}, "state": {"type": "string"}, "reason": {"type": "string"}, "approved_by": {"type": "string"}}, "required": ["issue_id", "state"]}},
    "screen_context": {"handler": screen_context, "safe": True,
        "description": "Capture the current macOS foreground app/window and optionally save a screenshot for grounded visual follow-up.",
        "parameters": {"type": "object", "properties": {"include_screenshot": {"type": "boolean"}, "directory": {"type": "string", "description": "Optional screenshot output directory"}}}},
    "run_agent": {"handler": run_agent, "safe": False,
        "description": "Invoke a registered agent/script by name (names defined in config 'agents'). Side-effectful; the user confirms first.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "args": {"type": "string", "description": "optional extra arguments"}}, "required": ["name"]}},
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
