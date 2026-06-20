import os

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


def test_history_payload_excludes_system_prompt_by_default():
    server.HISTORY.append({"role": "user", "content": "What happened?"})
    server.HISTORY.append({"role": "assistant", "content": "I checked the web."})

    payload = server._history_payload()

    assert payload["retention"] == "memory-only"
    assert payload["message_count"] == 2
    assert payload["messages"] == [
        {"role": "user", "content": "What happened?"},
        {"role": "assistant", "content": "I checked the web."},
    ]


def test_history_payload_can_include_full_model_context():
    server.HISTORY.append({"role": "user", "content": "Keep the system message too."})

    payload = server._history_payload(include_system=True)

    assert payload["message_count"] == 2
    assert payload["messages"][0]["role"] == "system"
    assert "You have hands" in payload["messages"][0]["content"]
    assert payload["messages"][1] == {
        "role": "user",
        "content": "Keep the system message too.",
    }


def test_history_markdown_exports_transcript():
    server.HISTORY.append({"role": "user", "content": "Capture this."})
    server.HISTORY.append({"role": "assistant", "content": "Captured."})

    markdown = server._history_markdown()

    assert markdown.startswith("# Yennefer Chat History")
    assert "- Retention: memory-only" in markdown
    assert "## User\n\nCapture this." in markdown
    assert "## Assistant\n\nCaptured." in markdown
    assert "## System" not in markdown


def test_reset_history_clears_chat_but_preserves_system_prompt():
    server.HISTORY.append({"role": "user", "content": "Temporary."})

    server._reset_history()

    assert server._history_payload()["messages"] == []
    assert len(server.HISTORY) == 1
    assert server.HISTORY[0]["role"] == "system"
