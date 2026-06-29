import asyncio

import httpx

from jarvis.brain import Brain


def test_lmstudio_fallback_tries_prometheus_before_stormbreaker(monkeypatch):
    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None, timeout=None):
            calls.append((url, headers or {}, json["model"]))
            request = httpx.Request("POST", url)
            if "127.0.0.1" in url:
                response = httpx.Response(401, request=request, text="unauthorized")
                raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)
            return httpx.Response(
                200,
                request=request,
                json={"choices": [{"message": {"content": "ready"}}]},
            )

    monkeypatch.setattr("jarvis.brain.httpx.AsyncClient", FakeClient)

    brain = Brain({
        "llm": {
            "api_base": "http://127.0.0.1:1234/v1",
            "api_key": "mac-token",
            "model": "mac-model",
            "fallbacks": [
                {
                    "name": "stormbreaker-lm-studio",
                    "api_base": "http://192.168.4.48:1234/v1",
                    "api_key": "windows-token",
                    "model": "windows-model",
                }
            ],
        }
    })

    result = asyncio.run(brain.chat_completion({"model": "mac-model", "messages": []}))

    assert result["choices"][0]["message"]["content"] == "ready"
    assert calls == [
        ("http://127.0.0.1:1234/v1/chat/completions", {"Authorization": "Bearer mac-token"}, "mac-model"),
        ("http://192.168.4.48:1234/v1/chat/completions", {"Authorization": "Bearer windows-token"}, "windows-model"),
    ]
    assert brain.api_base == "http://192.168.4.48:1234/v1"
