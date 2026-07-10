"""
Graph memory - Jarvis's durable continuity via the vault-rag Neo4j sidecar.

Implements the same contract as spec C2 (spec-dvc-service-mesh-jarvis.md):
every fired presence trigger is recorded as a LifeEvent node in the "live"
graph namespace, and before reacting she pulls continuity context (prior
observations of the same trigger class, open Gap/Decision nodes touching the
same resource) so she references what she already knows instead of
re-adjudicating.

Everything here is fail-soft: if vault-rag (8742) or Neo4j (7687) is down,
calls resolve to None/False and her voice path is never blocked. The only
user-visible effect of an outage is a single "memory unavailable" notice per
session.
"""

import socket
from datetime import datetime, timezone

import httpx
from rich.console import Console

console = Console()

DEFAULT_ENDPOINT = "http://127.0.0.1:8742"
# vault-rag binds 127.0.0.1 and trusts this header (tailscale serve is the
# only remote ingress); local callers identify themselves with it.
DEFAULT_AUTH_USER = "jarvis@local"
# Slightly above helm-gold's 1.5s fuse: cold Neo4j label scans can exceed it,
# and a transient timeout costs a false "memory unavailable" announcement.
DEFAULT_TIMEOUT_SECONDS = 2.5
CONTINUITY_WINDOW_HOURS = 168  # 7 days
CONTINUITY_LIMIT = 5

MEMORY_UNAVAILABLE_NOTICE = (
    "My graph memory is unreachable, so I am working without continuity until it returns."
)

CONTINUITY_INSTRUCTION = (
    "You have already observed and adjudicated the items above. Reference that "
    "continuity briefly instead of re-judging from scratch; call out only what "
    "is new or has changed."
)


