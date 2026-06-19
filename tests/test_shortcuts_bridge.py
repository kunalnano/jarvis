"""Shortcuts bridge routes - bearer auth, brief, ask, and hand-up ack."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from jarvis.shortcuts_bridge import brief_focus_reply, install_shortcuts_routes, limit_voice_reply


class FakePlaybooks:
    def __init__(self):
        self.messages = []
        self.state = FakeState()

    async def handle_chat(self, message):
        self.messages.append(message)
        if message == "later":
            return {"status": "snoozed", "reply": "Snoozed.", "snooze_until": "2026-06-14T16:40:00Z"}
        if message == "go ahead":
            return {"status": "approved", "reply": "Approved."}
        return None


class FakeState:
    def load(self):
        return {
            "active": {"playbook_id": "PB-1", "status": "waiting_for_chair"},
            "pm": {
                "status": "next_action",
                "next_action": {"identifier": "DAR-42", "title": "HELM Native v1"},
            },
        }


def make_client(tmp_path, client_host=("testclient", 50000)):
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "2026-06-13-strategist-brief.md").write_text(
        "## THE ONE THING\nShip the bridge.\n\n## DRIFT WATCH\nNone.\n",
        encoding="utf-8",
    )
    app = FastAPI()

    async def ask_yennefer(question):
        return f"Asked: {question}. Second sentence. Third sentence. Fourth sentence."

    playbooks = FakePlaybooks()
    install_shortcuts_routes(
        app,
        {"shortcuts": {"bearer_token": "test-token", "briefs_dir": str(briefs)}},
        ask_yennefer,
        playbooks,
    )
    return TestClient(app, client=client_host), playbooks


def auth():
    return {"Authorization": "Bearer test-token"}


def test_brief_today_requires_bearer_and_returns_latest_brief(tmp_path):
    client, _ = make_client(tmp_path)

    assert client.get("/brief/today").status_code == 401
    response = client.get("/brief/today", headers=auth())

    assert response.status_code == 200
    assert "## THE ONE THING" in response.text
    assert "Ship the bridge." in response.text


def test_brief_focus_allows_loopback_without_bearer(tmp_path):
    client, _ = make_client(tmp_path, client_host=("127.0.0.1", 50000))

    response = client.get("/brief/focus")

    assert response.status_code == 200
    assert response.text == "Ship the bridge."


def test_brief_focus_requires_bearer_off_loopback(tmp_path):
    client, _ = make_client(tmp_path)

    assert client.get("/brief/focus").status_code == 401
    response = client.get("/brief/focus", headers=auth())

    assert response.status_code == 200
    assert response.text == "Ship the bridge."


def test_ask_returns_three_sentence_voice_reply(tmp_path):
    client, _ = make_client(tmp_path)

    response = client.post("/ask", headers=auth(), json={"question": "Status?"})

    assert response.status_code == 200
    assert response.json()["reply"] == "Asked: Status? Second sentence. Third sentence."


def test_ask_local_allows_loopback_without_bearer(tmp_path):
    client, _ = make_client(tmp_path, client_host=("127.0.0.1", 50000))

    response = client.get("/ask/local", params={"question": "What now?"})

    assert response.status_code == 200
    assert response.text == "Asked: What now? Second sentence. Third sentence."


def test_ask_local_requires_bearer_off_loopback(tmp_path):
    client, _ = make_client(tmp_path)

    assert client.get("/ask/local", params={"question": "What now?"}).status_code == 401
    response = client.get("/ask/local", params={"question": "What now?"}, headers=auth())

    assert response.status_code == 200
    assert response.text == "Asked: What now? Second sentence. Third sentence."


def test_handup_ack_snoozes_or_approves(tmp_path):
    client, playbooks = make_client(tmp_path)

    snooze = client.post("/handup/ack", headers=auth(), json={"action": "snooze"})
    approve = client.post("/handup/ack", headers=auth(), json={"action": "go_ahead"})
    ack = client.post("/handup/ack", headers=auth(), json={"action": "ack"})

    assert snooze.json()["status"] == "snoozed"
    assert approve.json()["status"] == "approved"
    assert ack.json()["status"] == "acknowledged"
    assert playbooks.messages == ["later", "go ahead"]


def test_pm_next_requires_bearer_and_returns_pm_packet(tmp_path):
    client, _ = make_client(tmp_path)

    assert client.get("/pm/next").status_code == 401
    response = client.get("/pm/next", headers=auth())

    assert response.status_code == 200
    assert response.json()["pm"]["status"] == "next_action"
    assert response.json()["pm"]["next_action"]["identifier"] == "DAR-42"


def test_pm_next_surfaces_linear_auth_error(tmp_path):
    app = FastAPI()

    async def ask_yennefer(question):
        return "Asked."

    class ErrorState:
        def load(self):
            return {"last_error": "Linear unavailable: LINEAR_API_KEY is not configured"}

    class ErrorPlaybooks:
        state = ErrorState()

    install_shortcuts_routes(
        app,
        {"shortcuts": {"bearer_token": "test-token"}},
        ask_yennefer,
        ErrorPlaybooks(),
    )
    client = TestClient(app)

    response = client.get("/pm/next", headers=auth())

    assert response.status_code == 200
    assert response.json()["pm"] == {
        "status": "linear_unavailable",
        "error": "Linear unavailable: LINEAR_API_KEY is not configured",
    }


def test_pm_next_returns_review_packet_when_verifier_needed(tmp_path):
    app = FastAPI()

    async def ask_yennefer(question):
        return "Asked."

    class ReviewState:
        def load(self):
            return {
                "review": {
                    "status": "needs_verifier",
                    "count": 2,
                    "next_action": {"identifier": "DAR-48"},
                }
            }

    class ReviewPlaybooks:
        state = ReviewState()

    install_shortcuts_routes(
        app,
        {"shortcuts": {"bearer_token": "test-token"}},
        ask_yennefer,
        ReviewPlaybooks(),
    )
    client = TestClient(app)

    response = client.get("/pm/next", headers=auth())

    assert response.status_code == 200
    assert response.json()["pm"] == {
        "status": "needs_verifier",
        "count": 2,
        "next_action": {"identifier": "DAR-48"},
    }


def test_limit_voice_reply_is_three_sentences():
    assert limit_voice_reply("One. Two! Three? Four.") == "One. Two! Three?"


def test_brief_focus_reply_extracts_the_one_thing(tmp_path):
    make_client(tmp_path)
    config = {"shortcuts": {"briefs_dir": str(tmp_path / "briefs")}}

    assert brief_focus_reply(config, "What should I focus on next?") == "Ship the bridge."


def test_brief_focus_reply_uses_one_sentence_for_siri(tmp_path):
    briefs = tmp_path / "briefs"
    briefs.mkdir()
    (briefs / "2026-06-13-strategist-brief.md").write_text(
        "## THE ONE THING\nShip the bridge. Then explain the whole backlog in detail.\n",
        encoding="utf-8",
    )
    config = {"shortcuts": {"briefs_dir": str(briefs)}}

    assert brief_focus_reply(config, "Brief me") == "Ship the bridge."


def test_brief_focus_reply_ignores_unrelated_questions(tmp_path):
    make_client(tmp_path)
    config = {"shortcuts": {"briefs_dir": str(tmp_path / "briefs")}}

    assert brief_focus_reply(config, "Tell me a joke") == ""
