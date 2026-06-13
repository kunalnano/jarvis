"""
Playbook engine - Yennefer's approval-gated operating layer.

The engine reads playbooks from the vault, watches Linear through a
chair-provisioned LINEAR_API_KEY, raises one hand-up at a time in
~/.yennefer/state.json, and only executes ON-GO-AHEAD steps after a matching
chat approval.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx
from rich.console import Console

console = Console()

DEFAULT_VAULT_PLAYBOOKS = (
    "~/Documents/ai/obsidian-vault/dvc/yennefer/Yennefer Playbooks.md"
)
DEFAULT_STATE_PATH = "~/.yennefer/state.json"
DEFAULT_PAUSE_PATH = "~/.yennefer/pause"
DEFAULT_INTERVAL_SECONDS = 15 * 60
DEFAULT_JITTER_SECONDS = 90
LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

GO_AHEAD_PHRASES = (
    "go ahead",
    "go-ahead",
    "yes go ahead",
    "do it",
    "approved",
)
DECLINE_PHRASES = (
    "later",
    "not now",
    "snooze",
    "leave it",
    "decline",
    "don't go ahead",
    "do not go ahead",
    "no go ahead",
)


@dataclass(frozen=True)
class Playbook:
    id: str
    name: str
    trigger: str = ""
    hand_up: str = ""
    pre_authorized: str = ""
    on_go_ahead: str = ""
    stop_conditions: str = ""
    evidence: str = ""

    @property
    def opener(self) -> str:
        match = re.search(r'opener:\s*"([^"]+)"', self.hand_up, flags=re.I)
        if match:
            return match.group(1).strip()
        return self.hand_up.strip()


@dataclass(frozen=True)
class LinearIssue:
    id: str
    identifier: str
    title: str
    url: str = ""
    created_at: str = ""
    updated_at: str = ""
    state: str = ""
    state_type: str = ""
    labels: tuple[str, ...] = ()
    priority: int = 0
    project: str = ""


class LinearUnavailable(RuntimeError):
    """Raised when Linear is not reachable or no API key is configured."""


class LinearBoardClient:
    """Minimal Linear GraphQL client. Reads by default; PB-2 drafts are explicit."""

    def __init__(self, api_key: str | None = None, team: str | None = None):
        self.api_key = api_key if api_key is not None else os.environ.get("LINEAR_API_KEY")
        self.team = team

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        if not self.api_key:
            raise LinearUnavailable("LINEAR_API_KEY is not configured")
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                LINEAR_GRAPHQL_URL,
                headers={
                    "Authorization": self.api_key,
                    "Content-Type": "application/json",
                },
                json={"query": query, "variables": variables or {}},
            )
        if resp.status_code != 200:
            raise LinearUnavailable(f"Linear HTTP {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        if payload.get("errors"):
            raise LinearUnavailable(str(payload["errors"])[:300])
        return payload.get("data") or {}

    async def list_chair_call_issues(self, older_than: datetime) -> list[LinearIssue]:
        query = """
        query YenneferChairCalls($first: Int!) {
          issues(first: $first, filter: {
            labels: { name: { eq: "chair-call" } }
            state: { type: { neq: "completed" } }
          }) {
            nodes {
              id identifier title url createdAt updatedAt priority
              state { name type }
              project { name }
              labels { nodes { name } }
            }
          }
        }
        """
        data = await self._graphql(query, {"first": 50})
        issues = [_issue(node) for node in _nodes(data, "issues")]
        return [issue for issue in issues if _parse_ts(issue.created_at) <= older_than]

    async def list_queue_eligible_issues(self) -> list[LinearIssue]:
        query = """
        query YenneferQueueCandidates($first: Int!) {
          issues(first: $first, filter: {
            state: { type: { nin: ["completed", "canceled"] } }
          }) {
            nodes {
              id identifier title url createdAt updatedAt priority
              state { name type }
              project { name }
              labels { nodes { name } }
            }
          }
        }
        """
        data = await self._graphql(query, {"first": 100})
        issues = [_issue(node) for node in _nodes(data, "issues")]
        return [issue for issue in issues if _queue_eligible(issue)]

    async def create_needs_spec_issue(
        self,
        title: str,
        description: str,
        team: str,
        project: str | None = None,
    ) -> LinearIssue | None:
        project_clause = "projectName: $project," if project else ""
        query = f"""
        mutation YenneferCreateNeedsSpec(
          $team: String!
          $project: String
          $title: String!
          $description: String!
        ) {{
          issueCreate(input: {{
            teamName: $team,
            {project_clause}
            title: $title,
            description: $description,
            priority: 0,
            labelNames: ["needs-spec", "council"]
          }}) {{
            success
            issue {{
              id identifier title url createdAt updatedAt priority
              state {{ name type }}
              project {{ name }}
              labels {{ nodes {{ name }} }}
            }}
          }}
        }}
        """
        variables = {
            "team": team,
            "project": project,
            "title": title,
            "description": description,
        }
        data = await self._graphql(query, variables)
        node = ((data.get("issueCreate") or {}).get("issue") or None)
        return _issue(node) if node else None

    async def comment_on_issue(self, issue_id: str, body: str) -> None:
        query = """
        mutation YenneferComment($issueId: String!, $body: String!) {
          commentCreate(input: { issueId: $issueId, body: $body }) { success }
        }
        """
        await self._graphql(query, {"issueId": issue_id, "body": body})


class PlaybookState:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        if not self.path.exists():
            return {
                "version": 1,
                "active": None,
                "queue": [],
                "snoozes": {},
                "drafts": [],
                "events": [],
            }
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data.setdefault("version", 1)
                data.setdefault("active", None)
                data.setdefault("queue", [])
                data.setdefault("snoozes", {})
                data.setdefault("drafts", [])
                data.setdefault("events", [])
                return data
        except Exception:
            pass
        return {
            "version": 1,
            "active": None,
            "queue": [],
            "snoozes": {},
            "drafts": [],
            "events": [{"level": "warning", "message": "state file was unreadable"}],
        }

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = utc_now()
        fd, tmp_name = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


class PlaybookEngine:
    """Evaluate playbook triggers and handle approval/decline phrases."""

    def __init__(
        self,
        config: dict | None = None,
        client: LinearBoardClient | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        config = config or {}
        pc = config.get("playbooks", {}) or {}
        self.enabled = bool(pc.get("enabled", True))
        self.interval_seconds = float(pc.get("interval_seconds", DEFAULT_INTERVAL_SECONDS))
        self.jitter_seconds = float(pc.get("jitter_seconds", DEFAULT_JITTER_SECONDS))
        self.team = str(pc.get("team") or "Darkvectorcognition")
        self.project = pc.get("project") or "Yennefer"
        self.vault_path = _expand_path(str(pc.get("vault_path") or DEFAULT_VAULT_PLAYBOOKS))
        self.pause_path = _expand_path(str(pc.get("pause_path") or DEFAULT_PAUSE_PATH))
        self.state = PlaybookState(_expand_path(str(pc.get("state_path") or DEFAULT_STATE_PATH)))
        self.client = client or LinearBoardClient(team=self.team)
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if not self.enabled:
            return
        self._tasks.append(asyncio.create_task(self._loop()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _loop(self) -> None:
        await asyncio.sleep(random.uniform(0, self.jitter_seconds))
        while True:
            try:
                await self.evaluate_once()
            except Exception as exc:
                console.print(f"[dim]Yennefer playbook evaluation failed: {exc}[/dim]")
            delay = self.interval_seconds + random.uniform(0, self.jitter_seconds)
            await asyncio.sleep(delay)

    async def evaluate_once(self) -> dict:
        data = self.state.load()
        now = self.now_fn()
        data["paused"] = self.pause_path.exists()
        if data["paused"]:
            data["last_pause_seen_at"] = isoformat(now)
            self.state.save(data)
            return {"status": "paused"}

        if _is_waiting(data.get("active")):
            data["last_evaluated_at"] = isoformat(now)
            self.state.save(data)
            return {"status": "active_hand_up"}

        playbooks = parse_playbooks(self.vault_path.read_text(encoding="utf-8"))
        try:
            chair_calls = await self.client.list_chair_call_issues(now - timedelta(hours=24))
        except LinearUnavailable as exc:
            data["last_error"] = f"Linear unavailable: {exc}"
            data["last_evaluated_at"] = isoformat(now)
            self.state.save(data)
            return {"status": "linear_unavailable"}

        eligible_chair_calls = [
            issue for issue in chair_calls
            if not _is_snoozed(data, "PB-1", issue.identifier, now)
        ]
        if eligible_chair_calls:
            hand_up = self._build_pb1_hand_up(
                playbooks.get("PB-1", fallback_playbook("PB-1")),
                eligible_chair_calls,
                now,
            )
            data["active"] = hand_up
            data["hand_up"] = _overlay_hand_up(hand_up)
            data["last_evaluated_at"] = isoformat(now)
            self.state.save(data)
            return {"status": "hand_up", "playbook_id": "PB-1"}

        try:
            queue_eligible = await self.client.list_queue_eligible_issues()
        except LinearUnavailable as exc:
            data["last_error"] = f"Linear unavailable: {exc}"
            data["last_evaluated_at"] = isoformat(now)
            self.state.save(data)
            return {"status": "linear_unavailable"}

        if not queue_eligible:
            result = await self._run_pb2(playbooks.get("PB-2", fallback_playbook("PB-2")), data, now)
            data["last_evaluated_at"] = isoformat(now)
            self.state.save(data)
            return result

        data["last_evaluated_at"] = isoformat(now)
        self.state.save(data)
        return {"status": "idle", "queue_eligible": len(queue_eligible)}

    async def handle_chat(self, message: str) -> dict | None:
        data = self.state.load()
        active = data.get("active")
        if not _is_waiting(active):
            return None
        normalized = _normalize(message)
        now = self.now_fn()
        if _is_decline(normalized):
            for issue in active.get("payload", {}).get("tickets", []):
                key = f"{active['playbook_id']}:{issue.get('identifier')}"
                data.setdefault("snoozes", {})[key] = isoformat(now + timedelta(hours=24))
            active["status"] = "snoozed"
            active["snoozed_at"] = isoformat(now)
            active["snooze_until"] = isoformat(now + timedelta(hours=24))
            data["active"] = None
            data["hand_up"] = None
            data.setdefault("events", []).append({
                "ts": isoformat(now),
                "playbook_id": active["playbook_id"],
                "event": "declined",
                "snooze_until": active["snooze_until"],
            })
            self.state.save(data)
            return {
                "status": "snoozed",
                "reply": "As you wish. I will leave that alone for twenty-four hours.",
                "snooze_until": active["snooze_until"],
            }
        if _is_approval(normalized):
            active["status"] = "approved"
            active["approved_at"] = isoformat(now)
            data["active"] = active
            data["hand_up"] = _overlay_hand_up(active)
            data.setdefault("events", []).append({
                "ts": isoformat(now),
                "playbook_id": active["playbook_id"],
                "event": "approved",
            })
            self.state.save(data)
            return {
                "status": "approved",
                "reply": self._approval_reply(active),
                "active": active,
            }
        return None

    def _build_pb1_hand_up(
        self,
        playbook: Playbook,
        issues: list[LinearIssue],
        now: datetime,
    ) -> dict:
        packet = [_issue_packet(issue) for issue in issues[:5]]
        return {
            "id": f"PB-1:{','.join(i['identifier'] for i in packet)}:{isoformat(now)}",
            "playbook_id": "PB-1",
            "playbook_name": playbook.name,
            "status": "waiting_for_chair",
            "created_at": isoformat(now),
            "opener": playbook.opener,
            "pre_authorized": playbook.pre_authorized,
            "on_go_ahead": playbook.on_go_ahead,
            "payload": {
                "tickets": packet,
                "decision_packet": [
                    {
                        "identifier": issue["identifier"],
                        "question": f"What verdict should I record for {issue['identifier']}?",
                        "recommendation": "Handle the oldest chair-call first; no merge is performed by Yennefer.",
                    }
                    for issue in packet
                ],
            },
        }

    async def _run_pb2(self, playbook: Playbook, data: dict, now: datetime) -> dict:
        candidates = self._pb2_candidates(now)
        created = []
        for candidate in candidates[:3]:
            try:
                issue = await self.client.create_needs_spec_issue(
                    candidate["title"],
                    candidate["description"],
                    team=self.team,
                    project=self.project,
                )
            except LinearUnavailable as exc:
                data["last_error"] = f"Linear unavailable: {exc}"
                return {"status": "linear_unavailable"}
            if issue:
                created.append(_issue_packet(issue))
        data["drafts"] = created
        hand_up = {
            "id": f"PB-2:{isoformat(now)}",
            "playbook_id": "PB-2",
            "playbook_name": playbook.name,
            "status": "waiting_for_chair",
            "created_at": isoformat(now),
            "opener": playbook.opener,
            "pre_authorized": playbook.pre_authorized,
            "on_go_ahead": playbook.on_go_ahead,
            "payload": {"drafted_tickets": created},
        }
        data["active"] = hand_up
        data["hand_up"] = _overlay_hand_up(hand_up)
        return {"status": "drafted", "playbook_id": "PB-2", "drafted": len(created)}

    def _pb2_candidates(self, now: datetime) -> list[dict]:
        stamp = isoformat(now)
        sources = _recent_proposed_tickets()
        if not sources:
            sources = [
                "Clarify next weekend personal-infrastructure slice for TAS.",
                "Backfill Yennefer runbook evidence expectations.",
                "Define next small HELM native shell acceptance pass.",
            ]
        candidates = []
        for index, source in enumerate(sources[:3], start=1):
            title = _candidate_title(source, index)
            candidates.append({
                "title": title,
                "description": (
                    f"AUTHORED: {stamp}\n"
                    "SOURCE: Yennefer PB-2 Queue Starvation\n\n"
                    "PB-2 detected zero queue-eligible tickets while worker capacity existed. "
                    "This is a needs-spec candidate only; it is deliberately unranked and must "
                    "be chair-ranked before work begins.\n\n"
                    f"Candidate basis: {source}\n"
                ),
            })
        return candidates

    def _approval_reply(self, active: dict) -> str:
        if active.get("playbook_id") == "PB-1":
            tickets = active.get("payload", {}).get("tickets", [])
            if not tickets:
                return "Approved. There is no decision packet left to walk."
            first = tickets[0]
            return (
                f"First decision: {first['identifier']} — {first['title']}. "
                "Tell me the verdict to record; I will not merge anything."
            )
        if active.get("playbook_id") == "PB-2":
            count = len(active.get("payload", {}).get("drafted_tickets", []))
            return f"Approved. I drafted {count} needs-spec candidates. Rank only the ones you want built."
        return "Approved. I am ready to walk the next step."


def parse_playbooks(markdown: str) -> dict[str, Playbook]:
    sections = re.split(r"(?m)^# (PB-\d+):\s*(.+)$", markdown)
    playbooks: dict[str, Playbook] = {}
    for index in range(1, len(sections), 3):
        pb_id = sections[index].strip()
        name = sections[index + 1].strip()
        body = sections[index + 2]
        fields = _parse_fields(body)
        playbooks[pb_id] = Playbook(
            id=pb_id,
            name=name,
            trigger=fields.get("TRIGGER", ""),
            hand_up=fields.get("HAND-UP", ""),
            pre_authorized=fields.get("PRE-AUTHORIZED", ""),
            on_go_ahead=fields.get('ON "GO AHEAD"', ""),
            stop_conditions=fields.get("STOP CONDITIONS", ""),
            evidence=fields.get("EVIDENCE", ""),
        )
    return playbooks


def fallback_playbook(pb_id: str) -> Playbook:
    if pb_id == "PB-1":
        return Playbook(
            id="PB-1",
            name="Chair-Call Shepherd",
            hand_up='opener: "Two decisions are waiting on you; ninety seconds, dear."',
        )
    if pb_id == "PB-2":
        return Playbook(
            id="PB-2",
            name="Queue Starvation",
            hand_up='"The belt is empty. I have three candidates worth ranking."',
        )
    return Playbook(id=pb_id, name=pb_id)


def utc_now() -> str:
    return isoformat(datetime.now(timezone.utc))


def isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_ts(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _parse_fields(body: str) -> dict[str, str]:
    names = (
        "TRIGGER",
        "HAND-UP",
        "PRE-AUTHORIZED",
        'ON "GO AHEAD"',
        "STOP CONDITIONS",
        "EVIDENCE",
    )
    pattern = r"(?m)^(" + "|".join(re.escape(name) for name in names) + r"):\s*"
    parts = re.split(pattern, body)
    fields: dict[str, str] = {}
    for index in range(1, len(parts), 2):
        name = parts[index]
        value = parts[index + 1]
        fields[name] = " ".join(line.strip() for line in value.strip().splitlines()).strip()
    return fields


def _issue(node: dict | None) -> LinearIssue:
    node = node or {}
    labels = tuple(
        str(label.get("name") or "")
        for label in ((node.get("labels") or {}).get("nodes") or [])
        if label.get("name")
    )
    state = node.get("state") or {}
    project = node.get("project") or {}
    return LinearIssue(
        id=str(node.get("id") or ""),
        identifier=str(node.get("identifier") or node.get("id") or ""),
        title=str(node.get("title") or ""),
        url=str(node.get("url") or ""),
        created_at=str(node.get("createdAt") or node.get("created_at") or ""),
        updated_at=str(node.get("updatedAt") or node.get("updated_at") or ""),
        state=str(state.get("name") or node.get("state") or ""),
        state_type=str(state.get("type") or node.get("state_type") or ""),
        labels=labels,
        priority=int(node.get("priority") or 0),
        project=str(project.get("name") or node.get("project") or ""),
    )


def _nodes(data: dict, key: str) -> list[dict]:
    return list(((data.get(key) or {}).get("nodes") or []))


def _queue_eligible(issue: LinearIssue) -> bool:
    labels = {label.lower() for label in issue.labels}
    if "chair-call" in labels or "needs-spec" in labels:
        return False
    if "spec-driven" not in labels or "council" not in labels:
        return False
    if issue.priority == 0:
        return False
    if issue.state_type in {"completed", "canceled"}:
        return False
    return True


def _is_waiting(active: dict | None) -> bool:
    return bool(active and active.get("status") == "waiting_for_chair")


def _is_snoozed(data: dict, playbook_id: str, issue_id: str, now: datetime) -> bool:
    until = (data.get("snoozes") or {}).get(f"{playbook_id}:{issue_id}")
    return bool(until and _parse_ts(until) > now)


def _issue_packet(issue: LinearIssue) -> dict:
    return {
        "id": issue.id,
        "identifier": issue.identifier,
        "title": issue.title,
        "url": issue.url,
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
        "state": issue.state,
        "labels": list(issue.labels),
        "priority": issue.priority,
        "project": issue.project,
    }


def _overlay_hand_up(active: dict) -> dict:
    return {
        "active": active.get("status") in {"waiting_for_chair", "approved"},
        "id": active.get("id"),
        "playbook_id": active.get("playbook_id"),
        "playbook_name": active.get("playbook_name"),
        "status": active.get("status"),
        "message": active.get("opener"),
        "opener": active.get("opener"),
        "created_at": active.get("created_at"),
        "approved_at": active.get("approved_at"),
        "snooze_until": active.get("snooze_until"),
        "payload": active.get("payload") or {},
    }


def _normalize(message: str) -> str:
    return re.sub(r"\s+", " ", message.lower()).strip()


def _is_decline(normalized: str) -> bool:
    candidate = normalized[7:] if normalized.startswith("please ") else normalized
    return any(
        candidate == phrase or candidate.startswith(f"{phrase} ")
        for phrase in DECLINE_PHRASES
    )


def _is_approval(normalized: str) -> bool:
    if _is_decline(normalized):
        return False
    return bool(
        re.match(
            r"^(yes[,\s]+)?(please\s+)?(go ahead|go-ahead|do it|approved)([.!?\s].*)?$",
            normalized,
        )
    )


def _expand_path(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def _candidate_title(source: str, index: int) -> str:
    cleaned = re.sub(r"^[-*]\s*", "", source).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = f"PB-2 candidate {index}"
    if len(cleaned) > 80:
        cleaned = cleaned[:77].rstrip() + "..."
    return cleaned


def _recent_proposed_tickets() -> list[str]:
    briefs = _expand_path("~/Documents/ai/obsidian-vault/dvc/yennefer/briefs")
    if not briefs.exists():
        return []
    try:
        latest = sorted(briefs.glob("*-strategist-brief.md"))[-1]
    except IndexError:
        return []
    try:
        text = latest.read_text(encoding="utf-8")
    except Exception:
        return []
    match = re.search(r"(?ms)^## PROPOSED TICKETS\s*(.+?)(?:^## |\Z)", text)
    if not match:
        return []
    lines = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith(("-", "*")):
            lines.append(stripped)
    return lines
