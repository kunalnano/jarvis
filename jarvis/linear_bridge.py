"""
Local Linear bridge for Yennefer.

Yennefer should not need the full Linear API key in her daemon. This bridge
keeps the broad credential in one loopback-only process and exposes only the
small queue/governor surface she needs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from .config import load_dotenv
from .playbooks import LinearBoardClient, LinearUnavailable, _queue_eligible

DEFAULT_AUDIT_LOG = "~/.yennefer/linear-bridge-audit.jsonl"
ACTIVE_STATES = ("Backlog", "Todo", "In Progress", "In Review")


class NeedsSpecBody(BaseModel):
    title: str
    description: str
    team: str
    project: str | None = None
    prompt_summary: str | None = None


class CommentBody(BaseModel):
    body: str
    prompt_summary: str | None = None


class StateMoveBody(BaseModel):
    state: str
    approved_by: str | None = None
    reason: str | None = None
    prompt_summary: str | None = None


def create_app(
    linear: LinearBoardClient | None = None,
    bridge_token: str | None = None,
    allow_remote: bool = False,
    audit_log_path: str | Path | None = None,
) -> FastAPI:
    app = FastAPI(title="Yennefer Linear Bridge")
    token = bridge_token if bridge_token is not None else os.environ.get("YENNEFER_LINEAR_BRIDGE_TOKEN", "")
    audit_path = Path(os.path.expanduser(str(audit_log_path or os.environ.get("YENNEFER_LINEAR_AUDIT_LOG", DEFAULT_AUDIT_LOG))))
    client = linear or LinearBoardClient(
        api_key=os.environ.get("LINEAR_API_KEY"),
        team=os.environ.get("YENNEFER_LINEAR_TEAM", "Darkvectorcognition"),
        bridge_url="",
    )

    def authorize(request: Request) -> None:
        if not _is_loopback(request) and not allow_remote:
            raise HTTPException(status_code=403, detail="loopback only")
        if not token:
            raise HTTPException(status_code=503, detail="bridge token is not configured")
        if request.headers.get("authorization") != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/healthz")
    async def healthz(request: Request):
        authorize(request)
        return {"ok": True, "service": "yennefer-linear-bridge"}

    @app.get("/linear/chair-calls")
    async def chair_calls(request: Request, older_than: str | None = None):
        authorize(request)
        cutoff = _parse_cutoff(older_than)
        try:
            issues = await client.list_chair_call_issues(cutoff)
        except LinearUnavailable as exc:
            return {"error": str(exc)}
        return {"issues": [_issue_payload(issue) for issue in issues]}

    @app.get("/linear/queue-eligible")
    async def queue_eligible(request: Request):
        authorize(request)
        try:
            issues = await client.list_queue_eligible_issues()
        except LinearUnavailable as exc:
            return {"error": str(exc)}
        return {"issues": [_issue_payload(issue) for issue in issues if _queue_eligible(issue)]}

    @app.get("/linear/active")
    async def active(request: Request, states: str | None = None):
        authorize(request)
        wanted = _states_from_param(states)
        try:
            issues = await client.list_active_issues(wanted)
        except LinearUnavailable as exc:
            return {"error": str(exc)}
        return {"scope": wanted or list(ACTIVE_STATES), "issues": [_issue_payload(issue) for issue in issues]}

    @app.get("/linear/counts")
    async def counts(request: Request, states: str | None = None):
        authorize(request)
        wanted = _states_from_param(states) or list(ACTIVE_STATES)
        try:
            issues = await client.list_active_issues(wanted)
        except LinearUnavailable as exc:
            return {"error": str(exc)}
        by_state = {state: 0 for state in wanted}
        identifiers = {state: [] for state in wanted}
        for issue in issues:
            if issue.state in by_state:
                by_state[issue.state] += 1
                identifiers[issue.state].append(issue.identifier)
        return {
            "scope": {"states": wanted, "active_only": True},
            "counts": by_state,
            "identifiers": identifiers,
        }

    @app.get("/linear/in-review")
    async def in_review(request: Request):
        authorize(request)
        try:
            issues = await client.list_in_review_issues()
        except LinearUnavailable as exc:
            return {"error": str(exc)}
        return {"issues": [_issue_payload(issue) for issue in issues]}

    @app.post("/linear/issues/needs-spec")
    async def create_needs_spec(request: Request, body: NeedsSpecBody):
        authorize(request)
        try:
            issue = await client.create_needs_spec_issue(
                body.title,
                body.description,
                team=body.team,
                project=body.project,
            )
        except LinearUnavailable as exc:
            return {"error": str(exc)}
        _audit(
            audit_path,
            "linear.issue.create_needs_spec",
            request,
            {
                "issue": getattr(issue, "identifier", None),
                "before_state": None,
                "after_state": getattr(issue, "state", None) or "created",
                "prompt_summary": body.prompt_summary or _summarize(body.description or body.title),
                "title": body.title,
                "team": body.team,
                "project": body.project,
            },
        )
        return {"issue": _issue_payload(issue) if issue else None}

    @app.post("/linear/issues/{issue_id}/comments")
    async def comment(request: Request, issue_id: str, body: CommentBody):
        authorize(request)
        before = await _find_active_issue(client, issue_id)
        try:
            await client.comment_on_issue(issue_id, body.body)
        except LinearUnavailable as exc:
            return {"error": str(exc)}
        after = await _find_active_issue(client, issue_id)
        before_state = getattr(before, "state", None)
        after_state = getattr(after, "state", None) or before_state
        _audit(
            audit_path,
            "linear.issue.comment",
            request,
            {
                "issue": issue_id,
                "before_state": before_state,
                "after_state": after_state,
                "prompt_summary": body.prompt_summary or _summarize(body.body),
                "body_chars": len(body.body),
            },
        )
        return {"ok": True}

    @app.post("/linear/issues/{issue_id}/state")
    async def move_state(request: Request, issue_id: str, body: StateMoveBody):
        authorize(request)
        if not body.approved_by:
            _audit(
                audit_path,
                "linear.issue.move_denied",
                request,
                {
                    "issue": issue_id,
                    "before_state": None,
                    "after_state": None,
                    "target_state": body.state,
                    "prompt_summary": body.prompt_summary or _summarize(body.reason or "missing approved_by"),
                    "reason": "missing approved_by",
                },
            )
            raise HTTPException(status_code=400, detail="approved_by is required for state moves")
        before = await _find_active_issue(client, issue_id)
        try:
            issue = await client.move_issue_to_state(issue_id, body.state)
        except LinearUnavailable as exc:
            return {"error": str(exc)}
        before_state = getattr(before, "state", None)
        after_state = getattr(issue, "state", None) or body.state
        _audit(
            audit_path,
            "linear.issue.move",
            request,
            {
                "issue": issue_id,
                "before_state": before_state,
                "after_state": after_state,
                "target_state": body.state,
                "prompt_summary": body.prompt_summary or _summarize(body.reason or body.state),
                "approved_by": body.approved_by,
                "reason": body.reason,
                "result": getattr(issue, "identifier", None),
            },
        )
        return {"issue": _issue_payload(issue) if issue else None}

    return app


def _issue_payload(issue) -> dict:
    payload = asdict(issue)
    payload["labels"] = list(issue.labels)
    payload["comments"] = list(issue.comments)
    return payload


def _is_loopback(request: Request) -> bool:
    host = getattr(request.client, "host", "") if request.client else ""
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _states_from_param(value: str | None) -> list[str]:
    if not value:
        return []
    return [state.strip() for state in value.split(",") if state.strip()]


async def _find_active_issue(client: LinearBoardClient, issue_id: str):
    try:
        issues = await client.list_active_issues(list(ACTIVE_STATES))
    except LinearUnavailable:
        return None
    wanted = issue_id.lower()
    for issue in issues:
        if issue.identifier.lower() == wanted or issue.id.lower() == wanted:
            return issue
    return None


def _summarize(value: str, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[:limit - 1]}..."


def _parse_cutoff(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _audit(path: Path, action: str, request: Request, details: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    host = getattr(request.client, "host", "") if request.client else ""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "client": host,
        "details": details,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Yennefer's local Linear bridge.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=4351, type=int)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--audit-log", default=None)
    args = parser.parse_args()

    load_dotenv()
    app = create_app(allow_remote=args.allow_remote, audit_log_path=args.audit_log)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
