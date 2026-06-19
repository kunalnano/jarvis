"""Yennefer playbook engine tests - no real Linear calls."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from jarvis.playbooks import (
    DesktopNotifier,
    LinearBoardClient,
    LinearIssue,
    PlaybookEngine,
    parse_playbooks,
)


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
    def __init__(self, chair_calls=None, queue_eligible=None, in_review=None):
        self.chair_calls = chair_calls or []
        self.queue_eligible = queue_eligible or []
        self.in_review = in_review or []
        self.created = []
        self.calls = []

    async def list_chair_call_issues(self, older_than):
        self.calls.append(("chair", older_than))
        return self.chair_calls

    async def list_in_review_issues(self):
        self.calls.append(("review", None))
        return self.in_review

    async def list_queue_eligible_issues(self):
        self.calls.append(("queue", None))
        return self.queue_eligible

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


def test_parses_playbook_opener():
    playbooks = parse_playbooks(PLAYBOOKS_MD)
    assert playbooks["PB-1"].name == "Chair-Call Shepherd"
    assert playbooks["PB-1"].opener.startswith("Two decisions are waiting")
    assert "draft up to 3" in playbooks["PB-2"].pre_authorized


def test_desktop_notifier_uses_native_macos_commands(monkeypatch):
    calls = []

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", None

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        return FakeProcess()

    monkeypatch.setattr("jarvis.playbooks.platform.system", lambda: "Darwin")
    monkeypatch.setattr("jarvis.playbooks.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(
        DesktopNotifier({"shortcuts": {"notify_enabled": True, "notify_speak": False}}).notify(
            'Hello "chair"'
        )
    )

    assert result == {"ok": True, "output": ""}
    assert calls == (
        [
            (
                "osascript",
                "-e",
                'display notification "Hello \\"chair\\"" with title "Yennefer"',
            )
        ]
    )


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


def test_linear_board_client_can_read_from_local_bridge_without_api_key():
    class BridgeClient(LinearBoardClient):
        def __init__(self):
            super().__init__(api_key="", bridge_url="http://127.0.0.1:4351", bridge_token="bridge")
            self.calls = []

        async def _bridge_get(self, path, params=None):
            self.calls.append((path, params))
            return {
                "issues": [
                    {
                        "id": "issue-1",
                        "identifier": "DAR-42",
                        "title": "HELM Native",
                        "state": "Todo",
                        "state_type": "unstarted",
                        "labels": ["spec-driven", "council"],
                        "comments": ["Verifier says PASS"],
                        "priority": 2,
                        "project": "HELM",
                    }
                ]
            }

    client = BridgeClient()

    issues = asyncio.run(client.list_queue_eligible_issues())

    assert client.calls == [("/linear/queue-eligible", None)]
    assert issues[0].identifier == "DAR-42"
    assert issues[0].labels == ("spec-driven", "council")
    assert issues[0].comments == ("Verifier says PASS",)


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
    assert fake.calls == [("chair", NOW - timedelta(hours=24)), ("review", None)]


def test_in_review_without_verifier_verdict_blocks_chair_approval(tmp_path):
    review_issue = LinearIssue(
        id="issue-1",
        identifier="DAR-39",
        title="Yennefer Siri bridge",
        description="PR: https://github.com/dark-vector-cognition/yennefer/pull/39",
        url="https://linear.app/dar/issue/DAR-39",
        created_at="2026-06-12T10:00:00Z",
        state="In Review",
        state_type="started",
        labels=("chair-approved", "agent:claude", "spec-driven", "council", "chair-call"),
        priority=1,
        project="Yennefer",
    )
    fake = FakeLinear(
        chair_calls=[review_issue],
        in_review=[review_issue],
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

    assert result["status"] == "needs_verifier"
    assert result["next"] == "DAR-39"
    assert state["active"] is None
    assert state["hand_up"] is None
    assert state["review"]["status"] == "needs_verifier"
    packet = state["review"]["next_action"]
    assert packet["review_status"] == "needs_verifier"
    assert packet["implementer_lane"] == "agent:claude"
    assert packet["recommended_verifier_lane"] == "agent:gemini"
    assert packet["recommended_verifier_lane"] != packet["implementer_lane"]
    assert packet["pr_url"] == "https://github.com/dark-vector-cognition/yennefer/pull/39"
    assert "no_verifier_verdict" in packet["risk_flags"]
    assert "Final recommendation: PASS | REQUEST CHANGES | HOLD/CHAIR CALL" in packet[
        "verdict_comment_template"
    ]
    handoff_text = open(packet["handoff_path"], encoding="utf-8").read()
    assert "Chair approval is blocked until a verifier verdict exists." in handoff_text
    assert "Recommended verifier lane: agent:gemini" in handoff_text


def test_in_review_with_verifier_verdict_allows_chair_hand_up(tmp_path):
    review_issue = LinearIssue(
        id="issue-1",
        identifier="DAR-39",
        title="Yennefer Siri bridge",
        created_at="2026-06-12T10:00:00Z",
        state="In Review",
        state_type="started",
        labels=("chair-approved", "agent:claude", "spec-driven", "council", "chair-call"),
        comments=(
            "## Verifier Verdict - DAR-39\n\n"
            "Evidence checked:\n- tests passed\n\n"
            "Final recommendation: PASS",
        ),
        priority=1,
        project="Yennefer",
    )
    fake = FakeLinear(chair_calls=[review_issue], in_review=[review_issue])
    engine = make_engine(tmp_path, fake)

    result = asyncio.run(engine.evaluate_once())
    state = load_state(engine)

    assert result == {"status": "hand_up", "playbook_id": "PB-1"}
    assert state["active"]["playbook_id"] == "PB-1"
    assert state["active"]["status"] == "waiting_for_chair"
    assert "review" not in state


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


def test_hand_up_notifies_when_notifier_enabled(tmp_path):
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
