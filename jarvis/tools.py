"""
Tools - Jarvis's hands. A registry of named actions it can invoke on the Mac.

Safe-by-default: read-only / benign tools auto-run. Anything side-effectful
(arbitrary shell, registered agents) is GATED - the server returns a
confirmation request and only runs on explicit user approval. This keeps Jarvis
Siri-like (it just does things) without letting a local model execute whatever
it hallucinates.
"""

import asyncio
import html
import json
import os
import re
import shlex
import shutil
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
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


def _vault_root(cfg) -> Path:
    configured = (
        (cfg or {}).get("vault", {}) or {}
    ).get("path") or os.environ.get("OBSIDIAN_VAULT_PATH") or "~/Documents/ai/obsidian-vault"
    return Path(_expand(str(configured))).resolve()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _bin(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        candidate = Path(prefix) / name
        if candidate.exists():
            return str(candidate)
    return None


def _fetch_url(url: str, timeout: float = 12.0, max_bytes: int = 1_500_000) -> tuple[str, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Jarvis/1.0 (+local assistant)",
            "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.5",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content_type = resp.headers.get("content-type", "")
        data = resp.read(max_bytes)
    charset = "utf-8"
    match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    if match:
        charset = match.group(1)
    return content_type, data.decode(charset, "replace")


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title_parts: list[str] = []
        self.parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in {"p", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        text = " ".join(data.split())
        if not text or self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(text)
        else:
            self.parts.append(text)

    @property
    def title(self) -> str:
        return " ".join(self.title_parts).strip()

    @property
    def text(self) -> str:
        text = " ".join(self.parts)
        text = re.sub(r"\s*\n\s*", "\n", text)
        return re.sub(r"[ \t]{2,}", " ", text).strip()


def _html_to_text(html: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.title, parser.text


def _strip_markup(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _vault_relevance(text: str, pattern: re.Pattern) -> int:
    lower = text.lower()
    score = len(pattern.findall(text))
    for term in (
        "travel",
        "trip",
        "flight",
        "landing",
        "transit",
        "arrival",
        "departure",
        "itinerary",
        "hotel",
        "sko",
        "portugal",
        " lis ",
    ):
        if term in lower:
            score += 4
    for term in ("world travel map", "cesium", "token hygiene"):
        if term in lower:
            score -= 6
    return score


# ---- handlers ----------------------------------------------------------

async def system_status(_args, _cfg):
    machine = ((_cfg or {}).get("machine", {}) or {})
    identity = "\n".join(
        part for part in (
            "== identity ==",
            f"canonical_name: {machine.get('canonical_name')}" if machine.get("canonical_name") else "",
            f"role: {machine.get('role')}" if machine.get("role") else "",
            f"macos_reported_computer_name: {machine.get('macos_reported_computer_name')}"
            if machine.get("macos_reported_computer_name") else "",
            f"macos_reported_local_hostname: {machine.get('macos_reported_local_hostname')}"
            if machine.get("macos_reported_local_hostname") else "",
            f"tailscale_dns: {machine.get('tailscale_dns')}" if machine.get("tailscale_dns") else "",
            "mac_hosts: current=Prometheus, outgoing=Firestarter"
            if machine.get("mac_hosts") else "",
            "windows_hosts: " + ", ".join(machine.get("windows_hosts") or [])
            if machine.get("windows_hosts") else "",
            "invalid_aliases: " + ", ".join(machine.get("invalid_aliases") or [])
            if machine.get("invalid_aliases") else "",
            "not_this_host: " + ", ".join(machine.get("not_this_host") or [])
            if machine.get("not_this_host") else "",
        )
        if part
    )
    status = await _run(
        "echo '== uptime =='; uptime; echo; echo '== disk =='; df -h / | tail -2; "
        "echo; echo '== top cpu =='; ps aux | sort -nrk 3 | head -6"
    )
    return f"{identity}\n\n{status}" if identity else status

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


async def capabilities(_args, _cfg):
    return (
        "I can search the web, check weather, read the active Chrome or Safari tab, fetch a URL, "
        "search and read the local Vault, check Prometheus system status, list projects, "
        "check git repos, open apps or URLs, repair Jarvis services, and run curated skills or shell commands after confirmation. "
        "Fast commands bypass the local LLM; open-ended conversation still uses the slower local model."
    )


async def jarvis_doctor(args, _cfg):
    repair = bool(args.get("repair", True))
    script = Path(__file__).parent.parent / "scripts" / "jarvis-doctor.zsh"
    if not script.exists():
        return f"Jarvis doctor script is missing: {script}"
    mode = "--repair" if repair else "--check"
    return await _run([str(script), mode], timeout=90.0, cwd=str(script.parent.parent))

async def weather(args, cfg):
    location = str(args.get("location") or "").strip()
    if not location:
        location = str(((cfg or {}).get("weather", {}) or {}).get("default_location") or "").strip()
    place = urllib.parse.quote(location) if location else ""
    url = f"https://wttr.in/{place}?format=j1" if place else "https://wttr.in/?format=j1"
    try:
        _content_type, text = await asyncio.to_thread(_fetch_url, url, 10.0, 500_000)
        data = json.loads(text)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return f"Weather lookup failed: {exc}"

    current = (data.get("current_condition") or [{}])[0]
    area = (data.get("nearest_area") or [{}])[0]
    area_name = ((area.get("areaName") or [{}])[0].get("value") if area else None) or location or "your area"
    region = ((area.get("region") or [{}])[0].get("value") if area else None) or ""
    country = ((area.get("country") or [{}])[0].get("value") if area else None) or ""
    label = ", ".join(part for part in (area_name, region, country) if part)
    desc = ((current.get("weatherDesc") or [{}])[0].get("value")) or "unknown conditions"
    temp = current.get("temp_F") or current.get("temp_C") or "?"
    feels = current.get("FeelsLikeF") or current.get("FeelsLikeC") or "?"
    humidity = current.get("humidity") or "?"
    wind = current.get("windspeedMiles") or current.get("windspeedKmph") or "?"

    today = (data.get("weather") or [{}])[0]
    hourly = today.get("hourly") or []
    chance_rain = max((int(h.get("chanceofrain", 0) or 0) for h in hourly), default=0)
    hi = today.get("maxtempF") or today.get("maxtempC")
    lo = today.get("mintempF") or today.get("mintempC")
    range_text = f" High {hi}F, low {lo}F." if hi and lo else ""
    rain_text = f" Peak rain chance today: {chance_rain}%." if hourly else ""
    source = "wttr.in using IP-based location" if not location else "wttr.in"
    return (
        f"Weather for {label}: {temp}F, feels like {feels}F, {desc.lower()}. "
        f"Humidity {humidity}%, wind {wind} mph.{range_text}{rain_text} "
        f"Source: {source}."
    )


async def web_fetch(args, _cfg):
    url = str(args.get("url", "")).strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return f"Refusing to fetch non-http URL: {url}"
    max_chars = int(args.get("max_chars", 3500) or 3500)
    max_chars = max(500, min(max_chars, 12000))
    try:
        content_type, text = await asyncio.to_thread(_fetch_url, url, 12.0, 1_500_000)
    except (urllib.error.URLError, TimeoutError) as exc:
        return f"Web fetch failed for {url}: {exc}"

    if "html" in content_type.lower() or "<html" in text[:1000].lower():
        title, body = _html_to_text(text)
    else:
        title, body = "", text
    body = body.strip()
    if not body:
        return f"Fetched {url}, but did not find readable text."
    excerpt = body[:max_chars] + ("\n... truncated" if len(body) > max_chars else "")
    heading = f"Title: {title}\n" if title else ""
    return f"Fetched: {url}\n{heading}Text:\n{excerpt}"


def _web_search_google(query: str, count: int) -> tuple[str, list[dict]]:
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_SEARCH_API_KEY")
    cx = (
        os.environ.get("GOOGLE_SEARCH_ENGINE_ID")
        or os.environ.get("GOOGLE_CSE_ID")
        or os.environ.get("GOOGLE_CUSTOM_SEARCH_ENGINE_ID")
    )
    if not api_key or not cx:
        return "", []
    params = urllib.parse.urlencode({
        "key": api_key,
        "cx": cx,
        "q": query,
        "num": max(1, min(count, 10)),
    })
    _content_type, text = _fetch_url(
        f"https://www.googleapis.com/customsearch/v1?{params}",
        12.0,
        1_000_000,
    )
    data = json.loads(text)
    results = []
    for item in data.get("items") or []:
        results.append({
            "title": item.get("title") or "",
            "url": item.get("link") or "",
            "snippet": item.get("snippet") or "",
        })
    return "Google Custom Search", results


def _web_search_serper(query: str, count: int) -> tuple[str, list[dict]]:
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        return "", []
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        method="POST",
        data=json.dumps({"q": query, "num": max(1, min(count, 10))}).encode("utf-8"),
        headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
            "User-Agent": "Jarvis/1.0 (+local assistant)",
        },
    )
    with urllib.request.urlopen(req, timeout=12.0) as resp:
        data = json.loads(resp.read(1_000_000).decode("utf-8", "replace"))
    results = []
    for item in data.get("organic") or []:
        results.append({
            "title": item.get("title") or "",
            "url": item.get("link") or "",
            "snippet": item.get("snippet") or "",
        })
    return "Serper Google Search", results


def _web_search_brave(query: str, count: int) -> tuple[str, list[dict]]:
    api_key = os.environ.get("BRAVE_API_KEY") or os.environ.get("BRAVE_SEARCH_API_KEY")
    if not api_key:
        return "", []
    params = urllib.parse.urlencode({"q": query, "count": max(1, min(count, 20))})
    req = urllib.request.Request(
        f"https://api.search.brave.com/res/v1/web/search?{params}",
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
            "User-Agent": "Jarvis/1.0 (+local assistant)",
        },
    )
    with urllib.request.urlopen(req, timeout=12.0) as resp:
        data = json.loads(resp.read(1_000_000).decode("utf-8", "replace"))
    results = []
    for item in ((data.get("web") or {}).get("results") or []):
        results.append({
            "title": _strip_markup(item.get("title") or ""),
            "url": item.get("url") or "",
            "snippet": _strip_markup(item.get("description") or ""),
        })
    return "Brave Search", results


def _web_search_bing_rss(query: str, count: int) -> tuple[str, list[dict]]:
    params = urllib.parse.urlencode({"q": query, "format": "rss"})
    _content_type, text = _fetch_url(
        f"https://www.bing.com/search?{params}",
        12.0,
        1_000_000,
    )
    root = ET.fromstring(text)
    results = []
    for item in root.findall("./channel/item")[:count]:
        results.append({
            "title": _strip_markup(item.findtext("title") or ""),
            "url": _strip_markup(item.findtext("link") or ""),
            "snippet": _strip_markup(item.findtext("description") or ""),
            "published": _strip_markup(item.findtext("pubDate") or ""),
        })
    return "Bing RSS fallback", results


def _query_date_terms(query: str) -> tuple[str, ...]:
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,?\s+\d{4})?\b",
        query or "",
        re.I,
    )
    if not match:
        return ()
    month = match.group(1)
    day = str(int(match.group(2)))
    return (f"{month} {day}".lower(), f"{day} {month}".lower())


