"""Yennefer playbook engine tests - no real Linear calls."""

import asyncio
import json
from datetime import datetime, timezone

from jarvis.playbooks import LinearIssue, PlaybookEngine, parse_playbooks


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
    def __init__(self, chair_calls=None, queue_eligible=None):
        self.chair_calls = chair_calls or []
        self.queue_eligible = queue_eligible or []
        self.created = []
        self.calls = []

    async def list_chair_call_issues(self, older_than):
        self.calls.append(("chair", older_than))
        return self.chair_calls

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


def make_engine(tmp_path, fake):
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
                "interval_seconds": 900,
                "jitter_seconds": 0,
                "team": "Darkvectorcognition",
                "project": "Yennefer",
            }
        },
        client=fake,
        now_fn=lambda: NOW,
    )


def load_state(engine):
    return json.loads(engine.state.path.read_text(encoding="utf-8"))


def test_parses_playbook_opener():
    playbooks = parse_playbooks(PLAYBOOKS_MD)
    assert playbooks["PB-1"].name == "Chair-Call Shepherd"
    assert playbooks["PB-1"].opener.startswith("Two decisions are waiting")
    assert "draft up to 3" in playbooks["PB-2"].pre_authorized


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
