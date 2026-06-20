"""Local Linear bridge routes."""

from fastapi.testclient import TestClient

from jarvis.linear_bridge import create_app
from jarvis.playbooks import LinearIssue


class FakeLinear:
    def __init__(self):
        self.comments = []
        self.created = []
        self.moved = []

    async def list_chair_call_issues(self, older_than):
        return [
            LinearIssue(
                id="issue-1",
                identifier="DAR-39",
                title="Chair call",
                state="In Review",
                labels=("chair-call", "council"),
                priority=1,
            )
        ]

    async def list_queue_eligible_issues(self):
        return [
            LinearIssue(
                id="issue-2",
                identifier="DAR-42",
                title="Ready work",
                state="Todo",
                state_type="unstarted",
                labels=("spec-driven", "council"),
                priority=2,
            ),
            LinearIssue(
                id="issue-3",
                identifier="DAR-99",
                title="Not ready",
                state="Todo",
                state_type="unstarted",
                labels=("needs-spec", "council"),
                priority=1,
            ),
        ]

    async def list_active_issues(self, states=None):
        issues = [
            LinearIssue(
                id="issue-2",
                identifier="DAR-42",
                title="Ready work",
                state="Todo",
                state_type="unstarted",
                labels=("spec-driven", "council"),
                priority=2,
            ),
            LinearIssue(
                id="issue-5",
                identifier="DAR-48",
                title="Review work",
                state="In Review",
                state_type="started",
                labels=("agent:codex",),
                priority=1,
            ),
            LinearIssue(
                id="issue-6",
                identifier="DAR-80",
                title="Bridge work",
                state="Todo",
                state_type="unstarted",
                labels=("spec-driven",),
                priority=2,
            ),
        ]
        if states:
            wanted = {state.lower() for state in states}
            issues = [issue for issue in issues if issue.state.lower() in wanted]
        return issues

    async def list_in_review_issues(self):
        return [
            LinearIssue(
                id="issue-4",
                identifier="DAR-48",
                title="Review me",
                state="In Review",
                labels=("agent:codex", "spec-driven", "council"),
                comments=("Implementation comment",),
                priority=1,
            )
        ]

    async def create_needs_spec_issue(self, title, description, team, project=None):
        self.created.append((title, description, team, project))
        return LinearIssue(
            id="created",
            identifier="DAR-100",
            title=title,
            labels=("needs-spec", "council"),
            priority=0,
            project=project or "",
        )

    async def comment_on_issue(self, issue_id, body):
        self.comments.append((issue_id, body))

    async def move_issue_to_state(self, issue_id, state_name):
        self.moved.append((issue_id, state_name))
        return LinearIssue(
            id=issue_id,
            identifier=issue_id,
            title="Moved issue",
            state=state_name,
        )


def make_client(fake=None, host=("127.0.0.1", 50000), audit_log_path=None):
    app = create_app(fake or FakeLinear(), bridge_token="bridge-token", audit_log_path=audit_log_path)
    return TestClient(app, client=host)


def auth():
    return {"Authorization": "Bearer bridge-token"}


def test_bridge_requires_token():
    client = make_client()

    assert client.get("/linear/queue-eligible").status_code == 401
    assert client.get("/linear/queue-eligible", headers=auth()).status_code == 200


def test_bridge_is_loopback_only_by_default():
    client = make_client(host=("192.168.1.50", 50000))

    assert client.get("/linear/queue-eligible", headers=auth()).status_code == 403


def test_bridge_filters_queue_eligible():
    client = make_client()

    response = client.get("/linear/queue-eligible", headers=auth())

    assert response.status_code == 200
    issues = response.json()["issues"]
    assert [issue["identifier"] for issue in issues] == ["DAR-42"]


def test_bridge_counts_active_issues_by_state():
    client = make_client()

    response = client.get("/linear/counts", headers=auth(), params={"states": "Todo,In Review"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == {"states": ["Todo", "In Review"], "active_only": True}
    assert payload["counts"] == {"Todo": 2, "In Review": 1}
    assert payload["identifiers"]["Todo"] == ["DAR-42", "DAR-80"]


def test_bridge_exposes_review_comments_and_safe_writes(tmp_path):
    fake = FakeLinear()
    audit_log = tmp_path / "audit.jsonl"
    client = make_client(fake, audit_log_path=audit_log)

    review = client.get("/linear/in-review", headers=auth()).json()["issues"][0]
    assert review["comments"] == ["Implementation comment"]

    created = client.post(
        "/linear/issues/needs-spec",
        headers=auth(),
        json={
            "title": "Draft bridge task",
            "description": "Spec me",
            "team": "Darkvectorcognition",
            "project": "Yennefer",
        },
    )
    assert created.status_code == 200
    assert created.json()["issue"]["identifier"] == "DAR-100"
    assert fake.created == [("Draft bridge task", "Spec me", "Darkvectorcognition", "Yennefer")]

    commented = client.post(
        "/linear/issues/DAR-48/comments",
        headers=auth(),
        json={"body": "Verifier verdict"},
    )
    assert commented.status_code == 200
    assert fake.comments == [("DAR-48", "Verifier verdict")]

    audit = audit_log.read_text(encoding="utf-8")
    assert "linear.issue.create_needs_spec" in audit
    assert "linear.issue.comment" in audit


def test_state_moves_require_approval_and_are_audited(tmp_path):
    fake = FakeLinear()
    audit_log = tmp_path / "audit.jsonl"
    client = make_client(fake, audit_log_path=audit_log)

    denied = client.post(
        "/linear/issues/DAR-80/state",
        headers=auth(),
        json={"state": "In Progress"},
    )
    assert denied.status_code == 400
    assert fake.moved == []

    moved = client.post(
        "/linear/issues/DAR-80/state",
        headers=auth(),
        json={
            "state": "In Progress",
            "approved_by": "Hank via Yennefer UI",
            "reason": "Selected by zero-ticket loop",
        },
    )

    assert moved.status_code == 200
    assert moved.json()["issue"]["state"] == "In Progress"
    assert fake.moved == [("DAR-80", "In Progress")]
    audit = audit_log.read_text(encoding="utf-8")
    assert "linear.issue.move_denied" in audit
    assert "linear.issue.move" in audit
    assert "Hank via Yennefer UI" in audit