def _looks_like_other_date(text: str, date_terms: tuple[str, ...]) -> bool:
    if not date_terms:
        return False
    lower = (text or "").lower()
    month = date_terms[0].split()[0]
    if month not in lower:
        return False
    return not any(term in lower for term in date_terms)


def _normalise_fixture_team(name: str) -> str:
    words = [word for word in name.strip(" ,;:.()").split() if word]
    while words and words[0].upper() in {"AM", "PM", "ET", "CT", "MT", "PT"}:
        words.pop(0)
    venue_markers = {"stadium", "field", "park", "arena"}
    venue_indexes = [idx for idx, word in enumerate(words) if word.lower() in venue_markers]
    if venue_indexes:
        words = words[venue_indexes[-1] + 1:]
    if not words:
        return ""
    small = {"dr", "usa", "uk"}
    return " ".join(word.upper() if word.lower() in small else word.title() for word in words)


def _soccer_world_cup_query(query: str) -> bool:
    lower = (query or "").lower()
    return "world cup" in lower and any(term in lower for term in ("fifa", "soccer", "football"))


def _looks_like_soccer_world_cup_result(text: str) -> bool:
    lower = (text or "").lower()
    return any(term in lower for term in ("fifa", "soccer", "football"))


def _fixture_highlights(results: list[dict], query: str = "") -> list[str]:
    fixtures: list[str] = []
    date_terms = _query_date_terms(query)
    soccer_world_cup = _soccer_world_cup_query(query)
    snippet_patterns = (
        (r"(?:^|[.;]\s+|,\s+| and\s+)([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){0,3}) will face ([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){0,3})", " vs "),
        (r"(?:^|[.;]\s+|,\s+| and\s+)([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){0,3}) will take on ([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){0,3})", " vs "),
        (r"(?:^|[.;]\s+|,\s+| and\s+)([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){0,3}) will meet ([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){0,3})", " vs "),
    )
    any_text_patterns = (
        (r"\b([A-Z][A-Za-z]*(?:\s+(?:DR|[A-Z][A-Za-z]*)){0,3})\s+(?:v\.?|vs\.?|versus)\s+([A-Z][A-Za-z]*(?:\s+(?:DR|[A-Z][A-Za-z]*)){0,3})\b", " vs "),
    )
    for item in results:
        snippet = item.get("snippet") or ""
        title = item.get("title") or ""
        haystack = f"{title} {snippet}"
        if soccer_world_cup and not _looks_like_soccer_world_cup_result(haystack):
            continue
        if _looks_like_other_date(haystack, date_terms):
            continue
        for text, patterns in ((snippet, snippet_patterns), (haystack, any_text_patterns)):
            for pattern, separator in patterns:
                for left, right in re.findall(pattern, text):
                    left = _normalise_fixture_team(left)
                    right = _normalise_fixture_team(right)
                    if not left or not right or left.lower() in {"group", "stage"}:
                        continue
                    fixture = f"{left}{separator}{right}"
                    if fixture not in fixtures:
                        fixtures.append(fixture)
    return fixtures


