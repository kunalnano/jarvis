import os

from fastapi.testclient import TestClient

from jarvis import server


def _clear_loaded_shortcuts_token():
    os.environ.pop("YENNEFER_SHORTCUTS_TOKEN", None)


_clear_loaded_shortcuts_token()


def setup_function():
    _clear_loaded_shortcuts_token()
    server._reset_history()


def teardown_function():
    server._reset_history()
    _clear_loaded_shortcuts_token()


def test_chat_auto_searches_when_model_misses_tool_call(monkeypatch):
    calls = []

    async def fake_complete(messages, with_tools=True, max_tokens=None):
        if with_tools:
            return {
                "role": "assistant",
                "content": "",
                "reasoning_content": "I need to call web_search for current sources.",
                "tool_calls": [],
            }
        calls.append(("summary", messages[-1]["content"]))
        return {
            "role": "assistant",
            "content": "Verified the situation from current sources.",
        }

    async def fake_execute(name, args, config):
        calls.append((name, args))
        return (
            "Search results for: Anthropic Fable 5 export-control\n"
            "1. Source\n"
            "   URL: https://source.test/story\n"
            "Fetched excerpts:\nFable 5 access was suspended."
        )

    monkeypatch.setattr(server, "_complete", fake_complete)
    monkeypatch.setattr(server.tools, "execute", fake_execute)

    response = TestClient(server.app).post(
        "/api/chat",
        json={
            "message": "What is the Anthropic Fable 5 export-control situation? Cite sources.",
            "speak": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["actions"][0]["tool"] == "web_search"
    assert body["actions"][0]["args"]["max_results"] == 3
    assert "https://source.test/story" in body["reply"]
    assert calls[0][0] == "web_search"
    assert calls[1][0] == "summary"


def test_auto_search_args_ignore_ordinary_prompts():
    assert server._auto_search_args("Tell me a joke.", {"content": "Fine.", "tool_calls": []}) is None


def test_retrieval_summary_replaces_empty_done_reply():
    result = (
        "Search results for: Anthropic Fable 5 export control situation\n"
        "1. Anthropic statement\n"
        "   URL: https://anthropic.example/news\n"
        "   Snippet: The US government issued an export control directive.\n"
        "2. Legal analysis\n"
        "   URL: https://law.example/fable\n"
        "   Snippet: The directive suspended access to Fable 5 and Mythos 5.\n"
    )

    reply = server._with_source_urls(
        server._retrieval_summary("Done.", result),
        result,
    )

    assert "I retrieved current web evidence." in reply
    assert "export control directive" in reply
    assert "suspended access to Fable 5 and Mythos 5" in reply
    assert "https://anthropic.example/news" in reply


def test_direct_tool_summary_replaces_empty_done_reply():
    linear = server._direct_tool_summary(
        "linear_counts",
        "Done.",
        "Linear active issue counts (scope: states=Todo; active_only=True): Todo: 39",
    )
    screen = server._direct_tool_summary(
        "screen_context",
        "Done.",
        "Current screen context:\n- Front app: Ghostty\n- Screenshot saved: /tmp/screen.png",
    )

    assert "Todo: 39" in linear
    assert "Screenshot saved: /tmp/screen.png" in screen
