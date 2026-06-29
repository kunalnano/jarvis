import asyncio

import httpx
from fastapi.testclient import TestClient

from jarvis import server
from jarvis.server import (
    _convo,
    _fallback_action_summary,
    _fallback_contextual_reply,
    _fallback_web_search_summary,
    _intent_command,
    _looks_incomplete,
    _speech_excerpt,
    app,
)


def test_api_commands_returns_overlay_palette_contract():
    response = TestClient(app).get("/api/commands")

    assert response.status_code == 200
    commands = response.json()["commands"]
    assert commands
    assert {"id", "name", "description", "trigger"} <= set(commands[0])
    assert any(command["id"] == "status" for command in commands)
    assert any(command["id"] == "yennefer-doctor" for command in commands)


def test_voice_status_endpoint_reports_runtime_voice(monkeypatch):
    async def fake_status(probe_chatterbox=False):
        assert probe_chatterbox is True
        return {
            "engine": "elevenlabs",
            "preferred_engine": "chatterbox",
            "degraded": True,
            "fallback_warning": "VOICE WARNING: using ElevenLabs/11 fallback",
        }

    monkeypatch.setattr(server.VOICE, "runtime_status_async", fake_status)

    response = TestClient(app).get("/api/voice/status?probe=true")

    assert response.status_code == 200
    payload = response.json()
    assert payload["engine"] == "elevenlabs"
    assert payload["degraded"] is True
    assert "ElevenLabs/11" in payload["fallback_warning"]


def test_reload_chatterbox_endpoint_promotes_voice(monkeypatch):
    async def fake_promote(probe_audio=False):
        assert probe_audio is True
        return {"promoted": True, "engine": "chatterbox", "previous_engine": "elevenlabs"}

    monkeypatch.setattr(server.VOICE, "try_promote_chatterbox", fake_promote)

    response = TestClient(app).post("/api/voice/reload-chatterbox")

    assert response.status_code == 200
    assert response.json()["promoted"] is True


def test_yennefer_doctor_command_invokes_repair_script(monkeypatch):
    calls = []

    async def fake_run(cmd, timeout=60.0, cwd=None):
        calls.append((cmd, timeout, cwd))
        return "OK    Yennefer backend ready"

    monkeypatch.setattr("jarvis.tools._run", fake_run)

    response = TestClient(app).post(
        "/api/chat",
        json={"message": "repair yennefer services", "speak": False},
    )

    assert response.status_code == 200
    assert response.json()["command"] == "yennefer-doctor"
    assert calls
    assert calls[0][0][-1] == "--repair"