async def web_search(args, _cfg):
    query = str(args.get("query", "")).strip()
    if not query:
        return "No web search query given."
    count = int(args.get("max_results", 5) or 5)
    count = max(1, min(count, 10))
    providers = (
        _web_search_serper,
        _web_search_google,
        _web_search_brave,
        _web_search_bing_rss,
    )
    failures = []
    for provider in providers:
        try:
            source, results = await asyncio.to_thread(provider, query, count)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ET.ParseError) as exc:
            failures.append(f"{provider.__name__}: {exc}")
            continue
        if not source:
            continue
        if not results:
            failures.append(f"{source}: no results")
            continue
        lines = [f"Search results for: {query}", f"Source: {source}"]
        fixtures = _fixture_highlights(results, query)
        if fixtures:
            lines.append(f"Likely fixtures: {'; '.join(fixtures[:6])}.")
        for idx, item in enumerate(results[:count], 1):
            title = item.get("title") or "(untitled)"
            snippet = item.get("snippet") or ""
            url = item.get("url") or ""
            published = item.get("published") or ""
            date_text = f" ({published})" if published else ""
            lines.append(f"{idx}. {title}{date_text}")
            if snippet:
                lines.append(f"   {snippet}")
            if url:
                lines.append(f"   {url}")
        if source == "Bing RSS fallback":
            lines.append("Add SERPER_API_KEY, GOOGLE_API_KEY plus GOOGLE_SEARCH_ENGINE_ID, or BRAVE_API_KEY for a sturdier API-backed search path.")
        return "\n".join(lines)
    detail = "; ".join(failures[-3:]) if failures else "no configured search provider"
    return f"Web search failed for '{query}': {detail}"


