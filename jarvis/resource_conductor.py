"""
Resource conductor for Jarvis.

Reads the DVC Machine & Agent Registry on each decision, routes local-tolerant
work to LM Studio when healthy, and leaves an auditable tally for the daily
brief and PB-4 hand-ups.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


DEFAULT_REGISTRY_PATH = (
    "~/Documents/ai/obsidian-vault/dvc/operating-model/"
    "DVC Machine & Agent Registry.md"
)
DEFAULT_STATE_PATH = "~/.jarvis/state.json"
DEFAULT_TALLY_DIR = "~/.jarvis/resources"
DEFAULT_VAULT_RAG_URL = "http://127.0.0.1:8742"

LOCAL_HINTS = {
    "ambient",
    "triage",
    "summary",
    "summarization",
    "summarise",
    "summarize",
    "draft",
    "first pass",
    "embedding",
}
CLOUD_HINTS = {
    "council",
    "implementation",
    "verification",
    "judgment",
    "judgement",
    "strategist brief",
    "gate bound",
    "gate-bound",
}


@dataclass(frozen=True)
class RoutingRule:
    task_class: str
    engine: str
    where: str
    source: str = "registry"


@dataclass(frozen=True)
class ServiceHealth:
    name: str
    ok: bool
    url: str = ""
    latency_ms: int | None = None
    models: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class ConductorHealth:
    lmstudio: ServiceHealth
    vault_rag: ServiceHealth


@dataclass(frozen=True)
class RouteDecision:
    task_class: str
    engine: str
    where: str
    status: str
    reason: str
    policy_engine: str
    policy_where: str
    model_id: str = ""
    estimated_tokens: int = 0


class ResourceConductor:
    """Local-first routing and tally layer for Jarvis."""

    def __init__(
        self,
        config: dict | None = None,
        now_fn=None,
    ):
        self.config = config or {}
        rc = self.config.get("resources", {}) or {}
        vault = self.config.get("vault", {}) or {}
        playbooks = self.config.get("playbooks", {}) or {}
        llm = self.config.get("llm", {}) or {}

        self.enabled = bool(rc.get("enabled", True))
        self.interval_seconds = float(rc.get("interval_seconds", 300))
        self.registry_path = _expand_path(
            str(
                rc.get("registry_path")
                or _registry_from_vault(vault.get("path"))
                or DEFAULT_REGISTRY_PATH
            )
        )
        self.state_path = _expand_path(
            str(rc.get("state_path") or playbooks.get("state_path") or DEFAULT_STATE_PATH)
        )
        self.tally_dir = _expand_path(str(rc.get("tally_dir") or DEFAULT_TALLY_DIR))
        self.queue_dir = _expand_path(str(rc.get("queue_dir") or "~/.jarvis/resource-queue"))
        self.vault_rag_url = _clean_value(rc.get("vault_rag_url")) or DEFAULT_VAULT_RAG_URL
        self.local_fallback = (
            _clean_value(rc.get("local_fallback"))
            or _clean_value(rc.get("fallback_engine"))
            or "queued"
        )
        self.champion_model = (
            _clean_value(rc.get("champion_model"))
            or _clean_value(llm.get("model"))
            or ""
        )
        self.endpoints = self._load_lm_endpoints()
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._tasks: list[asyncio.Task] = []
        self._last_health: ConductorHealth | None = None

    async def start(self) -> None:
        if not self.enabled:
            return
        self._tasks.append(asyncio.create_task(self._loop()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh_health()
            except Exception:
                pass
            await asyncio.sleep(self.interval_seconds)

    async def refresh_health(self) -> ConductorHealth:
        lmstudio = await self._check_lmstudio()
        vault_rag = await self._check_vault_rag()
        health = ConductorHealth(lmstudio=lmstudio, vault_rag=vault_rag)
        self._last_health = health
        self._record_health(health)
        return health

    async def route_task(
        self,
        task_class: str,
        *,
        estimated_tokens: int = 0,
        health: ConductorHealth | dict | None = None,
        registry_text: str | None = None,
        record: bool = True,
        record_outage: bool = True,
    ) -> RouteDecision:
        if registry_text is None:
            registry_text, policy_error = self._read_registry_text()
        else:
            policy_error = ""
        rules = parse_registry_routing_policy(registry_text)
        if not rules:
            reason = policy_error or "routing_policy_parse_failed"
            decision = RouteDecision(
                task_class=task_class,
                engine="queued",
                where=str(self.registry_path),
                status="deferred",
                reason=reason,
                policy_engine="unavailable",
                policy_where=str(self.registry_path),
                estimated_tokens=max(0, int(estimated_tokens or 0)),
            )
            if record_outage:
                self._record_outage(
                    decision,
                    ServiceHealth(
                        name="routing-policy",
                        ok=False,
                        url=str(self.registry_path),
                        error=reason,
                    ),
                )
            if record:
                self._record_route(decision)
            return decision

        rule = select_rule(task_class, rules)
        health_obj = _health_from_any(health) if health is not None else await self.refresh_health()
        local_requested = _is_local_engine(rule.engine)

        if local_requested:
            if health_obj.lmstudio.ok:
                model = _selected_model(health_obj.lmstudio.models, self.champion_model)
                decision = RouteDecision(
                    task_class=task_class,
                    engine="lmstudio",
                    where=rule.where,
                    status="ready",
                    reason="registry_local_policy_lmstudio_healthy",
                    policy_engine=rule.engine,
                    policy_where=rule.where,
                    model_id=model,
                    estimated_tokens=max(0, int(estimated_tokens or 0)),
                )
            else:
                reason = health_obj.lmstudio.error or "lmstudio_unavailable"
                decision = RouteDecision(
                    task_class=task_class,
                    engine=self.local_fallback,
                    where=rule.where,
                    status="deferred",
                    reason=reason,
                    policy_engine=rule.engine,
                    policy_where=rule.where,
                    estimated_tokens=max(0, int(estimated_tokens or 0)),
                )
                if record_outage:
                    self._record_outage(decision, health_obj.lmstudio)
        elif _is_cloud_engine(rule.engine):
            decision = RouteDecision(
                task_class=task_class,
                engine="cloud",
                where=rule.where,
                status="ready",
                reason="registry_cloud_policy",
                policy_engine=rule.engine,
                policy_where=rule.where,
                estimated_tokens=max(0, int(estimated_tokens or 0)),
            )
        else:
            decision = RouteDecision(
                task_class=task_class,
                engine="script",
                where=rule.where,
                status="ready",
                reason="registry_deterministic_policy",
                policy_engine=rule.engine,
                policy_where=rule.where,
                estimated_tokens=max(0, int(estimated_tokens or 0)),
            )

        if record:
            self._record_route(decision)
        return decision

    async def execute_text_task(
        self,
        task_class: str,
        prompt: str,
        *,
        estimated_tokens: int = 0,
        health: ConductorHealth | dict | None = None,
        registry_text: str | None = None,
    ) -> dict:
        decision = await self.route_task(
            task_class,
            estimated_tokens=estimated_tokens or estimate_tokens(prompt),
            health=health,
            registry_text=registry_text,
            record=False,
            record_outage=False,
        )
        if decision.engine != "lmstudio":
            queued_path = self._queue_task(decision, prompt) if decision.engine == "queued" else None
            if queued_path:
                self._record_outage(
                    decision,
                    self._outage_health_for_decision(decision, health),
                    queued_path=queued_path,
                )
            self._record_route(decision, queued_path=queued_path)
            result = {"status": decision.engine, "decision": asdict(decision)}
            if queued_path:
                result["queued_path"] = str(queued_path)
            return result

        endpoint = self._active_endpoint()
        payload = {
            "model": decision.model_id or endpoint.get("model") or "auto",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{endpoint['api_base']}/chat/completions",
                    headers=_headers(endpoint.get("api_key")),
                    json=payload,
                    timeout=120.0,
                )
                response.raise_for_status()
        except Exception as exc:
            failed = RouteDecision(
                task_class=task_class,
                engine=self.local_fallback,
                where=decision.where,
                status="deferred",
                reason=f"lmstudio_execution_failed: {exc}",
                policy_engine=decision.policy_engine,
                policy_where=decision.policy_where,
                estimated_tokens=decision.estimated_tokens,
            )
            queued_path = self._queue_task(failed, prompt)
            self._record_outage(
                failed,
                ServiceHealth(
                    name=endpoint.get("name", "lmstudio"),
                    ok=False,
                    url=endpoint.get("api_base", ""),
                    error=str(exc),
                ),
                queued_path=queued_path,
            )
            self._record_route(failed, queued_path=queued_path)
            return {
                "status": failed.engine,
                "decision": asdict(failed),
                "queued_path": str(queued_path),
            }

        data = response.json()
        self._record_route(decision)
        return {"status": "completed", "decision": asdict(decision), "response": data}

    def routing_rules(self, registry_text: str | None = None) -> list[RoutingRule]:
        if registry_text is None:
            registry_text, _ = self._read_registry_text()
        return parse_registry_routing_policy(registry_text)

    def _read_registry_text(self) -> tuple[str, str]:
        try:
            return self.registry_path.read_text(encoding="utf-8"), ""
        except Exception as exc:
            return "", f"routing_policy_read_failed: {exc}"

    def resources_line(self, date_key: str | None = None) -> str:
        data = self._load_tally(date_key=date_key)
        health = data.get("last_health") or {}
        lm = health.get("lmstudio") or {}
        vault = health.get("vault_rag") or {}
        models = tuple(lm.get("models") or ())
        champion = self._champion_status(models)
        vault_status = "ok" if vault.get("ok") else "unhealthy" if vault else "unknown"
        return (
            "RESOURCES: "
            f"local={int(data.get('local_tasks') or 0)} "
            f"cloud={int(data.get('cloud_tasks') or 0)} "
            f"queued={int(data.get('queued_tasks') or 0)} "
            f"offloaded_tokens={int(data.get('estimated_tokens_offloaded') or 0)} "
            f"outages={len(data.get('outages') or [])}; "
            f"champion={champion}; vault-rag={vault_status}"
        )

    def _champion_status(self, models: tuple[str, ...]) -> str:
        if not self.champion_model:
            return "unknown"
        if not models:
            return f"{self.champion_model}:unknown"
        normalized = _normalize(self.champion_model)
        loaded = any(normalized in _normalize(model) or _normalize(model) in normalized for model in models)
        return f"{self.champion_model}:{'loaded' if loaded else 'missing'}"

    async def _check_lmstudio(self) -> ServiceHealth:
        errors: list[str] = []
        async with httpx.AsyncClient() as client:
            for endpoint in self.endpoints:
                started = time.perf_counter()
                url = f"{endpoint['api_base']}/models"
                try:
                    response = await client.get(
                        url,
                        headers=_headers(endpoint.get("api_key")),
                        timeout=5.0,
                    )
                    response.raise_for_status()
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    models = tuple(
                        str(item.get("id") or "")
                        for item in (response.json().get("data") or [])
                        if item.get("id")
                    )
                    return ServiceHealth(
                        name=endpoint["name"],
                        ok=True,
                        url=endpoint["api_base"],
                        latency_ms=latency_ms,
                        models=models,
                    )
                except Exception as exc:
                    errors.append(f"{endpoint['name']}: {exc}")
        first = self.endpoints[0] if self.endpoints else {"name": "lmstudio", "api_base": ""}
        return ServiceHealth(
            name=first["name"],
            ok=False,
            url=first.get("api_base", ""),
            error="; ".join(errors) or "no_lmstudio_endpoints",
        )

    async def _check_vault_rag(self) -> ServiceHealth:
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{self.vault_rag_url.rstrip('/')}/health", timeout=3.0)
                response.raise_for_status()
            return ServiceHealth(
                name="vault-rag",
                ok=True,
                url=self.vault_rag_url,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            return ServiceHealth(
                name="vault-rag",
                ok=False,
                url=self.vault_rag_url,
                error=str(exc),
            )

    def _record_health(self, health: ConductorHealth) -> None:
        data = self._load_tally()
        data["last_health"] = _health_dict(health)
        data["updated_at"] = _iso(self.now_fn())
        self._save_tally(data)

    def _record_route(self, decision: RouteDecision, queued_path: Path | None = None) -> None:
        data = self._load_tally()
        if decision.engine == "lmstudio":
            data["local_tasks"] = int(data.get("local_tasks") or 0) + 1
            data["estimated_tokens_offloaded"] = (
                int(data.get("estimated_tokens_offloaded") or 0)
                + max(0, int(decision.estimated_tokens or 0))
            )
        elif decision.engine == "cloud":
            data["cloud_tasks"] = int(data.get("cloud_tasks") or 0) + 1
        elif decision.engine == "queued":
            data["queued_tasks"] = int(data.get("queued_tasks") or 0) + 1
        else:
            data["script_tasks"] = int(data.get("script_tasks") or 0) + 1
        routes = list(data.get("last_routes") or [])
        route = {"ts": _iso(self.now_fn()), **asdict(decision)}
        if queued_path:
            route["queued_path"] = str(queued_path)
        routes.append(route)
        data["last_routes"] = routes[-20:]
        data["updated_at"] = _iso(self.now_fn())
        self._save_tally(data)

    def _record_outage(
        self,
        decision: RouteDecision,
        health: ServiceHealth,
        queued_path: Path | None = None,
    ) -> None:
        now = _iso(self.now_fn())
        data = self._load_tally()
        outage = {
            "ts": now,
            "service": health.name,
            "task_class": decision.task_class,
            "fallback": decision.engine,
            "reason": decision.reason,
            "url": health.url,
            "where": decision.where,
        }
        if queued_path:
            outage["queued_path"] = str(queued_path)
        data.setdefault("outages", []).append(outage)
        data["updated_at"] = now
        self._save_tally(data)
        self._raise_pb4_hand_up(outage)

    def _raise_pb4_hand_up(self, outage: dict) -> None:
        data = _load_json(self.state_path)
        resources = data.setdefault("resources", {})
        resources.setdefault("outages", []).append(outage)
        where = str(outage.get("where") or "Local compute")
        hand_up = {
            "id": f"PB-4:resources:{outage['ts']}",
            "playbook_id": "PB-4",
            "playbook_name": "Machine Health",
            "status": "waiting_for_chair",
            "created_at": outage["ts"],
            "opener": (
                f"{where} local compute is unavailable for "
                f"{outage['task_class']}; I queued the task and logged the outage."
            ),
            "pre_authorized": (
                "Read logs, diagnose restartable daemons, and write the incident line."
            ),
            "on_go_ahead": (
                "Guide the chair through any human credential or machine step."
            ),
            "payload": {"resources": outage},
        }
        active = data.get("active") or {}
        if active.get("status") == "waiting_for_chair":
            resources.setdefault("pending_handups", []).append(hand_up)
        else:
            data["active"] = hand_up
            data["hand_up"] = _overlay_hand_up(hand_up)
        _save_json(self.state_path, data)

    def _outage_health_for_decision(
        self,
        decision: RouteDecision,
        health: ConductorHealth | dict | None,
    ) -> ServiceHealth:
        if decision.policy_engine == "unavailable":
            return ServiceHealth(
                name="routing-policy",
                ok=False,
                url=decision.policy_where,
                error=decision.reason,
            )
        if health is not None:
            return _health_from_any(health).lmstudio
        if self._last_health is not None:
            return self._last_health.lmstudio
        return ServiceHealth(name="lmstudio", ok=False, error=decision.reason)

    def _queue_task(self, decision: RouteDecision, prompt: str) -> Path:
        stamp = _iso(self.now_fn())
        safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "-", decision.task_class).strip("-") or "task"
        unique = uuid.uuid4().hex[:12]
        path = self.queue_dir / f"{stamp.replace(':', '').replace('Z', '')}-{safe_task}-{unique}.json"
        _save_json(
            path,
            {
                "ts": stamp,
                "status": "queued",
                "task_class": decision.task_class,
                "prompt": prompt,
                "decision": asdict(decision),
                "estimated_tokens": decision.estimated_tokens,
            },
        )
        return path

    def _load_tally(self, date_key: str | None = None) -> dict:
        path = self._tally_path(date_key=date_key)
        data = _load_json(path)
        if not data:
            data = _empty_tally(date_key or self.now_fn().date().isoformat())
        return data

    def _save_tally(self, data: dict) -> None:
        _save_json(self._tally_path(date_key=str(data.get("date") or "")), data)

    def _tally_path(self, date_key: str | None = None) -> Path:
        key = date_key or self.now_fn().date().isoformat()
        return self.tally_dir / f"{key}.json"

    def _load_lm_endpoints(self) -> list[dict]:
        llm = self.config.get("llm", {}) or {}
        endpoints: list[dict] = []
        seen: set[str] = set()

        def add(name: str, api_base: Any, api_key: Any = None, model: Any = None) -> None:
            base = _clean_value(api_base)
            if not base:
                return
            base = base.rstrip("/")
            if base in seen:
                return
            seen.add(base)
            endpoints.append(
                {
                    "name": _clean_value(name) or base,
                    "api_base": base,
                    "api_key": _clean_value(api_key),
                    "model": _clean_value(model) or _clean_value(llm.get("model")) or "auto",
                }
            )

        add(
            "prometheus-lm-studio",
            os.environ.get("LM_STUDIO_API_BASE") or llm.get("api_base") or "http://127.0.0.1:1234/v1",
            llm.get("api_key") or os.environ.get("LM_API_TOKEN"),
            llm.get("model"),
        )
        for index, fallback in enumerate(llm.get("fallbacks") or []):
            add(
                fallback.get("name") or f"lm-studio-fallback-{index + 1}",
                fallback.get("api_base"),
                fallback.get("api_key"),
                fallback.get("model"),
            )
        add(
            "stormbreaker-lm-studio",
            os.environ.get("WINDOWS_LM_STUDIO_API_BASE")
            or os.environ.get("STORMBREAKER_LM_STUDIO_API_BASE"),
            os.environ.get("WINDOWS_LM_STUDIO_API_KEY")
            or os.environ.get("STORMBREAKER_LM_STUDIO_API_KEY"),
            os.environ.get("WINDOWS_LM_STUDIO_MODEL")
            or os.environ.get("STORMBREAKER_LM_STUDIO_MODEL"),
        )
        return endpoints

    def _active_endpoint(self) -> dict:
        if self._last_health and self._last_health.lmstudio.ok:
            for endpoint in self.endpoints:
                if endpoint["name"] == self._last_health.lmstudio.name:
                    return endpoint
        return self.endpoints[0]


def parse_registry_routing_policy(markdown: str) -> list[RoutingRule]:
    section = _section(markdown or "", "Compute routing policy")
    rules: list[RoutingRule] = []
    for line in section.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        if cells[0].lower() == "task class" or set(cells[0]) <= {"-", ":"}:
            continue
        task_cell, engine, where = cells[:3]
        tasks = _split_tasks(_strip_md(task_cell))
        for task in tasks:
            rules.append(RoutingRule(task_class=task, engine=_strip_md(engine), where=_strip_md(where)))
    return rules


def select_rule(task_class: str, rules: list[RoutingRule]) -> RoutingRule:
    wanted = _normalize(task_class)
    for rule in rules:
        name = _normalize(rule.task_class)
        if name and (name in wanted or wanted in name):
            return rule
    if any(hint in wanted for hint in CLOUD_HINTS):
        return RoutingRule(task_class=task_class, engine="Claude-class cloud", where="cloud", source="fallback")
    if any(hint in wanted for hint in LOCAL_HINTS):
        return RoutingRule(task_class=task_class, engine="Local LM", where="Stormbreaker", source="fallback")
    return RoutingRule(task_class=task_class, engine="scripts, no model", where="repo", source="fallback")


def merge_resources_line(brief: str, line: str) -> str:
    text = (brief or "").rstrip()
    if not line:
        return text
    if re.search(r"(?m)^RESOURCES:", text):
        return re.sub(r"(?m)^RESOURCES:.*$", line, text).rstrip() + "\n"
    return f"{text}\n\n{line}\n" if text else f"{line}\n"


def estimate_tokens(text: str) -> int:
    return max(0, len(text or "") // 4)


def _section(markdown: str, heading: str) -> str:
    pattern = re.compile(rf"(?im)^##+\s+.*{re.escape(heading)}.*$")
    match = pattern.search(markdown)
    if not match:
        return markdown
    tail = markdown[match.end():]
    next_heading = re.search(r"(?m)^##+\s+", tail)
    return tail[: next_heading.start()] if next_heading else tail


def _split_tasks(task_cell: str) -> list[str]:
    raw_parts = re.split(r",|\band\b", task_cell)
    parts = [part.strip(" .") for part in raw_parts if part.strip(" .")]
    return parts or [task_cell.strip()]


def _strip_md(text: str) -> str:
    return re.sub(r"[*_`]", "", text or "").strip()


def _is_local_engine(engine: str) -> bool:
    normalized = _normalize(engine)
    return "local" in normalized or "lm studio" in normalized or "lm" == normalized


def _is_cloud_engine(engine: str) -> bool:
    normalized = _normalize(engine)
    return "cloud" in normalized or "claude" in normalized


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _selected_model(models: tuple[str, ...], champion_model: str) -> str:
    if champion_model:
        wanted = _normalize(champion_model)
        for model in models:
            normalized = _normalize(model)
            if wanted in normalized or normalized in wanted:
                return model
    return models[0] if models else champion_model


def _health_from_any(value: ConductorHealth | dict) -> ConductorHealth:
    if isinstance(value, ConductorHealth):
        return value
    data = value or {}
    return ConductorHealth(
        lmstudio=_service_from_dict(data.get("lmstudio") or {}),
        vault_rag=_service_from_dict(data.get("vault_rag") or {}),
    )


def _service_from_dict(data: dict) -> ServiceHealth:
    return ServiceHealth(
        name=str(data.get("name") or ""),
        ok=bool(data.get("ok")),
        url=str(data.get("url") or ""),
        latency_ms=data.get("latency_ms"),
        models=tuple(str(model) for model in (data.get("models") or ())),
        error=str(data.get("error") or ""),
    )


def _health_dict(health: ConductorHealth) -> dict:
    return {
        "lmstudio": asdict(health.lmstudio),
        "vault_rag": asdict(health.vault_rag),
    }


def _registry_from_vault(vault_path: Any) -> str | None:
    base = _clean_value(vault_path)
    if not base:
        return None
    return str(Path(base) / "dvc" / "operating-model" / "DVC Machine & Agent Registry.md")


def _empty_tally(date_key: str) -> dict:
    return {
        "date": date_key,
        "local_tasks": 0,
        "cloud_tasks": 0,
        "queued_tasks": 0,
        "script_tasks": 0,
        "estimated_tokens_offloaded": 0,
        "outages": [],
        "last_routes": [],
    }


def _overlay_hand_up(active: dict) -> dict:
    return {
        "active": active.get("status") in {"waiting_for_chair", "approved"},
        "id": active.get("id"),
        "playbook_id": active.get("playbook_id"),
        "playbook_name": active.get("playbook_name"),
        "status": active.get("status"),
        "message": active.get("opener"),
        "opener": active.get("opener"),
        "created_at": active.get("created_at"),
        "approved_at": active.get("approved_at"),
        "snooze_until": active.get("snooze_until"),
        "payload": active.get("payload") or {},
    }


def _headers(api_key: str | None) -> dict[str, str]:
    key = _clean_value(api_key)
    return {"Authorization": f"Bearer {key}"} if key else {}


def _clean_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("${") or text.lower() in {"none", "null"}:
        return None
    return text


def _expand_path(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def _load_json(path: Path) -> dict:
    try:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