def test_check_system_status_uses_fast_command_path(monkeypatch):
    async def fake_execute(name, args, cfg):
        assert name == "system_status"
        assert args == {}
        return "== identity ==\ncanonical_name: Prometheus\n\n== uptime ==\nup"

    monkeypatch.setattr("jarvis.server.tools.execute", fake_execute)
    response = TestClient(app).post(
        "/api/chat",
        json={"message": "check system status", "speak": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["command"] == "status"
    assert payload["actions"][0]["tool"] == "system_status"
    assert "Prometheus" in payload["reply"]


def test_check_system_status_with_speak_queues_voice(monkeypatch):
    async def fake_execute(name, args, cfg):
        return "== identity ==\ncanonical_name: Prometheus\n\n== uptime ==\nup"

    queued = []
    monkeypatch.setattr("jarvis.server.tools.execute", fake_execute)
    monkeypatch.setattr("jarvis.server._queue_speech", queued.append)

    response = TestClient(app).post(
        "/api/chat",
        json={"message": "check system status", "speak": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert queued == [payload["reply"]]


def test_background_speech_coalesces_overlapping_requests(monkeypatch):
    spoken = []

    async def fake_speak(text):
        spoken.append(text)
        if text == "first":
            server._queue_speech("second")
            server._queue_speech("third")

    monkeypatch.setattr(server.VOICE, "speak", fake_speak)
    server.SPEECH_NEXT = None
    server.SPEECH_WORKER = None

    async def run_worker():
        server._queue_speech("first")
        await server.SPEECH_WORKER

    asyncio.run(run_worker())

    assert spoken == ["first", "third"]
    server.SPEECH_NEXT = None
    server.SPEECH_WORKER = None


def test_conversation_history_keeps_root_and_recent_messages(monkeypatch):
    monkeypatch.setattr(server, "HISTORY", [{"role": "system", "content": "root"}])

    for idx in range(server.CONVERSATION_MESSAGE_LIMIT + 5):
        server._remember("user", f"message {idx}")

    convo = server._convo()

    assert convo[0] == {"role": "system", "content": "root"}
    assert len(convo) == server.CONVERSATION_MESSAGE_LIMIT + 1
    assert convo[1]["content"] == "message 5"


def test_tool_result_is_kept_as_followup_context(monkeypatch):
    monkeypatch.setattr(server, "HISTORY", [{"role": "system", "content": "root"}])

    server._remember_tool_result(
        "web_search",
        {"query": "FIFA World Cup soccer fixtures"},
        "Search results for: FIFA World Cup soccer fixtures\nLikely fixtures: Panama vs England.",
    )

    convo = server._convo()
    context = convo[0]["content"]

    assert len(convo) == 1
    assert context.startswith("root")
    assert "Recent tool result for follow-ups: web_search" in context
    assert "query=FIFA World Cup soccer fixtures" in context
    assert "Likely fixtures: Panama vs England." in context


def test_web_search_fallback_answers_team_followup_from_recent_fixture_context(monkeypatch):
    monkeypatch.setattr(server, "HISTORY", [{"role": "system", "content": "root"}])
    server._remember_tool_result(
        "web_search",
        {"query": "FIFA World Cup soccer fixtures"},
        "Search results for: FIFA World Cup soccer fixtures\nLikely fixtures: Panama vs England; Croatia vs Ghana.",
    )

    reply = _fallback_web_search_summary(
        "\n".join([
            "Search results for: England World Cup match today",
            "Source: Serper Google Search",
            "1. England | Squad, Fixtures & News | FIFA World Cup 2026",
            "   Visit FIFA.com to find England fixtures and results.",
        ])
    )

    assert reply == "England are playing Panama today."


def test_contextual_fallback_answers_plain_team_followup(monkeypatch):
    monkeypatch.setattr(server, "HISTORY", [{"role": "system", "content": "root"}])
    server._remember_tool_result(
        "web_search",
        {"query": "FIFA World Cup soccer fixtures"},
        "Search results for: FIFA World Cup soccer fixtures\nLikely fixtures: Panama vs England; Croatia vs Ghana.",
    )

    assert _fallback_contextual_reply("what about England?") == "England are playing Panama today."


def test_weather_uses_fast_command_path(monkeypatch):
    async def fake_execute(name, args, cfg):
        assert name == "weather"
        assert args == {}
        return "Weather for San Antonio: 82F, clear."

    monkeypatch.setattr("jarvis.server.tools.execute", fake_execute)
    response = TestClient(app).post(
        "/api/chat",
        json={"message": "what's the weather", "speak": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["command"] == "weather"
    assert payload["reply"] == "Weather for San Antonio: 82F, clear."


def test_weather_intent_accepts_natural_phrasing():
    command = _intent_command("tell me the weather in San Antonio, TX")

    assert command["tool"] == "weather"
    assert command["args"]["location"] == "San Antonio, TX"


def test_sports_today_intent_routes_to_live_web_search():
    command = _intent_command("who is playing today in fifa world cup")

    assert command["tool"] == "web_search"
    assert "FIFA World Cup soccer fixtures" in command["args"]["query"]


def test_generic_world_cup_today_defaults_to_fifa_soccer_context():
    command = _intent_command("who is playing in the world cup today")

    assert command["tool"] == "web_search"
    assert command["args"]["query"].startswith("FIFA World Cup soccer fixtures")


def test_world_cup_elimination_query_searches_standings_context():
    command = _intent_command("if England loses today are they eliminated from FIFA World Cup")

    assert command["tool"] == "web_search"
    assert "FIFA soccer standings qualification scenario" in command["args"]["query"]
    assert "England loses" in command["args"]["query"]


def test_web_search_fast_path_answers_from_results(monkeypatch):
    async def fake_execute(name, args, cfg):
        assert name == "web_search"
        return "\n".join([
            "Search results for: FIFA World Cup fixtures June 27 2026",
            "Source: Bing RSS fallback",
            "Likely fixtures: England vs Panama; Portugal vs Colombia.",
            "1. Soccer Match Fixtures - 27 June 2026",
            "   England will face Panama, Portugal will take on Colombia.",
        ])

    async def fake_complete(messages, with_tools=True):
        assert with_tools is False
        prompt = messages[-1]["content"]
        assert "The user asked: who is playing today in fifa world cup" in prompt
        assert "Do not dump the raw search results" in prompt
        assert "Ask at most one short follow-up question" in prompt
        return {"content": "Today, the likely World Cup fixtures are England vs Panama and Portugal vs Colombia."}

    monkeypatch.setattr("jarvis.server.tools.execute", fake_execute)
    monkeypatch.setattr("jarvis.server._complete", fake_complete)

    response = TestClient(app).post(
        "/api/chat",
        json={"message": "who is playing today in fifa world cup", "speak": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["command"] == "web-search"
    assert payload["reply"] == "Today, the likely World Cup fixtures are England vs Panama and Portugal vs Colombia."
    assert "Search results for:" not in payload["reply"]


def test_web_search_reasoning_leak_uses_fixture_fallback(monkeypatch):
    async def fake_execute(name, args, cfg):
        assert name == "web_search"
        return "\n".join([
            "Search results for: FIFA World Cup fixtures June 27 2026",
            "Source: Serper Google Search",
            "Likely fixtures: England vs Panama; Portugal vs Colombia.",
            "1. Soccer Match Fixtures - 27 June 2026",
            "   England will face Panama, Portugal will take on Colombia.",
        ])

    async def fake_complete(messages, with_tools=True):
        assert with_tools is False
        return {
            "content": (
                "The user is asking about the stakes of the matches scheduled for today. "
                "I need to synthesize the information from the search results."
            )
        }

    monkeypatch.setattr("jarvis.server.tools.execute", fake_execute)
    monkeypatch.setattr("jarvis.server._complete", fake_complete)

    response = TestClient(app).post(
        "/api/chat",
        json={"message": "who is playing today in fifa world cup", "speak": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["reply"] == "Likely fixtures: England vs Panama; Portugal vs Colombia."
    assert "The user is asking" not in payload["reply"]


def test_web_search_elimination_reasoning_leak_uses_snippet_fallback(monkeypatch):
    async def fake_execute(name, args, cfg):
        assert name == "web_search"
        return "\n".join([
            "Search results for: if England loses today are they eliminated from FIFA World Cup FIFA soccer standings qualification scenario June 27 2026",
            "Source: Serper Google Search",
            "1. 2026 World Cup: How teams can advance to the knockout rounds",
            "   England's 0-0 draw with Ghana delayed their automatic progression, but results elsewhere mean they have confirmed progression into the next round.",
            "   https://example.com/england-scenarios",
        ])

    async def fake_complete(messages, with_tools=True):
        assert with_tools is False
        return {
            "content": (
                "The user is asking about whether England would be eliminated. "
                "I need to synthesize the search results."
            )
        }

    monkeypatch.setattr("jarvis.server.tools.execute", fake_execute)
    monkeypatch.setattr("jarvis.server._complete", fake_complete)

    response = TestClient(app).post(
        "/api/chat",
        json={"message": "if England loses today are they eliminated from FIFA World Cup", "speak": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["reply"] == "The snippets say England have confirmed progression, so a loss should not eliminate them. I'd verify the full standings before treating that as final."
    assert "I need to" not in payload["reply"]


def test_open_chat_retries_empty_reasoning_response_with_larger_budget(monkeypatch):
    calls = []

    async def fake_complete(messages, with_tools=True, max_tokens=None):
        calls.append({"with_tools": with_tools, "max_tokens": max_tokens})
        if len(calls) == 1:
            return {"content": "", "reasoning_content": "Thinking Process: still reasoning"}
        return {"content": "ready"}

    async def fake_playbook(message):
        return None

    monkeypatch.setattr("jarvis.server._complete", fake_complete)
    monkeypatch.setattr("jarvis.server.PLAYBOOKS.handle_chat", fake_playbook)
    server.HISTORY[:] = [{"role": "system", "content": server.SYSTEM}]

    response = TestClient(app).post(
        "/api/chat",
        json={"message": "Say exactly: ready", "speak": False},
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "ready"
    assert calls == [
        {"with_tools": True, "max_tokens": None},
        {"with_tools": False, "max_tokens": 1536},
    ]


def test_open_chat_handles_lm_studio_auth_failure_as_reply(monkeypatch):
    async def fake_complete(messages, with_tools=True, max_tokens=None):
        request = httpx.Request("POST", "http://127.0.0.1:1234/v1/chat/completions")
        response = httpx.Response(401, request=request, text="unauthorized")
        raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)

    async def fake_playbook(message):
        return None

    monkeypatch.setattr("jarvis.server._complete", fake_complete)
    monkeypatch.setattr("jarvis.server.PLAYBOOKS.handle_chat", fake_playbook)
    server.HISTORY[:] = [{"role": "system", "content": server.SYSTEM}]

    response = TestClient(app).post(
        "/api/chat",
        json={"message": "Say hello", "speak": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["model_error"] is True
    assert "API token" in payload["reply"]
    assert "Command actions still work" in payload["reply"]


def test_conversation_keeps_tool_context_in_front_system_message():
    server.HISTORY[:] = [
        {"role": "system", "content": "root prompt"},
        {"role": "user", "content": "what is the weather"},
        {"role": "system", "content": "Recent tool result for follow-ups: weather. Sunny."},
        {"role": "assistant", "content": "Sunny."},
        {"role": "user", "content": "Say exactly: ready"},
    ]

    messages = _convo()

    assert [message["role"] for message in messages] == ["system", "user", "assistant", "user"]
    assert "root prompt" in messages[0]["content"]
    assert "Recent tool result for follow-ups: weather. Sunny." in messages[0]["content"]


def test_capabilities_intent_accepts_help_phrasing():
    command = _intent_command("what can you do?")

    assert command["tool"] == "capabilities"


def test_browser_intent_accepts_natural_phrasing():
    command = _intent_command("what am I looking at on this page?")

    assert command["tool"] == "observe_browser"


def test_url_intent_accepts_page_reading_request():
    command = _intent_command("read this page https://example.com/test?x=1")

    assert command["tool"] == "web_fetch"
    assert command["args"]["url"] == "https://example.com/test?x=1"


def test_browser_speech_excerpt_does_not_read_whole_page():
    text = (
        "Active browser tab: Example Product\n"
        "Fetched: https://example.com\n"
        "Title: Example Product\n"
        "Text:\n"
        + ("Long product text " * 200)
    )

    spoken = _speech_excerpt(text)

    assert "I can see the active browser tab: Example Product." in spoken
    assert len(spoken) < 120


def test_search_speech_excerpt_does_not_read_all_results():
    text = "\n".join([
        "Search results for: world cup",
        "Source: Bing RSS fallback",
        "Likely fixtures: England vs Panama; Portugal vs Colombia.",
        "1. FIFA World Cup fixtures today",
        "   England face Panama.",
        "2. ESPN schedule",
        "   More fixtures.",
    ])

    spoken = _speech_excerpt(text)

    assert spoken == "Likely fixtures: England vs Panama; Portugal vs Colombia."


def test_system_status_summary_is_speakable():
    result = "\n".join([
        "== identity ==",
        "canonical_name: Prometheus",
        "macos_reported_local_hostname: Als-MacBook-Pro.local",
        "== uptime ==",
        "10:57 up 1 day, load averages: 3.0 2.0 1.0",
        "== disk ==",
        "Filesystem Size Used Avail Capacity iused ifree %iused Mounted on",
        "/dev/disk3s1s1 926Gi 12Gi 608Gi 2% 480k 4.3G 0% /",
    ])

    reply = _fallback_action_summary("system_status", result)

    assert reply.startswith("Ran system status. Prometheus is online.")
    assert "canonical_name" not in reply


def test_ellipsis_counts_as_incomplete_reply():
    assert _looks_incomplete("...")
    assert _looks_incomplete("…")


def test_short_complete_reply_without_punctuation_is_allowed():
    assert not _looks_incomplete("ready")