async def observe_browser(args, cfg):
    max_chars = int(args.get("max_chars", 3500) or 3500)
    automation_errors = []
    script = """
        tell application "Google Chrome"
            if (count of windows) = 0 then return ""
            set t to active tab of front window
            return (title of t) & linefeed & (URL of t)
        end tell
    """
    raw = await _run(["osascript", "-e", script], timeout=1.5)
    if raw.startswith("(timed out"):
        automation_errors.append("Chrome automation timed out")
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[-1].startswith(("http://", "https://")):
        script = """
            tell application "Safari"
                if (count of windows) = 0 then return ""
                set t to current tab of front window
                return (name of t) & linefeed & (URL of t)
            end tell
        """
        raw = await _run(["osascript", "-e", script], timeout=1.5)
        if raw.startswith("(timed out"):
            automation_errors.append("Safari automation timed out")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[-1].startswith(("http://", "https://")):
        if automation_errors:
            return (
                "I could not observe the browser because macOS browser automation did not respond. "
                + "; ".join(automation_errors)
                + "."
            )
        return "I could not find an active Chrome or Safari tab with an http(s) URL."
    title, url = lines[0], lines[-1]
    fetched = await web_fetch({"url": url, "max_chars": max_chars}, cfg)
    return f"Active browser tab: {title}\n{fetched}"

