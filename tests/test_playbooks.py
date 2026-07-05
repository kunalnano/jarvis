"""Yennefer playbook engine tests - no real Linear calls."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from jarvis.playbooks import LinearBoardClient, LinearIssue, LinearUnavailable, PlaybookEngine, parse_playbooks


NOW = datetime(2026, 6, 13, 16, 40, tzinfo=timezone.utc)

PLAYBOOKS_MD = """
# PB-1: Chair-Call Shepherd
TRIGGER: any ticket labeled chair-call older than 24h.
HAND-UP: tray hand-up + opener: "Two decisions are waiting on you; the masthead taste pass is 26 hours old. Ninety seconds, dear."
PRE-AUTHORIZED: assemble the decision packet.
ON "GO AHEAD": walk the decisions one at a time in chat; record the chair's verdict as a ticket comment; move the card per the verdict.
STOP CONDITIONS: chair says "later" -> snooze 24h, no re-nudge before.
EVIDENCE: verdict comments on tickets; one line in next strategist brief.

# PB-2: Queue Starvation
TRIGGER: zero /queue-eligible tickets while worker capacity exists.
HAND-UP: "The belt is empty. I have three candidates worth ranking."
PRE-AUTHORIZED: draft up to 3 /brief-quality specs filed as needs-spec (never self-ranked).
ON "GO AHEAD": chair ranks in chat; she applies priorities + flips needs-spec -> spec-driven on the approved ones.
STOP CONDITIONS: chair declines twice in a week.
EVIDENCE: tickets with AUTHORED stamps citing PB-2.
"""


class FakeLinear:
    def __init__(self, chair_calls=None, queue_eligible=None, backlog=None, in_progress=None):
        self.chair_calls = chair_calls or []
        self.queue_eligible = queue_eligible or []
        self.backlog = backlog or []
        self.in_progress = in_progress or []
        self.created = []
        self.comments = []
        self.updates = []
        self.validations = []
        self.calls = []

    async def list_chair_call_issues(self, older_than):
        self.calls.append(("chair", older_than))
        return self.chair_calls

    async def list_queue_eligible_issues(self):
        self.calls.append(("queue", None))
        return self.queue_eligible

    async def list_backlog_issues(self, limit=50):
        self.calls.append(("backlog", limit))
        return self.backlog[:limit]

    async def list_in_progress_issues(self):
        self.calls.append(("active", None))
        return self.in_progress

    async def create_needs_spec_issue(self, title, description, team, project=None):
        self.created.append({
            "title": title,
            "description": description,
            "team": team,
            "project": project,
        })
        return LinearIssue(
            id=f"draft-{len(self.created)}",
            identifier=f"DAR-DRAFT-{len(self.created)}",
            title=title,
            labels=("needs-spec", "council"),
            priority=0,
            project=project or "",
        )

    async def comment_on_issue(self, issue_id, body):
        self.comments.append((issue_id, body))

    async def update_issue_state(self, issue_id, target_state, add_labels=(), remove_labels=()):
        self.updates.append((issue_id, target_state, add_labels, remove_labels))

    async def validate_issue_states(self, state_names):
        self.validations.append(tuple(state_names))


class FakeNotifier:
    def __init__(self):
        self.messages = []

    async def notify(self, text):
        self.messages.append(text)
        return {"ok": True, "output": "notified"}


def make_engine(tmp_path, fake, notifier=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    vault_path = tmp_path / "Yennefer Playbooks.md"
    vault_path.write_text(PLAYBOOKS_MD, encoding="utf-8")
    state_path = tmp_path / "state.json"
    pause_path = tmp_path / "pause"
    return PlaybookEngine(
        {
            "playbooks": {
                "vault_path": str(vault_path),
                "state_path": str(state_path),
                "pause_path": str(pause_path),
                "handoff_dir": str(tmp_path / "handoffs"),
                "interval_seconds": 900,
                "jitter_seconds": 0,
                "team": "Darkvectorcognition",
                "project": "Yennefer",
            }
        },
        client=fake,
        notifier=notifier,
        now_fn=lambda: NOW,
    )


def load_state(engine):
    return json.loads(engine.state.path.read_text(encoding="utf-8"))


class StubLinearClient(LinearBoardClient):
    def __init__(self, responses, team="Darkvectorcognition"):
        super().__init__(api_key="test", team=team)
        self.responses = responses
        self.calls = []

    async def _graphql(self, query, variables=None):
        self.calls.append((query, variables or {}))
        return self.responses.pop(0)


class FailingStateFakeLinear(FakeLinear):
    async def validate_issue_states(self, state_names):
        self.validations.append(tuple(state_names))
        raise LinearUnavailable("Linear workflow state not found: Todo")


def test_parses_playbook_opener():
    playbooks = parse_playbooks(PLAYBOOKS_MD)
    assert playbooks["PB-1"].name == "Chair-Call Shepherd"
    assert playbooks["PB-1"].opener.startswith("Two decisions are waiting")
    assert "draft up to 3" in playbooks["PB-2"].pre_authorized


def test_linear_client_fails_closed_when_team_state_is_missing():
    client = StubLinearClient([
        {
            "teams": {
                "nodes": [
                    {"id": "team-dar", "name": "Darkvectorcognition", "key": "DAR"}
                ]
            }
        },
        {"workflowStates": {"nodes": []}},
    ])

    with pytest.raises(LinearUnavailable, match="workflow state not found"):
        asyncio.run(client.update_issue_state("issue-1", "Todo"))

    assert client.calls[1][1]["teamId"] == "team-dar"


def test_pb1_chair_call_hand_up_writes_state(tmp_path):
    fake = FakeLinear(chair_calls=[
        LinearIssue(
            id="issue-1",
            identifier="DAR-99",
            title="Synthetic chair-call",
            url="https://linear.app/dar/issue/DAR-99",
            created_at="2026-06-12T10:00:00Z",
            labels=("chair-call", "council"),
            priority=1,
        )
    ])
    engine = make_engine(tmp_path, fake)

    result = asyncio.run(engine.evaluate_once())
    state = load_state(engine)

    assert result == {"status": "hand_up", "playbook_id": "PB-1"}
    assert state["active"]["playbook_id"] == "PB-1"
    assert state["active"]["status"] == "waiting_for_chair"
    assert state["hand_up"]["message"].startswith("Two decisions are waiting")
    assert state["active"]["payload"]["tickets"][0]["identifier"] == "DAR-99"


def test_pb1_go_ahead_and_later_are_state_transitions(tmp_path):
    fake = FakeLinear(chair_calls=[
        LinearIssue(
            id="issue-1",
            identifier="DAR-99",
            title="Synthetic chair-call",
            created_at="2026-06-12T10:00:00Z",
            labels=("chair-call",),
        )
    ])
    engine = make_engine(tmp_path, fake)
    asyncio.run(engine.evaluate_once())

    approved = asyncio.run(engine.handle_chat("go ahead"))
    state = load_state(engine)
    assert approved["status"] == "approved"
    assert "DAR-99" in approved["reply"]
    assert state["active"]["status"] == "approved"
    assert state["active"]["approved_at"] == "2026-06-13T16:40:00Z"

    engine = make_engine(tmp_path / "decline", fake)
    asyncio.run(engine.evaluate_once())
    snoozed = asyncio.run(engine.handle_chat("later"))
    state = load_state(engine)
    assert snoozed["status"] == "snoozed"
    assert snoozed["snooze_until"] == "2026-06-14T16:40:00Z"
    assert state["active"] is None
    assert state["snoozes"]["PB-1:DAR-99"] == "2026-06-14T16:40:00Z"

    engine = make_engine(tmp_path / "spoof", fake)
    asyncio.run(engine.evaluate_once())
    spoof = asyncio.run(engine.handle_chat("please do not go ahead"))
    state = load_state(engine)
    assert spoof["status"] == "snoozed"
    assert state["active"] is None


def test_pb2_drafts_needs_spec_candidates_without_ranking(tmp_path):
    fake = FakeLinear(chair_calls=[], queue_eligible=[])
    engine = make_engine(tmp_path, fake)

    result = asyncio.run(engine.evaluate_once())
    state = load_state(engine)

    assert result["status"] == "drafted"
    assert result["playbook_id"] == "PB-2"
    assert 1 <= len(fake.created) <= 3
    for draft in fake.created:
        assert "AUTHORED: 2026-06-13T16:40:00Z" in draft["description"]
        assert "PB-2" in draft["description"]
    assert len(state["drafts"]) == len(fake.created)
    assert all(draft["priority"] == 0 for draft in state["drafts"])
    assert all("needs-spec" in draft["labels"] for draft in state["drafts"])


def test_backlog_curator_dry_run_promotes_safe_backlog_without_linear_writes(tmp_path):
    fake = FakeLinear(
        chair_calls=[],
        backlog=[
            LinearIssue(
                id="issue-77",
                identifier="DAR-77",
                title="Backlog curator",
                state="Backlog",
                state_type="backlog",
                labels=("spec-driven", "council", "lift-M"),
                priority=1,
                project="Yennefer",
            )
        ],
        queue_eligible=[],
    )
    engine = make_engine(tmp_path, fake)

    result = asyncio.run(engine.evaluate_once())
    state = load_state(engine)

    assert result["status"] == "backlog_curator_dry_run_promotions_ready"
    assert result["playbook_id"] == "PB-3"
    assert fake.comments == []
    assert fake.updates == []
    assert fake.calls == [("chair", NOW - timedelta(hours=24)), ("backlog", 8), ("active", None)]
    curator = state["backlog_curator"]
    assert curator["status"] == "dry_run_promotions_ready"
    assert curator["preempted_pm"] is True
    assert curator["actions"][0]["classification"] == "promote:todo"
    assert curator["actions"][0]["target_state"] == "Todo"
    assert "Backlog Curator review" in curator["actions"][0]["audit_comment"]
    assert "DAR-77" in open(curator["packet_path"], encoding="utf-8").read()


def test_repeated_backlog_curator_dry_run_does_not_starve_pm_next(tmp_path):
    backlog_issue = LinearIssue(
        id="issue-77",
        identifier="DAR-77",
        title="Backlog curator",
        state="Backlog",
        state_type="backlog",
        labels=("spec-driven", "council", "lift-M"),
        priority=1,
        project="Yennefer",
    )
    next_issue = LinearIssue(
        id="issue-42",
        identifier="DAR-42",
        title="HELM Native v1",
        state="Todo",
        state_type="unstarted",
        labels=("spec-driven", "council"),
        priority=2,
        project="HELM",
    )
    fake = FakeLinear(
        chair_calls=[],
        backlog=[backlog_issue],
        queue_eligible=[next_issue],
    )
    engine = make_engine(tmp_path, fake)

    first = asyncio.run(engine.evaluate_once())
    second = asyncio.run(engine.evaluate_once())
    state = load_state(engine)

    assert first["status"] == "backlog_curator_dry_run_promotions_ready"
    assert second["status"] == "pm_next_action"
    assert second["next"] == "DAR-42"
    assert state["backlog_curator"]["status"] == "dry_run_promotions_ready"
    assert state["backlog_curator"]["preempted_pm"] is False
    assert state["pm"]["next_action"]["identifier"] == "DAR-42"


def test_backlog_curator_applies_promotions_and_preserves_labels(tmp_path):
    fake = FakeLinear(
        chair_calls=[],
        backlog=[
            LinearIssue(
                id="issue-40",
                identifier="DAR-40",
                title="Resource conductor",
                state="Backlog",
                state_type="backlog",
                labels=("spec-driven", "council", "lift-M"),
                priority=1,
                project="Yennefer",
            )
        ],
    )
    engine = make_engine(tmp_path, fake)
    engine.backlog_curator_dry_run = False

    result = asyncio.run(engine.evaluate_once())
    state = load_state(engine)

    assert result["status"] == "backlog_curator_applied"
    assert fake.validations == [("Todo",)]
    assert fake.comments[0][0] == "issue-40"
    assert "Classification: `promote:todo`" in fake.comments[0][1]
    assert "Labels preserved: spec-driven, council, lift-M" in fake.comments[0][1]
    assert fake.updates == [("issue-40", "Todo", (), ())]
    assert state["backlog_curator"]["actions"][0]["applied"] is True


def test_backlog_curator_preflights_target_state_before_commenting(tmp_path):
    fake = FailingStateFakeLinear(
        chair_calls=[],
        backlog=[
            LinearIssue(
                id="issue-40",
                identifier="DAR-40",
                title="Resource conductor",
                state="Backlog",
                state_type="backlog",
                labels=("spec-driven", "council", "lift-M"),
                priority=1,
                project="Yennefer",
            )
        ],
    )
    engine = make_engine(tmp_path, fake)
    engine.backlog_curator_dry_run = False

    result = asyncio.run(engine.evaluate_once())

    assert result["status"] == "linear_unavailable"
    assert fake.validations == [("Todo",)]
    assert fake.comments == []
    assert fake.updates == []


def test_backlog_curator_refuses_needs_spec_and_full_capacity_auto_start(tmp_path):
    fake = FakeLinear()
    engine = make_engine(tmp_path, fake)
    active = [
        LinearIssue(id=f"active-{idx}", identifier=f"DAR-{idx}", title="Active")
        for idx in range(3)
    ]

    actions = engine._classify_backlog(
        [
            LinearIssue(
                id="needs-spec",
                identifier="DAR-72",
                title="Research stub",
                state="Backlog",
                labels=("needs-spec", "council"),
                priority=1,
                project="Yennefer",
            ),
            LinearIssue(
                id="auto",
                identifier="DAR-80",
                title="Worker-ready bridge",
                state="Backlog",
                labels=("spec-driven", "council", "worker-ready"),
                priority=1,
                project="Yennefer",
            ),
        ],
        active,
        NOW,
    )

    by_id = {action.issue.identifier: action for action in actions}
    assert by_id["DAR-72"].classification == "hold:needs_spec"
    assert by_id["DAR-72"].target_state == ""
    assert by_id["DAR-80"].classification == "hold:capacity"
    assert by_id["DAR-80"].target_state == ""


def test_backlog_curator_allows_only_one_auto_start(tmp_path):
    fake = FakeLinear()
    engine = make_engine(tmp_path, fake)

    actions = engine._classify_backlog(
        [
            LinearIssue(
                id="auto-1",
                identifier="DAR-80",
                title="Bridge",
                state="Backlog",
                labels=("spec-driven", "council", "worker-ready"),
                priority=1,
                project="Yennefer",
            ),
            LinearIssue(
                id="auto-2",
                identifier="DAR-81",
                title="QA lane",
                state="Backlog",
                labels=("spec-driven", "council", "worker-ready"),
                priority=1,
                project="Yennefer",
            ),
        ],
        [],
        NOW,
    )

    assert actions[0].classification == "promote:in_progress"
    assert actions[0].target_state == "In Progress"
    assert actions[1].classification == "hold:capacity"
    assert actions[1].target_state == ""


def test_pm_governor_writes_next_action_and_worker_handoff(tmp_path):
    fake = FakeLinear(
        chair_calls=[],
        queue_eligible=[
            LinearIssue(
                id="issue-1",
                identifier="DAR-42",
                title="HELM Native v1",
                url="https://linear.app/dar/issue/DAR-42",
                created_at="2026-06-13T01:00:00Z",
                state="Todo",
                state_type="unstarted",
                labels=("spec-driven", "council"),
                priority=2,
                project="HELM",
            )
        ],
    )
    engine = make_engine(tmp_path, fake)

    result = asyncio.run(engine.evaluate_once())
    state = load_state(engine)

    assert result["status"] == "pm_next_action"
    assert result["next"] == "DAR-42"
    assert state["active"] is None
    assert state["pm"]["status"] == "next_action"
    assert state["pm"]["next_action"]["identifier"] == "DAR-42"
    handoff = state["pm"]["next_action"]["handoff_path"]
    assert "DAR-42" in handoff
    text = open(handoff, encoding="utf-8").read()
    assert "Do not treat this as a note" in text
    assert "Run the active worker flow for `DAR-42`." in text


def test_weekend_filter_skips_commercial_work_before_selecting_next_action(tmp_path):
    fake = FakeLinear(
        chair_calls=[],
        queue_eligible=[
            LinearIssue(
                id="work",
                identifier="DAR-47",
                title="Animate the DVC website with the LLM-directed engine",
                created_at="2026-06-13T01:00:00Z",
                state="Todo",
                state_type="unstarted",
                labels=("spec-driven", "council"),
                priority=1,
                project="DVC Ops",
            ),
            LinearIssue(
                id="personal",
                identifier="DAR-42",
                title="HELM Native v1",
                created_at="2026-06-13T02:00:00Z",
                state="Todo",
                state_type="unstarted",
                labels=("spec-driven", "council"),
                priority=3,
                project="HELM",
            ),
        ],
    )
    engine = make_engine(tmp_path, fake)

    result = asyncio.run(engine.evaluate_once())
    state = load_state(engine)

    assert result["status"] == "pm_next_action"
    assert result["weekend_personal_pass"] is True
    assert result["next"] == "DAR-42"
    assert state["pm"]["next_action"]["identifier"] == "DAR-42"
    assert state["pm"]["skipped_for_weekend"][0]["identifier"] == "DAR-47"


def test_chair_call_preempts_pm_queue_governor(tmp_path):
    fake = FakeLinear(
        chair_calls=[
            LinearIssue(
                id="issue-1",
                identifier="DAR-41",
                title="Panel-list sign-off",
                created_at="2026-06-12T10:00:00Z",
                labels=("chair-call", "council"),
                priority=1,
            )
        ],
        queue_eligible=[
            LinearIssue(
                id="issue-2",
                identifier="DAR-42",
                title="HELM Native v1",
                state="Todo",
                state_type="unstarted",
                labels=("spec-driven", "council"),
                priority=1,
                project="HELM",
            )
        ],
    )
    engine = make_engine(tmp_path, fake)

    result = asyncio.run(engine.evaluate_once())
    state = load_state(engine)

    assert result == {"status": "hand_up", "playbook_id": "PB-1"}
    assert state["active"]["playbook_id"] == "PB-1"
    assert state.get("pm") is None
    assert fake.calls == [("chair", NOW - timedelta(hours=24))]


def test_pause_file_halts_evaluation_without_linear_calls(tmp_path):
    fake = FakeLinear(chair_calls=[
        LinearIssue(id="issue-1", identifier="DAR-99", title="Synthetic chair-call")
    ])
    engine = make_engine(tmp_path, fake)
    engine.pause_path.write_text("", encoding="utf-8")

    result = asyncio.run(engine.evaluate_once())
    state = load_state(engine)

    assert result == {"status": "paused"}
    assert state["paused"] is True
    assert fake.calls == []

    engine.pause_path.unlink()
    result = asyncio.run(engine.evaluate_once())
    assert result["status"] == "hand_up"
    assert fake.calls[0][0] == "chair"


def test_hand_up_notifies_shortcuts_when_notifier_enabled(tmp_path):
    fake = FakeLinear(chair_calls=[
        LinearIssue(
            id="issue-1",
            identifier="DAR-99",
            title="Synthetic chair-call",
            created_at="2026-06-12T10:00:00Z",
            labels=("chair-call",),
        )
    ])
    notifier = FakeNotifier()
    engine = make_engine(tmp_path, fake, notifier=notifier)

    result = asyncio.run(engine.evaluate_once())
    state = load_state(engine)

    assert result == {"status": "hand_up", "playbook_id": "PB-1"}
    assert notifier.messages[0].startswith("Two decisions are waiting")
    assert state["last_notification"]["ok"] is True
    assert state["last_notification"]["playbook_id"] == "PB-1"
