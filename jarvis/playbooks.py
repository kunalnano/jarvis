"""
Playbook engine - Jarvis's approval-gated operating layer.

The engine reads playbooks from the vault, watches Linear through a
chair-provisioned LINEAR_API_KEY, raises one hand-up at a time in
~/.jarvis/state.json, and only executes ON-GO-AHEAD steps after a matching
chat approval.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import httpx
from rich.console import Console

console = Console()

DEFAULT_VAULT_PLAYBOOKS = (
    "~/Documents/ai/obsidian-vault/dvc/jarvis/Jarvis Playbooks.md"
)
DEFAULT_STATE_PATH = "~/.jarvis/state.json"
DEFAULT_PAUSE_PATH = "~/.jarvis/pause"
DEFAULT_HANDOFF_DIR = "~/.jarvis/handoffs"
DEFAULT_INTERVAL_SECONDS = 15 * 60
DEFAULT_JITTER_SECONDS = 90
DEFAULT_PM_TIMEZONE = "America/Chicago"
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
    due_date: str = ""
    state: str = ""
    state_type: str = ""
    labels: tuple[str, ...] = ()
    label_ids: tuple[str, ...] = ()
    priority: int = 0
    project: str = ""


@dataclass(frozen=True)
class CuratorAction:
    issue: LinearIssue
    classification: str
    reason: str
    target_state: str = ""
    audit_comment: str = ""
    applied: bool = False


class LinearUnavailable(RuntimeError):
    """Raised when Linear is not reachable or no API key is configured."""


class LinearBoardClient:
    """Minimal Linear GraphQL client. Reads by default; PB-2 drafts are explicit."""

    def __init__(self, api_key: str | None = None, team: str | None = None):
        self.api_key = api_key if api_key is not None else os.environ.get("LINEAR_API_KEY")
        self.team = team
        self._state_id_cache: dict[str, str] = {}
        self._team_id_cache: str | None = None

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
        team_id = await self._team_id()
        query = """
        query JarvisChairCalls($first: Int!, $teamId: String!) {
          issues(first: $first, filter: {
            team: { id: { eq: $teamId } }
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
        data = await self._graphql(query, {"first": 50, "teamId": team_id})
        issues = [_issue(node) for node in _nodes(data, "issues")]
        return [issue for issue in issues if _parse_ts(issue.created_at) <= older_than]

    async def list_queue_eligible_issues(self) -> list[LinearIssue]:
        team_id = await self._team_id()
        query = """
        query JarvisQueueCandidates($first: Int!, $teamId: String!) {
          issues(first: $first, filter: {
            team: { id: { eq: $teamId } }
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
        data = await self._graphql(query, {"first": 100, "teamId": team_id})
        issues = [_issue(node) for node in _nodes(data, "issues")]
        return [issue for issue in issues if _queue_eligible(issue)]

    async def list_backlog_issues(self, limit: int = 50) -> list[LinearIssue]:
        team_id = await self._team_id()
        query = """
        query JarvisBacklogIssues($first: Int!, $teamId: String!) {
          issues(first: $first, filter: {
            team: { id: { eq: $teamId } }
            state: { type: { eq: "backlog" } }
          }) {
            nodes {
              id identifier title url createdAt updatedAt priority
              dueDate
              state { name type }
              project { name }
              labels { nodes { id name } }
            }
          }
        }
        """
        data = await self._graphql(query, {"first": int(limit), "teamId": team_id})
        return [_issue(node) for node in _nodes(data, "issues")]

    async def list_in_progress_issues(self, limit: int = 50) -> list[LinearIssue]:
        team_id = await self._team_id()
        query = """
        query JarvisInProgressIssues($first: Int!, $teamId: String!) {
          issues(first: $first, filter: {
            team: { id: { eq: $teamId } }
            state: { type: { eq: "started" } }
          }) {
            nodes {
              id identifier title url createdAt updatedAt priority
              dueDate
              state { name type }
              project { name }
              labels { nodes { id name } }
            }
          }
        }
        """
        data = await self._graphql(query, {"first": int(limit), "teamId": team_id})
        return [_issue(node) for node in _nodes(data, "issues")]

    async def create_needs_spec_issue(
        self,
        title: str,
        description: str,
        team: str,
        project: str | None = None,
    ) -> LinearIssue | None:
        project_clause = "projectName: $project," if project else ""
        query = f"""
        mutation JarvisCreateNeedsSpec(
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
        query = """
        mutation JarvisComment($issueId: String!, $body: String!) {
          commentCreate(input: { issueId: $issueId, body: $body }) { success }
        }
        """
        await self._graphql(query, {"issueId": issue_id, "body": body})

    async def update_issue_state(
        self,
        issue_id: str,
        target_state: str,
        add_labels: tuple[str, ...] = (),
        remove_labels: tuple[str, ...] = (),
    ) -> None:
        state_id = await self._workflow_state_id(target_state)
        issue_input: dict = {"stateId": state_id}
        if add_labels or remove_labels:
            current = await self._issue_label_ids(issue_id)
            add_ids = await self._label_ids_by_name(add_labels)
            remove_ids = await self._label_ids_by_name(remove_labels)
            final = (set(current) | set(add_ids.values())) - set(remove_ids.values())
            issue_input["labelIds"] = sorted(final)
        query = """
        mutation JarvisUpdateIssue($issueId: String!, $input: IssueUpdateInput!) {
          issueUpdate(id: $issueId, input: $input) { success }
        }
        """
        await self._graphql(query, {"issueId": issue_id, "input": issue_input})

    async def validate_issue_states(self, state_names: tuple[str, ...]) -> None:
        for state_name in tuple(dict.fromkeys(state for state in state_names if state)):
            await self._workflow_state_id(state_name)

    async def _workflow_state_id(self, state_name: str) -> str:
        cache_key = state_name.strip().lower()
        if cache_key in self._state_id_cache:
            return self._state_id_cache[cache_key]
        team_id = await self._team_id()
        query = """
        query JarvisWorkflowState($name: String!, $teamId: String!) {
          workflowStates(first: 100, filter: {
            team: { id: { eq: $teamId } }
            name: { eq: $name }
          }) {
            nodes { id name team { name key } }
          }
        }
        """
        data = await self._graphql(query, {"name": state_name, "teamId": team_id})
        states = _nodes(data, "workflowStates")
        if states:
            self._state_id_cache[cache_key] = str(states[0]["id"])
            return self._state_id_cache[cache_key]
        raise LinearUnavailable(f"Linear workflow state not found: {state_name}")

    async def _team_id(self) -> str:
        if self._team_id_cache:
            return self._team_id_cache
        if not self.team:
            raise LinearUnavailable("Linear team is not configured")
        query = """
        query JarvisTeams($first: Int!) {
          teams(first: $first) {
            nodes { id name key }
          }
        }
        """
        data = await self._graphql(query, {"first": 250})
        wanted = self.team.lower()
        for team in _nodes(data, "teams"):
            if wanted in {
                str(team.get("name") or "").lower(),
                str(team.get("key") or "").lower(),
            }:
                self._team_id_cache = str(team["id"])
                return self._team_id_cache
        raise LinearUnavailable(f"Linear team not found: {self.team}")

    async def _issue_label_ids(self, issue_id: str) -> tuple[str, ...]:
        query = """
        query JarvisIssueLabels($issueId: String!) {
          issue(id: $issueId) {
            labels { nodes { id name } }
          }
        }
        """
        data = await self._graphql(query, {"issueId": issue_id})
        labels = (((data.get("issue") or {}).get("labels") or {}).get("nodes") or [])
        return tuple(str(label.get("id") or "") for label in labels if label.get("id"))

    async def _label_ids_by_name(self, names: tuple[str, ...]) -> dict[str, str]:
        clean = tuple(dict.fromkeys(name.strip() for name in names if name.strip()))
        if not clean:
            return {}
        query = """
        query JarvisLabelIds($first: Int!) {
          issueLabels(first: $first) {
            nodes { id name }
          }
        }
        """
        data = await self._graphql(query, {"first": 250})
        wanted = {name.lower() for name in clean}
        result = {}
        for label in _nodes(data, "issueLabels"):
            name = str(label.get("name") or "")
            if name.lower() in wanted and label.get("id"):
                result[name] = str(label["id"])
        missing = [name for name in clean if name.lower() not in {key.lower() for key in result}]
        if missing:
            raise LinearUnavailable(f"Linear labels not found: {', '.join(missing)}")
        return result


class ShortcutNotifier:
    """Fail-soft wrapper around macOS `shortcuts run`."""

    def __init__(self, config: dict | None = None):
        sc = (config or {}).get("shortcuts", {}) or {}
        self.enabled = bool(sc.get("notify_enabled", False))
        self.shortcut_name = str(sc.get("notify_shortcut") or "Jarvis Says")
        self.timeout_seconds = float(sc.get("notify_timeout_seconds", 10))

    async def notify(self, text: str) -> dict:
        if not self.enabled:
            return {"ok": False, "skipped": "disabled"}
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as fh:
                fh.write(text)
                temp_path = fh.name
            proc = await asyncio.create_subprocess_exec(
                "shortcuts",
                "run",
                self.shortcut_name,
                "--input-path",
                temp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_seconds)
            except asyncio.TimeoutError:
                proc.kill()
                return {"ok": False, "error": "timeout"}
            output = out.decode("utf-8", "replace").strip()
            return {"ok": proc.returncode == 0, "output": output[:500]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)


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
        notifier: ShortcutNotifier | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        config = config or {}
        pc = config.get("playbooks", {}) or {}
        self.enabled = bool(pc.get("enabled", True))
        self.interval_seconds = float(pc.get("interval_seconds", DEFAULT_INTERVAL_SECONDS))
        self.jitter_seconds = float(pc.get("jitter_seconds", DEFAULT_JITTER_SECONDS))
        self.team = str(pc.get("team") or "Darkvectorcognition")
        self.project = pc.get("project") or "Jarvis"
        self.vault_path = _expand_path(str(pc.get("vault_path") or DEFAULT_VAULT_PLAYBOOKS))
        self.pause_path = _expand_path(str(pc.get("pause_path") or DEFAULT_PAUSE_PATH))
        self.handoff_dir = _expand_path(str(pc.get("handoff_dir") or DEFAULT_HANDOFF_DIR))
        self.state = PlaybookState(_expand_path(str(pc.get("state_path") or DEFAULT_STATE_PATH)))
        self.client = client or LinearBoardClient(team=self.team)
        self.notifier = notifier or ShortcutNotifier(config)
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        pm = config.get("pm", {}) or {}
        self.pm_timezone = str(pm.get("timezone") or DEFAULT_PM_TIMEZONE)
        self.weekend_mode = str(pm.get("weekend_mode") or "auto").lower()
        self.weekend_personal_projects = tuple(
            str(name).strip()
            for name in pm.get("weekend_personal_projects", ("Jarvis", "HELM"))
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
                    "jarvis",
                    "helm native",
                    "swift native",
                    "weekend",
                ),
            )
            if str(word).strip()
        )
        curator = pm.get("backlog_curator", {}) or {}
        self.backlog_curator_enabled = bool(curator.get("enabled", True))
        self.backlog_curator_dry_run = bool(curator.get("dry_run", True))
        self.backlog_curator_batch_size = int(curator.get("batch_size", 8))
        self.worker_capacity = int(pm.get("worker_capacity", curator.get("worker_capacity", 3)))
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
                console.print(f"[dim]Jarvis playbook evaluation failed: {exc}[/dim]")
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
            await self._notify_hand_up(data, hand_up, now)
            data["last_evaluated_at"] = isoformat(now)
            self.state.save(data)
            return {"status": "hand_up", "playbook_id": "PB-1"}

        if self.backlog_curator_enabled:
            try:
                backlog = await self.client.list_backlog_issues(self.backlog_curator_batch_size)
                active_lanes = await self.client.list_in_progress_issues()
            except LinearUnavailable as exc:
                data["last_error"] = f"Linear unavailable: {exc}"
                data["last_evaluated_at"] = isoformat(now)
                self.state.save(data)
                return {"status": "linear_unavailable"}
            try:
                curator_result = await self._run_backlog_curator(
                    playbooks.get("PB-3", fallback_playbook("PB-3")),
                    backlog,
                    active_lanes,
                    data,
                    now,
                )
            except LinearUnavailable as exc:
                data["last_error"] = f"Linear unavailable: {exc}"
                data["last_evaluated_at"] = isoformat(now)
                self.state.save(data)
                return {"status": "linear_unavailable"}
            if curator_result:
                data["last_evaluated_at"] = isoformat(now)
                self.state.save(data)
                return curator_result

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
                        "recommendation": "Handle the oldest chair-call first; no merge is performed by Jarvis.",
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

    async def _run_backlog_curator(
        self,
        playbook: Playbook,
        backlog: list[LinearIssue],
        active_lanes: list[LinearIssue],
        data: dict,
        now: datetime,
    ) -> dict | None:
        if not backlog:
            data["backlog_curator"] = {
                "status": "empty",
                "playbook_id": "PB-3",
                "last_reviewed_at": isoformat(now),
                "backlog_count": 0,
                "active_lanes": len(active_lanes),
                "dry_run": self.backlog_curator_dry_run,
                "actions": [],
            }
            return None

        actions = self._classify_backlog(backlog, active_lanes, now)
        packet_path = self._write_curator_packet(actions, active_lanes, now)
        promotable = [action for action in actions if action.target_state]
        signature = _curator_signature(actions)
        previous_signature = (data.get("backlog_curator") or {}).get("preempt_signature")
        applied = 0
        if promotable and not self.backlog_curator_dry_run:
            await self.client.validate_issue_states(
                tuple(action.target_state for action in promotable)
            )
            applied_ids = set()
            for action in promotable:
                await self.client.comment_on_issue(action.issue.id, action.audit_comment)
                await self.client.update_issue_state(action.issue.id, action.target_state)
                applied += 1
                applied_ids.add(action.issue.id)
            actions = [
                replace(action, applied=True) if action.issue.id in applied_ids else action
                for action in actions
            ]

        snapshot = {
            "status": "review_ready" if promotable else "reviewed_no_promotions",
            "playbook_id": "PB-3",
            "playbook_name": playbook.name,
            "last_reviewed_at": isoformat(now),
            "dry_run": self.backlog_curator_dry_run,
            "backlog_count": len(backlog),
            "active_lanes": len(active_lanes),
            "worker_capacity": self.worker_capacity,
            "packet_path": str(packet_path),
            "promotable_count": len(promotable),
            "applied_count": applied,
            "preempt_signature": signature,
            "actions": [_curator_action_packet(action) for action in actions],
        }
        if promotable and self.backlog_curator_dry_run:
            snapshot["status"] = "dry_run_promotions_ready"
        elif applied:
            snapshot["status"] = "applied"
        repeated_dry_run = (
            bool(promotable)
            and self.backlog_curator_dry_run
            and previous_signature == signature
        )
        snapshot["preempted_pm"] = bool(promotable and not repeated_dry_run)
        data["backlog_curator"] = snapshot
        data.setdefault("events", []).append({
            "ts": isoformat(now),
            "playbook_id": "PB-3",
            "event": snapshot["status"],
            "promotable_count": len(promotable),
            "applied_count": applied,
            "preempted_pm": snapshot["preempted_pm"],
        })
        if promotable and not repeated_dry_run:
            return {
                "status": f"backlog_curator_{snapshot['status']}",
                "playbook_id": "PB-3",
                "promotable_count": len(promotable),
                "applied_count": applied,
                "dry_run": self.backlog_curator_dry_run,
                "packet_path": str(packet_path),
            }
        return None

    def _classify_backlog(
        self,
        backlog: list[LinearIssue],
        active_lanes: list[LinearIssue],
        now: datetime,
    ) -> list[CuratorAction]:
        ranked = sorted(backlog, key=_queue_rank)
        active_count = len(active_lanes)
        auto_start_used = False
        actions = []
        weekend = self._weekend_personal_pass(now)
        for issue in ranked:
            action, active_count, auto_start_used = self._classify_backlog_issue(
                issue,
                active_count,
                auto_start_used,
                weekend,
                now,
            )
            actions.append(action)
        return actions

    def _classify_backlog_issue(
        self,
        issue: LinearIssue,
        active_count: int,
        auto_start_used: bool,
        weekend: bool,
        now: datetime,
    ) -> tuple[CuratorAction, int, bool]:
        labels = {label.lower() for label in issue.labels}
        if weekend and not _is_weekend_personal_issue(
            issue,
            self.weekend_personal_projects,
            self.weekend_personal_keywords,
        ):
            return (
                self._curator_action(
                    issue,
                    "hold:weekend",
                    "Weekend personal pass is active; this card is not in the weekend-safe project or keyword set.",
                    now,
                ),
                active_count,
                auto_start_used,
            )
        if "needs-spec" in labels:
            return (
                self._curator_action(
                    issue,
                    "hold:needs_spec",
                    "Protected needs-spec label is present; curator cannot make it queue eligible.",
                    now,
                ),
                active_count,
                auto_start_used,
            )
        if "chair-call" in labels:
            return (
                self._curator_action(
                    issue,
                    "hold:chair_call",
                    "Chair-call label requires explicit chair decision before promotion.",
                    now,
                ),
                active_count,
                auto_start_used,
            )
        if "blocked" in labels or issue.state.lower() == "blocked":
            return (
                self._curator_action(
                    issue,
                    "hold:blocked",
                    "Blocked cards stay out of worker queue until evidence clears the blocker.",
                    now,
                ),
                active_count,
                auto_start_used,
            )
        if "spec-driven" not in labels or "council" not in labels or issue.priority == 0:
            return (
                self._curator_action(
                    issue,
                    "hold:needs_spec",
                    "Promotion requires spec-driven and council labels plus a nonzero priority.",
                    now,
                ),
                active_count,
                auto_start_used,
            )

        wants_auto_start = bool(labels & {"auto-start", "start-now", "worker-ready"})
        if wants_auto_start:
            if auto_start_used or active_count >= self.worker_capacity:
                return (
                    self._curator_action(
                        issue,
                        "hold:capacity",
                        "Auto-start requested, but worker capacity is full or this pass already selected one starter.",
                        now,
                    ),
                    active_count,
                    auto_start_used,
                )
            active_count += 1
            auto_start_used = True
            return (
                self._curator_action(
                    issue,
                    "promote:in_progress",
                    "Spec-driven council card is worker-ready and capacity is available.",
                    now,
                    target_state="In Progress",
                ),
                active_count,
                auto_start_used,
            )

        return (
            self._curator_action(
                issue,
                "promote:todo",
                "Spec-driven council card is unblocked and safe to make queue eligible.",
                now,
                target_state="Todo",
            ),
            active_count,
            auto_start_used,
        )

    def _curator_action(
        self,
        issue: LinearIssue,
        classification: str,
        reason: str,
        now: datetime,
        target_state: str = "",
    ) -> CuratorAction:
        action = CuratorAction(
            issue=issue,
            classification=classification,
            reason=reason,
            target_state=target_state,
        )
        return CuratorAction(
            issue=issue,
            classification=classification,
            reason=reason,
            target_state=target_state,
            audit_comment=_curator_comment(action, now, self.backlog_curator_dry_run),
        )

    def _write_curator_packet(
        self,
        actions: list[CuratorAction],
        active_lanes: list[LinearIssue],
        now: datetime,
    ) -> Path:
        self.handoff_dir.mkdir(parents=True, exist_ok=True)
        path = self.handoff_dir / f"{isoformat(now).replace(':', '').replace('Z', '')}-PB-3-backlog-curator.json"
        payload = {
            "created_at": isoformat(now),
            "playbook_id": "PB-3",
            "dry_run": self.backlog_curator_dry_run,
            "worker_capacity": self.worker_capacity,
            "active_lanes": [_issue_packet(issue) for issue in active_lanes],
            "actions": [_curator_action_packet(action) for action in actions],
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

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
            f"# Jarvis Worker Handoff - {selected.identifier}",
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
                "Backfill Jarvis runbook evidence expectations.",
                "Define next small HELM native shell acceptance pass.",
            ]
        candidates = []
        for index, source in enumerate(sources[:3], start=1):
            title = _candidate_title(source, index)
            candidates.append({
                "title": title,
                "description": (
                    f"AUTHORED: {stamp}\n"
                    "SOURCE: Jarvis PB-2 Queue Starvation\n\n"
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
    if pb_id == "PB-3":
        return Playbook(
            id="PB-3",
            name="Backlog Curator",
            hand_up='"I found backlog cards that are safe to promote; I have the audit trail ready."',
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
    label_nodes = ((node.get("labels") or {}).get("nodes") or [])
    labels = tuple(str(label.get("name") or "") for label in label_nodes if label.get("name"))
    label_ids = tuple(str(label.get("id") or "") for label in label_nodes if label.get("id"))
    state = node.get("state") or {}
    project = node.get("project") or {}
    return LinearIssue(
        id=str(node.get("id") or ""),
        identifier=str(node.get("identifier") or node.get("id") or ""),
        title=str(node.get("title") or ""),
        url=str(node.get("url") or ""),
        created_at=str(node.get("createdAt") or node.get("created_at") or ""),
        updated_at=str(node.get("updatedAt") or node.get("updated_at") or ""),
        due_date=str(node.get("dueDate") or node.get("due_date") or ""),
        state=str(state.get("name") or node.get("state") or ""),
        state_type=str(state.get("type") or node.get("state_type") or ""),
        labels=labels,
        label_ids=label_ids,
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


def _curator_action_packet(action: CuratorAction) -> dict:
    return {
        "issue": _issue_packet(action.issue),
        "classification": action.classification,
        "reason": action.reason,
        "target_state": action.target_state,
        "audit_comment": action.audit_comment,
        "applied": action.applied,
    }


def _curator_signature(actions: list[CuratorAction]) -> str:
    parts = [
        f"{action.issue.identifier}:{action.classification}:{action.target_state}"
        for action in actions
    ]
    return "|".join(parts)


def _curator_comment(action: CuratorAction, now: datetime, dry_run: bool) -> str:
    issue = action.issue
    target = action.target_state or "no state change"
    mode = "dry-run proposal" if dry_run else "applied action"
    labels = ", ".join(issue.labels) if issue.labels else "(none)"
    return (
        f"Backlog Curator review - {isoformat(now)}\n\n"
        f"Mode: {mode}\n"
        f"Classification: `{action.classification}`\n"
        f"Proposed move: `{target}`\n"
        f"Reason: {action.reason}\n"
        f"Labels preserved: {labels}\n\n"
        "Safety: Jarvis never marks Done, never merges, and never strips protected labels during this pass."
    )


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
    briefs = _expand_path("~/Documents/ai/obsidian-vault/dvc/jarvis/briefs")
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
