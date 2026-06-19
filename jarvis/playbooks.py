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
import platform
import random
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import httpx
from rich.console import Console

console = Console()

DEFAULT_VAULT_PLAYBOOKS = (
    "~/Documents/ai/obsidian-vault/dvc/yennefer/Yennefer Playbooks.md"
)
DEFAULT_STATE_PATH = "~/.yennefer/state.json"
DEFAULT_PAUSE_PATH = "~/.yennefer/pause"
DEFAULT_HANDOFF_DIR = "~/.yennefer/handoffs"
DEFAULT_INTERVAL_SECONDS = 15 * 60
DEFAULT_JITTER_SECONDS = 90
DEFAULT_PM_TIMEZONE = "America/Chicago"
LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
DEFAULT_LINEAR_BRIDGE_TIMEOUT = 10.0

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
    description: str = ""
    url: str = ""
    created_at: str = ""
    updated_at: str = ""
    due_date: str = ""
    state: str = ""
    state_type: str = ""
    labels: tuple[str, ...] = ()
    comments: tuple[str, ...] = ()
    priority: int = 0
    project: str = ""


class LinearUnavailable(RuntimeError):
    """Raised when Linear is not reachable or no API key is configured."""


class LinearBoardClient:
    """Minimal Linear client. Uses a local bridge when configured, else GraphQL."""

    def __init__(
        self,
        api_key: str | None = None,
        team: str | None = None,
        bridge_url: str | None = None,
        bridge_token: str | None = None,
    ):
        self.api_key = api_key if api_key is not None else os.environ.get("LINEAR_API_KEY")
        self.team = team
        if bridge_url is None:
            bridge_url = (
                os.environ.get("YENNEFER_LINEAR_BRIDGE_URL")
                or os.environ.get("LINEAR_BRIDGE_URL")
                or ""
            )
        if bridge_token is None:
            bridge_token = (
                os.environ.get("YENNEFER_LINEAR_BRIDGE_TOKEN")
                or os.environ.get("LINEAR_BRIDGE_TOKEN")
                or ""
            )
        self.bridge_url = bridge_url.rstrip("/")
        self.bridge_token = bridge_token

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

    async def _bridge_get(self, path: str, params: dict | None = None) -> dict:
        if not self.bridge_url:
            raise LinearUnavailable("Linear bridge URL is not configured")
        async with httpx.AsyncClient(timeout=DEFAULT_LINEAR_BRIDGE_TIMEOUT) as client:
            resp = await client.get(
                f"{self.bridge_url}{path}",
                params=params or {},
                headers=self._bridge_headers(),
            )
        return self._bridge_payload(resp)

    async def _bridge_post(self, path: str, payload: dict | None = None) -> dict:
        if not self.bridge_url:
            raise LinearUnavailable("Linear bridge URL is not configured")
        async with httpx.AsyncClient(timeout=DEFAULT_LINEAR_BRIDGE_TIMEOUT) as client:
            resp = await client.post(
                f"{self.bridge_url}{path}",
                json=payload or {},
                headers=self._bridge_headers(),
            )
        return self._bridge_payload(resp)

    def _bridge_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.bridge_token:
            headers["Authorization"] = f"Bearer {self.bridge_token}"
        return headers

    @staticmethod
    def _bridge_payload(resp: httpx.Response) -> dict:
        if resp.status_code != 200:
            raise LinearUnavailable(f"Linear bridge HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise LinearUnavailable(f"Linear bridge returned invalid JSON: {exc}") from exc
        if payload.get("error"):
            raise LinearUnavailable(str(payload["error"])[:300])
        return payload

    async def list_chair_call_issues(self, older_than: datetime) -> list[LinearIssue]:
        if self.bridge_url:
            payload = await self._bridge_get(
                "/linear/chair-calls",
                {"older_than": isoformat(older_than)},
            )
            return [_issue(node) for node in payload.get("issues", [])]

        query = """
        query YenneferChairCalls($first: Int!) {
          issues(first: $first, filter: {
            labels: { name: { eq: "chair-call" } }
            state: { type: { neq: "completed" } }
          }) {
            nodes {
              id identifier title url createdAt updatedAt priority
              dueDate
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
        if self.bridge_url:
            payload = await self._bridge_get("/linear/queue-eligible")
            return [_issue(node) for node in payload.get("issues", [])]

        query = """
        query YenneferQueueCandidates($first: Int!) {
          issues(first: $first, filter: {
            state: { type: { nin: ["completed", "canceled"] } }
          }) {
            nodes {
              id identifier title url createdAt updatedAt priority
              dueDate
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

    async def list_in_review_issues(self) -> list[LinearIssue]:
        if self.bridge_url:
            payload = await self._bridge_get("/linear/in-review")
            return [_issue(node) for node in payload.get("issues", [])]

        query = """
        query YenneferReviewGovernor($first: Int!) {
          issues(first: $first, filter: {
            state: { name: { eq: "In Review" } }
          }) {
            nodes {
              id identifier title description url createdAt updatedAt priority
              dueDate
              state { name type }
              project { name }
              labels { nodes { name } }
              comments(first: 20) { nodes { body } }
            }
          }
        }
        """
        data = await self._graphql(query, {"first": 50})
        return [_issue(node) for node in _nodes(data, "issues")]

    async def create_needs_spec_issue(
        self,
        title: str,
        description: str,
        team: str,
        project: str | None = None,
    ) -> LinearIssue | None:
        if self.bridge_url:
            payload = await self._bridge_post(
                "/linear/issues/needs-spec",
                {
                    "title": title,
                    "description": description,
                    "team": team,
                    "project": project,
                },
            )
            issue = payload.get("issue")
            return _issue(issue) if issue else None

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
              dueDate
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
        if self.bridge_url:
            await self._bridge_post(
                f"/linear/issues/{issue_id}/comments",
                {"body": body},
            )
            return

        query = """
        mutation YenneferComment($issueId: String!, $body: String!) {
          commentCreate(input: { issueId: $issueId, body: $body }) { success }
        }
        """
        await self._graphql(query, {"issueId": issue_id, "body": body})


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class DesktopNotifier:
    """Fail-soft wrapper around native macOS notification and speech."""

    def __init__(self, config: dict | None = None):
        sc = (config or {}).get("shortcuts", {}) or {}
        self.enabled = bool(sc.get("notify_enabled", False))
        self.speak = bool(sc.get("notify_speak", True))
        self.timeout_seconds = float(sc.get("notify_timeout_seconds", 10))

    async def notify(self, text: str) -> dict:
        if not self.enabled:
            return {"ok": False, "skipped": "disabled"}
        if platform.system() != "Darwin":
            return {"ok": False, "skipped": "unsupported platform"}
        message = re.sub(r"\s+", " ", (text or "").strip())
        if not message:
            return {"ok": False, "skipped": "empty message"}
        commands = [
            (
                "osascript",
                "-e",
                f"display notification {_applescript_string(message)} with title \"Yennefer\"",
            )
        ]
        if self.speak:
            commands.append(("say", message))
        outputs: list[str] = []
        try:
            for command in commands:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_seconds)
                except asyncio.TimeoutError:
                    proc.kill()
                    return {"ok": False, "error": "timeout"}
                output = out.decode("utf-8", "replace").strip()
                if output:
                    outputs.append(output)
                if proc.returncode != 0:
                    return {"ok": False, "output": "\n".join(outputs)[:500]}
            return {"ok": True, "output": "\n".join(outputs)[:500]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


ShortcutNotifier = DesktopNotifier


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
        notifier: DesktopNotifier | None = None,
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
        self.handoff_dir = _expand_path(str(pc.get("handoff_dir") or DEFAULT_HANDOFF_DIR))
        self.state = PlaybookState(_expand_path(str(pc.get("state_path") or DEFAULT_STATE_PATH)))
        self.client = client or LinearBoardClient(
            team=self.team,
            bridge_url=pc.get("linear_bridge_url"),
            bridge_token=pc.get("linear_bridge_token"),
        )
        self.notifier = notifier or DesktopNotifier(config)
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        pm = config.get("pm", {}) or {}
        self.pm_timezone = str(pm.get("timezone") or DEFAULT_PM_TIMEZONE)
        self.weekend_mode = str(pm.get("weekend_mode") or "auto").lower()
        self.weekend_personal_projects = tuple(
            str(name).strip()
            for name in pm.get("weekend_personal_projects", ("Yennefer", "HELM"))
            if str(name).strip()
        )
        self.weekend_personal_keywords = tuple(
            str(word).strip().lower()
            for word in pm.get(
                "weekend_personal_keywords",
                (
                    "personal",
                    "assistant",
                    "presence",
                    "yennefer",
                    "helm native",
                    "swift native",
                    "weekend",
                ),
            )
            if str(word).strip()
        )
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
            review_issues = await self.client.list_in_review_issues()
        except LinearUnavailable as exc:
            data["last_error"] = f"Linear unavailable: {exc}"
            data["last_evaluated_at"] = isoformat(now)
            self.state.save(data)
            return {"status": "linear_unavailable"}

        review_needing_verifier = [
            issue for issue in review_issues
            if not _verifier_verdict(issue)
            and not _is_snoozed(data, "REVIEW", issue.identifier, now)
        ]
        if review_needing_verifier:
            result = self._run_review_governor(review_needing_verifier, data, now)
            data["last_evaluated_at"] = isoformat(now)
            self.state.save(data)
            return result
        data.pop("review", None)

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
            await self._notify_hand_up(data, hand_up, now)
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

        result = self._run_pm_governor(queue_eligible, data, now)
        data["last_evaluated_at"] = isoformat(now)
        self.state.save(data)
        return result

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
        await self._notify_hand_up(data, hand_up, now)
        return {"status": "drafted", "playbook_id": "PB-2", "drafted": len(created)}

    def _run_pm_governor(
        self,
        queue_eligible: list[LinearIssue],
        data: dict,
        now: datetime,
    ) -> dict:
        ranked = sorted(queue_eligible, key=_queue_rank)
        weekend = self._weekend_personal_pass(now)
        skipped: list[LinearIssue] = []
        candidates = ranked
        if weekend:
            candidates = [
                issue for issue in ranked
                if _is_weekend_personal_issue(
                    issue,
                    self.weekend_personal_projects,
                    self.weekend_personal_keywords,
                )
            ]
            skipped = [issue for issue in ranked if issue not in candidates]

        if not candidates:
            data["pm"] = {
                "status": "weekend_personal_queue_empty" if weekend else "queue_empty",
                "weekend_personal_pass": weekend,
                "last_governed_at": isoformat(now),
                "queue_eligible": len(ranked),
                "skipped_for_weekend": [_issue_packet(issue) for issue in skipped[:8]],
                "next_action": None,
            }
            return {
                "status": data["pm"]["status"],
                "queue_eligible": len(ranked),
                "skipped_for_weekend": len(skipped),
            }

        selected = candidates[0]
        handoff_path = self._write_worker_handoff(selected, ranked, skipped, weekend, now)
        next_action = {
            **_issue_packet(selected),
            "rank": 1,
            "handoff_path": str(handoff_path),
            "instruction": (
                f"Start a worker on {selected.identifier}. "
                f"Use the handoff file at {handoff_path}."
            ),
        }
        data["pm"] = {
            "status": "next_action",
            "weekend_personal_pass": weekend,
            "last_governed_at": isoformat(now),
            "queue_eligible": len(ranked),
            "queue": [_issue_packet(issue) for issue in candidates[:8]],
            "skipped_for_weekend": [_issue_packet(issue) for issue in skipped[:8]],
            "next_action": next_action,
        }
        return {
            "status": "pm_next_action",
            "next": selected.identifier,
            "queue_eligible": len(ranked),
            "weekend_personal_pass": weekend,
            "handoff_path": str(handoff_path),
        }

    def _run_review_governor(
        self,
        review_issues: list[LinearIssue],
        data: dict,
        now: datetime,
    ) -> dict:
        packets = []
        for issue in review_issues[:5]:
            packet = _review_packet(issue)
            handoff_path = self._write_verifier_handoff(issue, packet, now)
            packet["handoff_path"] = str(handoff_path)
            packets.append(packet)

        data["active"] = None
        data["hand_up"] = None
        data["review"] = {
            "status": "needs_verifier",
            "last_governed_at": isoformat(now),
            "count": len(review_issues),
            "queue": packets,
            "next_action": packets[0] if packets else None,
            "monitor_language": (
                "In Review means independent verifier needed. "
                "Done is the celebration moment."
            ),
        }
        return {
            "status": "needs_verifier",
            "count": len(review_issues),
            "next": packets[0]["identifier"] if packets else None,
            "handoff_path": packets[0]["handoff_path"] if packets else None,
        }

    def _write_verifier_handoff(
        self,
        issue: LinearIssue,
        packet: dict,
        now: datetime,
    ) -> Path:
        self.handoff_dir.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", issue.identifier or "issue")
        path = (
            self.handoff_dir
            / f"{isoformat(now).replace(':', '').replace('Z', '')}-{safe_id}-verifier.md"
        )
        lines = [
            f"# Yennefer Verifier Handoff - {issue.identifier}",
            "",
            f"Created: {isoformat(now)}",
            "",
            "## Review Gate",
            "",
            "`In Review` means independent verifier needed, not ready for chair approval.",
            "Chair approval is blocked until a verifier verdict exists.",
            "",
            "## Ticket",
            "",
            f"- ID: {issue.identifier}",
            f"- Title: {issue.title}",
            f"- Project: {issue.project or '(none)'}",
            f"- State: {issue.state or '(unknown)'}",
            f"- Priority: {issue.priority}",
            f"- URL: {issue.url or '(none)'}",
            f"- PR URL: {packet['pr_url'] or '(missing - verifier must add it)'}",
            f"- Implementer lane: {packet['implementer_lane'] or '(unknown)'}",
            f"- Recommended verifier lane: {packet['recommended_verifier_lane']}",
            f"- Reason: {packet['recommended_verifier_reason']}",
            "",
            "## Evidence Checklist",
            "",
            *[f"- [ ] {item}" for item in packet["evidence_checklist"]],
            "",
            "## Risk Flags",
            "",
        ]
        if packet["risk_flags"]:
            lines.extend(f"- {flag}" for flag in packet["risk_flags"])
        else:
            lines.append("- none")
        lines.extend([
            "",
            "## Linear Verdict Comment Template",
            "",
            packet["verdict_comment_template"],
        ])
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return path

    def _write_worker_handoff(
        self,
        selected: LinearIssue,
        ranked: list[LinearIssue],
        skipped: list[LinearIssue],
        weekend: bool,
        now: datetime,
    ) -> Path:
        self.handoff_dir.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", selected.identifier or "issue")
        path = self.handoff_dir / f"{isoformat(now).replace(':', '').replace('Z', '')}-{safe_id}.md"
        lines = [
            f"# Yennefer Worker Handoff - {selected.identifier}",
            "",
            f"Created: {isoformat(now)}",
            f"Weekend personal pass: {str(weekend).lower()}",
            "",
            "## Execute",
            "",
            f"Run the active worker flow for `{selected.identifier}`.",
            "Do not treat this as a note; pick up the card, execute the spec, and leave evidence.",
            "",
            "## Ticket",
            "",
            f"- ID: {selected.identifier}",
            f"- Title: {selected.title}",
            f"- Project: {selected.project or '(none)'}",
            f"- State: {selected.state or '(unknown)'}",
            f"- Priority: {selected.priority}",
            f"- URL: {selected.url or '(none)'}",
            "",
            "## Guardrails",
            "",
            "- Preserve Linear labels when updating cards.",
            "- Do not mark Done automatically; Done requires chair-approved merge or explicit human confirmation.",
            "- If a blocker appears, comment evidence on the card and move to chair-call/In Review as appropriate.",
            "- If the ticket touches credentials, public deploys, external comms, destructive deletion, or access controls, require visible chair approval.",
            "",
            "## Queue Snapshot",
            "",
        ]
        for index, issue in enumerate(ranked[:8], start=1):
            lines.append(
                f"{index}. {issue.identifier} - {issue.title} "
                f"[priority={issue.priority}, state={issue.state}, project={issue.project}]"
            )
        if skipped:
            lines.extend(["", "## Skipped For Weekend Personal Pass", ""])
            for issue in skipped[:8]:
                lines.append(f"- {issue.identifier} - {issue.title} ({issue.project})")
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return path

    def _weekend_personal_pass(self, now: datetime) -> bool:
        if self.weekend_mode in {"off", "false", "0", "regular"}:
            return False
        if self.weekend_mode in {"on", "true", "1", "weekend"}:
            return True
        try:
            local_now = now.astimezone(ZoneInfo(self.pm_timezone))
        except Exception:
            local_now = now
        return local_now.weekday() >= 5

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

    async def _notify_hand_up(self, data: dict, hand_up: dict, now: datetime) -> None:
        result = await self.notifier.notify(hand_up.get("opener") or "")
        data["last_notification"] = {
            "ts": isoformat(now),
            "playbook_id": hand_up.get("playbook_id"),
            **result,
        }


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
    raw_labels = node.get("labels") or ()
    if isinstance(raw_labels, dict):
        labels = tuple(
            str(label.get("name") or "")
            for label in ((raw_labels or {}).get("nodes") or [])
            if label.get("name")
        )
    else:
        labels = tuple(str(label) for label in raw_labels if str(label))

    raw_comments = node.get("comments") or ()
    if isinstance(raw_comments, dict):
        comments = tuple(
            str(comment.get("body") or "")
            for comment in ((raw_comments or {}).get("nodes") or [])
            if comment.get("body")
        )
    else:
        comments = tuple(str(comment) for comment in raw_comments if str(comment))

    state = node.get("state") or {}
    if not isinstance(state, dict):
        state = {"name": state, "type": node.get("state_type") or ""}
    project = node.get("project") or {}
    if not isinstance(project, dict):
        project = {"name": project}
    return LinearIssue(
        id=str(node.get("id") or ""),
        identifier=str(node.get("identifier") or node.get("id") or ""),
        title=str(node.get("title") or ""),
        description=str(node.get("description") or ""),
        url=str(node.get("url") or ""),
        created_at=str(node.get("createdAt") or node.get("created_at") or ""),
        updated_at=str(node.get("updatedAt") or node.get("updated_at") or ""),
        due_date=str(node.get("dueDate") or node.get("due_date") or ""),
        state=str(state.get("name") or node.get("state") or ""),
        state_type=str(state.get("type") or node.get("state_type") or ""),
        labels=labels,
        comments=comments,
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
    if issue.state_type and issue.state_type != "unstarted":
        return False
    if issue.state and issue.state.lower() not in {"todo", "to do"}:
        return False
    if issue.state_type in {"completed", "canceled"}:
        return False
    return True


def _queue_rank(issue: LinearIssue) -> tuple[int, str, str, str]:
    due = issue.due_date or "9999-12-31"
    created = issue.created_at or "9999-12-31T23:59:59Z"
    return (issue.priority, due, created, issue.identifier)


def _is_weekend_personal_issue(
    issue: LinearIssue,
    personal_projects: tuple[str, ...],
    personal_keywords: tuple[str, ...],
) -> bool:
    project = (issue.project or "").strip().lower()
    personal_project_names = {name.lower() for name in personal_projects}
    if project in personal_project_names:
        return True
    haystack = " ".join([issue.identifier, issue.title, issue.project, *issue.labels]).lower()
    return any(keyword in haystack for keyword in personal_keywords)


def _review_packet(issue: LinearIssue) -> dict:
    verdict = _verifier_verdict(issue)
    implementer = _implementer_lane(issue)
    verifier, reason = _recommended_verifier_lane(implementer)
    pr_url = _extract_pr_url(issue)
    risk_flags = []
    if not pr_url:
        risk_flags.append("missing_pr_url")
    if not implementer:
        risk_flags.append("missing_implementer_lane")
    if verdict is None:
        risk_flags.append("no_verifier_verdict")
    if implementer and verifier == implementer:
        risk_flags.append("verifier_matches_implementer")
    return {
        **_issue_packet(issue),
        "review_status": "ready_for_chair" if verdict else "needs_verifier",
        "implementer_lane": implementer,
        "recommended_verifier_lane": verifier,
        "recommended_verifier_reason": reason,
        "pr_url": pr_url,
        "verdict": verdict,
        "evidence_checklist": [
            f"Open `{issue.identifier}` and confirm the PR/diff maps to the issue scope.",
            "Run or inspect the relevant tests and paste exact output.",
            "Check for out-of-scope changes, missing evidence, and label/state drift.",
            "Verify the implementing lane is not the only reviewer.",
        ],
        "risk_flags": risk_flags,
        "verdict_comment_template": _verifier_comment_template(issue, pr_url),
    }


def _implementer_lane(issue: LinearIssue) -> str:
    for label in issue.labels:
        normalized = label.strip().lower()
        if normalized.startswith("agent:"):
            return normalized
    return ""


def _recommended_verifier_lane(implementer: str) -> tuple[str, str]:
    options = {
        "agent:claude": (
            "agent:gemini",
            "agent:gemini is the adversarial QA lane for Claude implementation work.",
        ),
        "agent:codex": (
            "agent:gemini",
            "agent:gemini provides an independent adversarial QA lane for Codex changes.",
        ),
        "agent:gemini": (
            "agent:codex",
            "agent:codex provides mechanical implementation/test review distinct from Gemini QA.",
        ),
    }
    if implementer in options:
        return options[implementer]
    return (
        "agent:gemini",
        "No implementer lane was visible, so default to the adversarial QA lane.",
    )


def _verifier_verdict(issue: LinearIssue) -> dict | None:
    for comment in reversed(issue.comments):
        upper = comment.upper()
        if "VERIFIER" not in upper and "FINAL RECOMMENDATION" not in upper:
            continue
        if re.search(r"\bREQUEST\s+CHANGES\b", upper):
            return {"recommendation": "REQUEST CHANGES", "source": comment[:500]}
        if re.search(r"\bHOLD\b|\bCHAIR\s+CALL\b", upper):
            return {"recommendation": "HOLD/CHAIR CALL", "source": comment[:500]}
        if re.search(r"\bPASS\b", upper):
            return {"recommendation": "PASS", "source": comment[:500]}
    return None


def _extract_pr_url(issue: LinearIssue) -> str:
    haystack = "\n".join([issue.description, *issue.comments])
    match = re.search(r"https://[^\s)>\"]+/pull/\d+", haystack)
    return match.group(0) if match else ""


def _verifier_comment_template(issue: LinearIssue, pr_url: str) -> str:
    return "\n".join([
        f"## Verifier Verdict - {issue.identifier}",
        "",
        f"Issue: {issue.identifier}",
        f"PR: {pr_url or '(add PR URL)'}",
        "",
        "Evidence checked:",
        "- [ ] PR/diff matches the issue scope",
        "- [ ] Relevant tests or verification output inspected",
        "- [ ] Out-of-scope changes checked",
        "- [ ] Implementer is not the only verifier",
        "",
        "Final recommendation: PASS | REQUEST CHANGES | HOLD/CHAIR CALL",
    ])


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
