import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from jarvis.resource_conductor import (
    ConductorHealth,
    ResourceConductor,
    ServiceHealth,
    merge_resources_line,
    parse_registry_routing_policy,
)


NOW = datetime(2026, 7, 5, 20, 0, tzinfo=timezone.utc)

REGISTRY = """
## Compute routing policy (Jarvis = conductor, planned DAR-40)
| Task class | Engine | Where |
|---|---|---|
| Ambient musings, triage, summarization, first-pass drafts, embeddings | Local LM (Qwen/Gemma via LM Studio) | Stormbreaker |
| Council implementation, verification judgment, strategist brief reasoning, anything gate-bound | Claude-class cloud | dispatched from Firestarter |
| Image generation | InvokeAI | Stormbreaker |
| Deterministic checks (sweeps, greps, builds) | scripts, no model | wherever the repo is |
"""


def healthy():
    return ConductorHealth(
        lmstudio=ServiceHealth(
            name="stormbreaker-lm-studio",
            ok=True,
            url="http://stormbreaker:1234/v1",
            models=("qwen-agentworld-35b-a3b",),
        ),
        vault_rag=ServiceHealth(name="vault-rag", ok=True, url="http://127.0.0.1:8742"),
    )


def unhealthy():
    return ConductorHealth(
        lmstudio=ServiceHealth(
            name="stormbreaker-lm-studio",
            ok=False,
            url="http://stormbreaker:1234/v1",
            error="connection refused",
        ),
        vault_rag=ServiceHealth(name="vault-rag", ok=False, url="http://127.0.0.1:8742"),
    )


def conductor(tmp_path, registry_text=REGISTRY):
    registry = tmp_path / "registry.md"
    registry.write_text(registry_text, encoding="utf-8")
    return ResourceConductor(
        {
            "resources": {
                "registry_path": str(registry),
                "state_path": str(tmp_path / "state.json"),
                "tally_dir": str(tmp_path / "resources"),
                "queue_dir": str(tmp_path / "queue"),
                "champion_model": "qwen-agentworld-35b-a3b",
            },
            "llm": {"model": "qwen-agentworld-35b-a3b"},
        },
        now_fn=lambda: NOW,
    )


def test_registry_policy_routes_summarization_to_local_lm(tmp_path):
    rc = conductor(tmp_path)

    decision = asyncio.run(
        rc.route_task("summarization", estimated_tokens=1200, health=healthy())
    )

    assert decision.engine == "lmstudio"
    assert decision.model_id == "qwen-agentworld-35b-a3b"
    assert decision.reason == "registry_local_policy_lmstudio_healthy"
    assert "local=1" in rc.resources_line()
    assert "offloaded_tokens=1200" in rc.resources_line()


def test_outage_queues_task_and_raises_pb4_hand_up(tmp_path):
    rc = conductor(tmp_path)

    result = asyncio.run(
        rc.execute_text_task(
            "summarization",
            "Summarize the queue.",
            estimated_tokens=800,
            health=unhealthy(),
        )
    )

    decision = result["decision"]
    queued_path = Path(result["queued_path"])
    queued = json.loads(queued_path.read_text(encoding="utf-8"))
    assert queued["prompt"] == "Summarize the queue."
    assert decision["engine"] == "queued"
    assert decision["status"] == "deferred"
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["active"]["playbook_id"] == "PB-4"
    assert state["hand_up"]["message"].startswith("Stormbreaker local compute")
    assert state["resources"]["outages"][0]["task_class"] == "summarization"
    assert state["resources"]["outages"][0]["queued_path"] == result["queued_path"]
    line = rc.resources_line()
    assert "queued=1" in line
    assert "outages=1" in line


def test_registry_edit_changes_route_without_restart(tmp_path):
    rc = conductor(tmp_path)

    first = asyncio.run(rc.route_task("summarization", health=healthy()))
    assert first.engine == "lmstudio"

    rc.registry_path.write_text(
        """
## Compute routing policy
| Task class | Engine | Where |
|---|---|---|
| summarization | Claude-class cloud | dispatched from Firestarter |
""",
        encoding="utf-8",
    )
    second = asyncio.run(rc.route_task("summarization", health=healthy()))

    assert second.engine == "cloud"
    assert second.reason == "registry_cloud_policy"


def test_parse_registry_policy_expands_task_classes():
    rules = parse_registry_routing_policy(REGISTRY)

    names = {rule.task_class for rule in rules}
    assert "summarization" in names
    assert "triage" in names
    assert "verification judgment" in names


def test_policy_parse_failure_queues_and_surfaces_fault(tmp_path):
    rc = conductor(tmp_path, registry_text="## Compute routing policy\nnot a table\n")

    decision = asyncio.run(rc.route_task("summarization", health=healthy()))

    assert decision.engine == "queued"
    assert decision.reason == "routing_policy_parse_failed"
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["active"]["playbook_id"] == "PB-4"
    assert state["active"]["payload"]["resources"]["service"] == "routing-policy"


def test_merge_resources_line_replaces_existing_line():
    merged = merge_resources_line("## A\nText\n\nRESOURCES: old\n", "RESOURCES: new")

    assert merged == "## A\nText\n\nRESOURCES: new\n"


def test_execute_failure_records_only_fallback_tally(tmp_path, monkeypatch):
    rc = conductor(tmp_path)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None, timeout=None):
            request = httpx.Request("GET", url)
            if url.endswith("/models"):
                return httpx.Response(
                    200,
                    request=request,
                    json={"data": [{"id": "qwen-agentworld-35b-a3b"}]},
                )
            return httpx.Response(200, request=request, json={"ok": True})

        async def post(self, url, headers=None, json=None, timeout=None):
            raise httpx.ConnectError("boom", request=httpx.Request("POST", url))

    monkeypatch.setattr("jarvis.resource_conductor.httpx.AsyncClient", FakeClient)

    result = asyncio.run(rc.execute_text_task("summarization", "summarize this", estimated_tokens=400))

    assert result["status"] == "queued"
    assert result["queued_path"]
    queued = json.loads(Path(result["queued_path"]).read_text(encoding="utf-8"))
    assert queued["prompt"] == "summarize this"
    line = rc.resources_line()
    assert "local=0" in line
    assert "queued=1" in line
    assert "outages=1" in line


def test_same_second_queue_writes_do_not_overwrite(tmp_path):
    rc = conductor(tmp_path)

    first = asyncio.run(
        rc.execute_text_task(
            "summarization",
            "first prompt",
            estimated_tokens=100,
            health=unhealthy(),
        )
    )
    second = asyncio.run(
        rc.execute_text_task(
            "summarization",
            "second prompt",
            estimated_tokens=100,
            health=unhealthy(),
        )
    )

    assert first["queued_path"] != second["queued_path"]
    queue_files = sorted((tmp_path / "queue").glob("*.json"))
    assert len(queue_files) == 2
    prompts = {
        json.loads(path.read_text(encoding="utf-8"))["prompt"]
        for path in queue_files
    }
    assert prompts == {"first prompt", "second prompt"}
