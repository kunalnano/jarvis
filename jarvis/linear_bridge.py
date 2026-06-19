"""
Local Linear bridge for Yennefer.

Yennefer should not need the full Linear API key in her daemon. This bridge
keeps the broad credential in one loopback-only process and exposes only the
small queue/governor surface she needs.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import asdict
from datetime import datetime, timezone
from ipaddress import ip_address

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from .config import load_dotenv
from .playbooks import LinearBoardClient, LinearUnavailable, _queue_eligible


class NeedsSpecBody(BaseModel):
    title: str
    description: str
    team: str
    project: str | None = None


class CommentBody(BaseModel):
    body: str


def create_app(
    linear: LinearBoardClient | None = None,
    bridge_token: str | None = None,
    allow_remote: bool = False,
) -> FastAPI:
    app = FastAPI(title="Yennefer Linear Bridge")
    token = bridge_token if bridge_token is not None else os.environ.get("YENNEFER_LINEAR_BRIDGE_TOKEN", "")
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
        return {"issue": _issue_payload(issue) if issue else None}

    @app.post("/linear/issues/{issue_id}/comments")
    async def comment(request: Request, issue_id: str, body: CommentBody):
        authorize(request)
        try:
            await client.comment_on_issue(issue_id, body.body)
        except LinearUnavailable as exc:
            return {"error": str(exc)}
        return {"ok": True}

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


def _parse_cutoff(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Yennefer's local Linear bridge.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=4351, type=int)
    parser.add_argument("--allow-remote", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    app = create_app(allow_remote=args.allow_remote)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