async def vault_search(args, cfg):
    query = str(args.get("query", "")).strip()
    if not query:
        return "No vault search query given."
    root = _vault_root(cfg)
    if not root.is_dir():
        return f"Vault path is not available: {root}"
    limit = int(args.get("limit", 8) or 8)
    limit = max(1, min(limit, 20))
    context = int(args.get("context", 4) or 4)
    context = max(0, min(context, 6))

    rg = _bin("rg")
    if not rg:
        return "Vault search needs ripgrep (`rg`), but it is not installed or visible."
    raw = await _run([rg, "-l", "-i", "--glob", "*.md", "--", query, str(root)], timeout=30)
    if raw == "(no output)":
        return f"No vault matches for: {query}"
    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        pattern = re.compile(re.escape(query), re.IGNORECASE)

    candidates = []
    for line in raw.splitlines():
        path = Path(line)
        if not path.is_file() or not _inside(path, root):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        candidates.append((_vault_relevance(text, pattern), path, text))
    candidates.sort(key=lambda item: item[0], reverse=True)

    parts = []
    for _score, path, text in candidates[:limit]:
        rel = path.relative_to(root)
        lines = text.splitlines()
        meta = [line for line in lines[:25] if line.startswith(("title:", "created_at:", "updated_at:"))]
        match_indexes = [i for i, line in enumerate(lines) if pattern.search(line)][:3]
        if not match_indexes:
            continue
        parts.append(f"## {rel}")
        if meta:
            parts.extend(meta)
        for idx in match_indexes:
            start = max(0, idx - context)
            end = min(len(lines), idx + context + 1)
            parts.append(f"-- lines {start + 1}-{end} --")
            for line_no in range(start, end):
                parts.append(f"{line_no + 1}: {lines[line_no]}")
    if not parts:
        return f"No readable vault matches for: {query}"
    if len(candidates) > limit:
        parts.append(f"... truncated to {limit} files")
    out = "\n".join(parts)
    return out[:12000] + ("\n... truncated" if len(out) > 12000 else "")

async def vault_read(args, cfg):
    raw_path = str(args.get("path", "")).strip()
    if not raw_path:
        return "No vault file path given."
    root = _vault_root(cfg)
    path = Path(_expand(raw_path))
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not _inside(path, root):
        return f"Refusing to read outside the vault: {raw_path}"
    if not path.is_file():
        return f"Vault file not found: {raw_path}"
    max_chars = int(args.get("max_chars", 6000) or 6000)
    max_chars = max(1000, min(max_chars, 20000))
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:max_chars] + ("\n... truncated" if len(text) > max_chars else "")

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
# Jarvis runs curated DVC Claude Code skills via `claude -p "/<id>"`.
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
    "capabilities": {"handler": capabilities, "safe": True,
        "description": "Explain what Jarvis can do right now from the actual local tool registry.",
        "parameters": {"type": "object", "properties": {}}},
    "jarvis_doctor": {"handler": jarvis_doctor, "safe": True,
        "description": "Check and repair Jarvis services: backend, overlay, Chatterbox voice, local Prometheus LM Studio, and Stormbreaker/Windows LM Studio fallback. Does not print secrets.",
        "parameters": {"type": "object", "properties": {"repair": {"type": "boolean", "description": "true to kickstart/open missing services; false to only check status"}}}},
    "web_search": {"handler": web_search, "safe": True,
        "description": "Search the live web for current information, sports fixtures, news, schedules, products, and facts that may have changed recently. Uses Serper/Google/Brave API keys when configured and a no-key Bing RSS fallback otherwise.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Search query"}, "max_results": {"type": "integer", "description": "number of results to return, default 5"}}, "required": ["query"]}},
    "weather": {"handler": weather, "safe": True,
        "description": "Get current weather and today's forecast. If no location is provided, use IP-based location.",
        "parameters": {"type": "object", "properties": {"location": {"type": "string", "description": "City/region, e.g. 'San Antonio, TX'. Optional; empty means IP-based location."}}}},
    "web_fetch": {"handler": web_fetch, "safe": True,
        "description": "Fetch and read a public http(s) web page by URL. Use this to inspect web content when the user gives a link.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "http(s) URL to read"}, "max_chars": {"type": "integer", "description": "maximum readable characters to return, default 3500"}}, "required": ["url"]}},
    "observe_browser": {"handler": observe_browser, "safe": True,
        "description": "Read the active Chrome or Safari tab URL/title and fetch readable page text. Use when the user asks what is on the current web page.",
        "parameters": {"type": "object", "properties": {"max_chars": {"type": "integer", "description": "maximum readable characters to return, default 3500"}}}},
    "vault_search": {"handler": vault_search, "safe": True,
        "description": "Search the local Obsidian vault markdown files. Use this for questions about the user's past notes, travel, meetings, memories, or saved conversations.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "ripgrep-compatible text or regex to search for"}, "limit": {"type": "integer", "description": "maximum matching lines to return, default 40"}}, "required": ["query"]}},
    "vault_read": {"handler": vault_read, "safe": True,
        "description": "Read a markdown file from the local Obsidian vault after vault_search finds a relevant path.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "vault-relative or absolute path to a markdown file"}, "max_chars": {"type": "integer", "description": "maximum characters to return, default 6000"}}, "required": ["path"]}},
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
        "description": "Run one of Jarvis's curated DVC skills by id or phrase via Claude Code (e.g. skill='dvc-eod-done-sweep' for 'run the done sweep'; 'bookmark review' -> twitter-bookmark-review). Side-effectful; the user confirms first. Unknown skills are reported back, never invented.",
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