def utc_ts() -> str:
    """ISO 8601 with 'Z' suffix, matching the contract's JS-style timestamps."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class GraphMemory:
    """Fail-soft client for the vault-rag LifeEvent / continuity endpoints."""

    def __init__(self, config: dict | None = None, transport: httpx.AsyncBaseTransport | None = None):
        gm = (config or {}).get("graph_memory", {}) or {}
        self.enabled = bool(gm.get("enabled", True))
        self.base_url = str(gm.get("endpoint") or DEFAULT_ENDPOINT).rstrip("/")
        self.timeout = float(gm.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        self.auth_user = str(gm.get("auth_user") or DEFAULT_AUTH_USER)
        self._transport = transport  # test seam (httpx.MockTransport)
        self.available = True
        self._announced = False

    # ---- session state -------------------------------------------------

    def reset_session(self):
        self.available = True
        self._announced = False

    def consume_unavailable_notice(self) -> str | None:
        """The "memory unavailable" notice, exactly once per session while down."""
        if self.available or self._announced:
            return None
        self._announced = True
        return MEMORY_UNAVAILABLE_NOTICE

    def _mark_down(self, operation: str, error: Exception | str):
        if self.available:
            detail = str(error) or type(error).__name__
            console.print(f"[dim]Graph memory unavailable during {operation}: {detail}[/dim]")
        self.available = False

    # ---- HTTP plumbing --------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=self._transport,
            headers={
                "Content-Type": "application/json",
                "Tailscale-User-Login": self.auth_user,
            },
        )

    # ---- write path: one LifeEvent per fired trigger --------------------

    def build_life_event(
        self,
        trigger: str,
        severity: str,
        text: str,
        context: str = "",
        resource: str | None = None,
        ts: str | None = None,
    ) -> dict:
        ts = ts or utc_ts()
        payload = {
            "namespace": "live",
            "source_path": f"jarvis://observations/{trigger}/{ts}",
            "trigger": trigger,
            "severity": severity,
            "text": text,
            "context": context,
            "ts": ts,
            "provenance": {
                "source_system": "jarvis-standalone",
                "emitter": "presence",
                "host": socket.gethostname(),
            },
        }
        if resource:
            payload["resource"] = resource
        return payload

    async def record_observation(
        self,
        trigger: str,
        severity: str,
        text: str,
        context: str = "",
        resource: str | None = None,
        ts: str | None = None,
    ) -> bool:
        """POST the observation as a LifeEvent. Fail-soft: False on any error."""
        if not self.enabled:
            return False
        payload = self.build_life_event(trigger, severity, text, context, resource, ts)
        try:
            async with self._client() as client:
                resp = await client.post("/api/graph/ingest/life-event", json=payload)
            if resp.status_code != 200:
                self._mark_down("ingest", f"HTTP {resp.status_code}")
                return False
            self.available = True
            return True
        except Exception as exc:
            self._mark_down("ingest", exc)
            return False

    # ---- read path: continuity before commentary ------------------------

    async def fetch_continuity(self, trigger: str, resource: str | None = None) -> dict | None:
        """Prior observations of this trigger class (7d) plus open Gap/Decision
        nodes touching the resource. Fail-soft: None on any error."""
        if not self.enabled:
            return None
        try:
            async with self._client() as client:
                events_resp = await client.get(
                    "/api/graph/life-events",
                    params={
                        "trigger": trigger,
                        "since_hours": CONTINUITY_WINDOW_HOURS,
                        "limit": CONTINUITY_LIMIT,
                    },
                )
                if events_resp.status_code != 200:
                    self._mark_down("continuity", f"HTTP {events_resp.status_code}")
                    return None
                resource_payload = {}
                if resource:
                    resource_resp = await client.get(
                        "/api/graph/context/resource",
                        params={"resource": resource, "limit": CONTINUITY_LIMIT},
                    )
                    if resource_resp.status_code == 200:
                        resource_payload = resource_resp.json()
            self.available = True
            events = (events_resp.json() or {}).get("events") or []
            return {
                "prior_observations": [
                    {
                        "ts": str(e.get("ts") or ""),
                        "severity": str(e.get("severity") or ""),
                        "text": str(e.get("text") or ""),
                    }
                    for e in events
                    if isinstance(e, dict) and e.get("text")
                ],
                "open_gaps": _items(resource_payload.get("gaps")),
                "decisions": _items(resource_payload.get("decisions")),
            }
        except Exception as exc:
            self._mark_down("continuity", exc)
            return None


# ---- prompt injection ----------------------------------------------------


def has_continuity(continuity: dict | None) -> bool:
    return bool(
        continuity
        and (
            continuity.get("prior_observations")
            or continuity.get("open_gaps")
            or continuity.get("decisions")
        )
    )


def format_continuity_block(continuity: dict, resource: str | None = None) -> str:
    """Render the continuity block injected into the LM prompt."""
    target = resource or "this resource"
    lines = []
    if continuity.get("prior_observations"):
        lines.append("PRIOR OBSERVATIONS (same trigger class, last 7 days):")
        lines.extend(
            f"- [{o['ts']}] ({o['severity']}) {o['text']}"
            for o in continuity["prior_observations"]
        )
    if continuity.get("open_gaps"):
        lines.append(f"OPEN GAPS touching {target}:")
        lines.extend(f"- {g['text']} ({g['source_path']})" for g in continuity["open_gaps"])
    if continuity.get("decisions"):
        lines.append(f"STANDING DECISIONS touching {target}:")
        lines.extend(f"- {d['text']} ({d['source_path']})" for d in continuity["decisions"])
    lines.append(CONTINUITY_INSTRUCTION)
    return "\n".join(lines)


def build_situation(base: str, continuity: dict | None, resource: str | None = None) -> str:
    """Append the continuity block to a presence situation prompt when present."""
    if not has_continuity(continuity):
        return base
    return f"{base}\n\n{format_continuity_block(continuity, resource)}"


def _items(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [
        {
            "text": str(item.get("text") or ""),
            "source_path": str(item.get("source_path") or ""),
        }
        for item in value
        if isinstance(item, dict) and item.get("text")
    ]
