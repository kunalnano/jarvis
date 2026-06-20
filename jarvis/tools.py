"""
Tools - Yennefer's hands. A registry of named actions she can invoke on the Mac.

Safe-by-default: read-only / benign tools auto-run. Anything side-effectful
(arbitrary shell, registered agents) is GATED - the server returns a
confirmation request and only runs on explicit user approval. This keeps her
Siri-like (she just does things) without letting a local model execute whatever
it hallucinates.
"""

import asyncio
import html
import os
import re
import shlex
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx


MAX_TOOL_OUTPUT_CHARS = 4000
DEFAULT_USER_AGENT = "YenneferLocal/1.0"
BLOCK_TAGS = {
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}
SKIP_TAGS = {"script", "style", "svg", "noscript"}


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


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lo, min(hi, parsed))


def _bool_arg(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _http_url(url: str) -> str | None:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url.strip()


def _clean_result_url(url: str, base_url: str) -> str:
    cleaned = urljoin(base_url, html.unescape(url.strip()))
    parsed = urlparse(cleaned)
    params = parse_qs(parsed.query)
    if "uddg" in params and params["uddg"]:
        cleaned = params["uddg"][0]
    return cleaned


def _normalise_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip: list[str] = []

    def handle_starttag(self, tag, _attrs):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip.append(tag)
            return
        if not self.skip and tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip and self.skip[-1] == tag:
            self.skip.pop()
            return
        if not self.skip and tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        return _normalise_text(" ".join(self.parts))


class _SearchResultParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.results: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.capture_title = False
        self.capture_snippet = False
        self.snippet_target: dict[str, str] | None = None

    @staticmethod
    def _classes(attrs) -> set[str]:
        attrs_dict = dict(attrs)
        return set(str(attrs_dict.get("class", "")).split())

    def handle_starttag(self, tag, attrs):
        classes = self._classes(attrs)
        attrs_dict = dict(attrs)
        if tag == "a" and "result__a" in classes:
            self.current = {
                "title": "",
                "url": _clean_result_url(str(attrs_dict.get("href", "")), self.base_url),
                "snippet": "",
            }
            self.capture_title = True
            return
        if "result__snippet" in classes and self.results:
            self.snippet_target = self.results[-1]
            self.capture_snippet = True

    def handle_endtag(self, tag):
        if self.capture_title and tag == "a":
            if self.current and _http_url(self.current["url"]):
                self.current["title"] = _normalise_text(self.current["title"])
                if self.current["title"]:
                    self.results.append(self.current)
            self.current = None
            self.capture_title = False
        if self.capture_snippet and tag in {"a", "div"}:
            self.snippet_target = None
            self.capture_snippet = False

    def handle_data(self, data):
        if self.capture_title and self.current is not None:
            self.current["title"] += data
        elif self.capture_snippet and self.snippet_target is not None:
            self.snippet_target["snippet"] += data


def _extract_html_text(markup: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(markup)
    text = parser.text()
    if text:
        return text
    fallback = re.sub(r"<[^>]+>", " ", markup)
    return _normalise_text(fallback)


def _extract_text(body: str, content_type: str) -> str:
    lower_type = content_type.lower()
    if "html" in lower_type or "<html" in body[:500].lower():
        return _extract_html_text(body)
    return _normalise_text(body)


def _parse_search_results(markup: str, base_url: str, limit: int) -> list[dict[str, str]]:
    parser = _SearchResultParser(base_url)
    parser.feed(markup)
    seen = set()
    results = []
    for result in parser.results:
        url = result["url"]
        if url in seen:
            continue
        seen.add(url)
        result["snippet"] = _normalise_text(result.get("snippet", ""))
        results.append(result)
        if len(results) >= limit:
            break
    return results


def _web_config(cfg: dict | None) -> dict:
    return ((cfg or {}).get("web") or {})


def _http_transport(cfg: dict | None):
    return (cfg or {}).get("_http_transport") or _web_config(cfg).get("_http_transport")


def _http_headers(cfg: dict | None) -> dict[str, str]:
    return {"User-Agent": str(_web_config(cfg).get("user_agent") or DEFAULT_USER_AGENT)}


async def _read_url(url: str, cfg: dict | None, max_chars: int = MAX_TOOL_OUTPUT_CHARS) -> dict[str, str]:
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers=_http_headers(cfg),
        timeout=float(_web_config(cfg).get("timeout", 20.0)),
        transport=_http_transport(cfg),
    ) as client:
        response = await client.get(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    text = _extract_text(response.text, content_type)
    return {
        "url": str(response.url),
        "status": str(response.status_code),
        "content_type": content_type or "unknown",
        "text": text[:max_chars],
    }


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

async def fetch_url(args, cfg):
    url = _http_url(str(args.get("url", "")))
    if not url:
        return f"Refusing to fetch non-http URL: {args.get('url', '')}"
    max_chars = _clamp_int(args.get("max_chars"), MAX_TOOL_OUTPUT_CHARS, 500, 8000)
    page = await _read_url(url, cfg, max_chars=max_chars)
    if not page["text"]:
        return f"Fetched {page['url']} ({page['status']}, {page['content_type']}), but extracted no text."
    return (
        f"Fetched {page['url']} ({page['status']}, {page['content_type']})\n\n"
        f"{page['text']}"
    )

async def web_search(args, cfg):
    query = str(args.get("query", "")).strip()
    if not query:
        return "No search query given."

    max_results = _clamp_int(args.get("max_results"), 5, 1, 8)
    fetch_results = _bool_arg(args.get("fetch_results"), True)
    fetch_count = _clamp_int(args.get("fetch_count"), 2, 0, min(3, max_results))
    search_cfg = _web_config(cfg)
    search_url = str(search_cfg.get("search_url") or "https://duckduckgo.com/html/")
    search_param = str(search_cfg.get("search_query_param") or "q")

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers=_http_headers(cfg),
        timeout=float(search_cfg.get("timeout", 20.0)),
        transport=_http_transport(cfg),
    ) as client:
        response = await client.get(search_url, params={search_param: query})
    response.raise_for_status()

    results = _parse_search_results(response.text, str(response.url), max_results)
    if not results:
        return f"No search results found for: {query}"

    lines = [f"Search results for: {query}"]
    for idx, result in enumerate(results, start=1):
        lines.append(f"{idx}. {result['title']}")
        lines.append(f"   URL: {result['url']}")
        if result.get("snippet"):
            lines.append(f"   Snippet: {result['snippet']}")

    if fetch_results and fetch_count:
        lines.append("")
        lines.append("Fetched excerpts:")
        for idx, result in enumerate(results[:fetch_count], start=1):
            try:
                page = await _read_url(result["url"], cfg, max_chars=1200)
            except Exception as exc:
                lines.append(f"{idx}. {result['url']} - fetch failed: {exc}")
                continue
            excerpt = page["text"] or "(no extractable text)"
            lines.append(f"{idx}. {result['title']} ({page['url']})")
            lines.append(excerpt)

    return "\n".join(lines)[:MAX_TOOL_OUTPUT_CHARS]


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
    "fetch_url": {"handler": fetch_url, "safe": True,
        "description": "Fetch an http(s) URL and return extracted page text into the conversation. Use this before making claims about web pages.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "max_chars": {"type": "integer", "description": "Maximum extracted characters to return, 500-8000"}}, "required": ["url"]}},
    "web_search": {"handler": web_search, "safe": True,
        "description": "Search the web and return result titles, URLs, snippets, and fetched excerpts from top results. Use before answering current-events or external-fact questions.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "description": "Number of search results, 1-8"}, "fetch_results": {"type": "boolean", "description": "Whether to fetch excerpts from top results"}, "fetch_count": {"type": "integer", "description": "Number of top results to fetch, 0-3"}}, "required": ["query"]}},
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
