"""Graph memory contract tests — mocked vault-rag endpoint, no network."""

import asyncio
import json

import httpx
import pytest

from jarvis.graph_memory import (
    MEMORY_UNAVAILABLE_NOTICE,
    GraphMemory,
    build_situation,
    format_continuity_block,
    has_continuity,
    utc_ts,
)

TS = "2026-06-10T17:00:00.000Z"

CONTINUITY = {
    "prior_observations": [
        {"ts": "2026-06-09T10:00:00.000Z", "severity": "observation", "text": "Saw this yesterday."}
    ],
    "open_gaps": [{"text": "Wire yennefer memory", "source_path": "specs/spec-x.md"}],
    "decisions": [{"text": "Observations go to live namespace", "source_path": "specs/spec-x.md"}],
}


def make_memory(handler) -> GraphMemory:
    return GraphMemory(config={}, transport=httpx.MockTransport(handler))


def down_memory() -> GraphMemory:
    def handler(request):
        raise httpx.ConnectError("connect ECONNREFUSED 127.0.0.1:8742")

    return make_memory(handler)


class TestRecordObservation:
    def test_posts_life_event_contract(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["headers"] = request.headers
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"status": "ok"})

        memory = make_memory(handler)
        ok = asyncio.run(
            memory.record_observation(
                trigger="ci-build",
                severity="observation",
                text="The build broke again.",
                context="exit 1",
                resource="ci-build",
                ts=TS,
            )
        )

        assert ok is True
        assert memory.available is True
        assert seen["url"] == "http://127.0.0.1:8742/api/graph/ingest/life-event"
        assert seen["headers"]["Tailscale-User-Login"] == "yennefer@local"
        body = seen["body"]
        assert body["namespace"] == "live"
        assert body["source_path"] == f"yennefer://observations/ci-build/{TS}"
        assert body["trigger"] == "ci-build"
        assert body["severity"] == "observation"
        assert body["resource"] == "ci-build"
        assert body["provenance"]["source_system"] == "yennefer-standalone"
        assert body["provenance"]["emitter"] == "presence"

    def test_omits_resource_when_absent(self):
        memory = GraphMemory()
        payload = memory.build_life_event("ambient", "info", "hello", ts=TS)
        assert "resource" not in payload

    def test_fails_soft_when_graph_is_down(self):
        memory = down_memory()
        ok = asyncio.run(memory.record_observation("ci-build", "observation", "x", ts=TS))
        assert ok is False
        assert memory.available is False

    def test_fails_soft_on_503(self):
        memory = make_memory(lambda request: httpx.Response(503, json={"error": "down"}))
        ok = asyncio.run(memory.record_observation("ci-build", "observation", "x", ts=TS))
        assert ok is False
        assert memory.available is False


class TestFetchContinuity:
    def test_returns_prior_observations_gaps_and_decisions(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            if "/api/graph/life-events" in str(request.url):
                return httpx.Response(
                    200,
                    json={"events": [
                        {"ts": "2026-06-09T10:00:00.000Z", "severity": "observation", "text": "Saw this yesterday."}
                    ]},
                )
            return httpx.Response(
                200,
                json={
                    "gaps": [{"text": "Wire yennefer memory", "source_path": "specs/spec-x.md"}],
                    "decisions": [{"text": "Live namespace", "source_path": "specs/spec-x.md"}],
                },
            )

        memory = make_memory(handler)
        continuity = asyncio.run(memory.fetch_continuity("ci-build", "ci-build"))

        assert continuity is not None
        assert continuity["prior_observations"][0]["text"] == "Saw this yesterday."
        assert continuity["open_gaps"][0]["text"] == "Wire yennefer memory"
        assert continuity["decisions"][0]["source_path"] == "specs/spec-x.md"
        assert any("trigger=ci-build" in url for url in calls)
        assert any("resource=ci-build" in url for url in calls)

    def test_skips_resource_query_without_resource(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={"events": []})

        memory = make_memory(handler)
        continuity = asyncio.run(memory.fetch_continuity("ambient"))

        assert continuity == {"prior_observations": [], "open_gaps": [], "decisions": []}
        assert len(calls) == 1

    def test_fails_soft_when_graph_is_down(self):
        memory = down_memory()
        assert asyncio.run(memory.fetch_continuity("ci-build", "ci-build")) is None
        assert memory.available is False


class TestUnavailableNotice:
    def test_announces_exactly_once_per_session(self):
        memory = down_memory()
        asyncio.run(memory.fetch_continuity("ci-build"))

        assert memory.consume_unavailable_notice() == MEMORY_UNAVAILABLE_NOTICE
        assert memory.consume_unavailable_notice() is None

    def test_silent_while_healthy(self):
        assert GraphMemory().consume_unavailable_notice() is None

    def test_rearms_after_session_reset(self):
        memory = down_memory()
        asyncio.run(memory.fetch_continuity("ci-build"))
        memory.consume_unavailable_notice()

        memory.reset_session()
        asyncio.run(memory.fetch_continuity("ci-build"))
        assert memory.consume_unavailable_notice() == MEMORY_UNAVAILABLE_NOTICE


class TestPromptInjection:
    def test_injects_continuity_block_into_situation(self):
        situation = build_situation("Something changed in ci-build.", CONTINUITY, "ci-build")

        assert situation.startswith("Something changed in ci-build.")
        assert "PRIOR OBSERVATIONS (same trigger class, last 7 days):" in situation
        assert "Saw this yesterday." in situation
        assert "OPEN GAPS touching ci-build:" in situation
        assert "STANDING DECISIONS touching ci-build:" in situation
        assert "Reference that continuity" in situation

    def test_leaves_situation_untouched_without_continuity(self):
        base = "Something changed in ci-build."
        empty = {"prior_observations": [], "open_gaps": [], "decisions": []}

        assert build_situation(base, None, "ci-build") == base
        assert build_situation(base, empty, "ci-build") == base

    def test_has_continuity_and_block_agree(self):
        assert has_continuity(None) is False
        assert has_continuity({"prior_observations": [], "open_gaps": [], "decisions": []}) is False
        assert has_continuity(CONTINUITY) is True
        assert "- [2026-06-09T10:00:00.000Z]" in format_continuity_block(CONTINUITY, "ci-build")


def test_utc_ts_is_iso_with_z_suffix():
    ts = utc_ts()
    assert ts.endswith("Z")
    assert "T" in ts
