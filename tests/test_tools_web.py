"""Web retrieval tools: mocked network, no live internet."""

import asyncio

import httpx

from jarvis import tools


def run(coro):
    return asyncio.run(coro)


def test_fetch_url_returns_extracted_page_text_without_script_content():
    def handler(request):
        assert str(request.url) == "https://example.test/fable"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="""
            <html>
              <head><script>secretToken()</script></head>
              <body>
                <h1>Anthropic Fable 5 suspension</h1>
                <p>Export-control review paused the launch on June 12, 2026.</p>
              </body>
            </html>
            """,
        )

    result = run(
        tools.execute(
            "fetch_url",
            {"url": "https://example.test/fable"},
            {"_http_transport": httpx.MockTransport(handler)},
        )
    )

    assert "Fetched https://example.test/fable" in result
    assert "Anthropic Fable 5 suspension" in result
    assert "Export-control review paused the launch" in result
    assert "secretToken" not in result


def test_fetch_url_rejects_non_http_urls():
    result = run(tools.execute("fetch_url", {"url": "file:///etc/passwd"}, {}))

    assert result == "Refusing to fetch non-http URL: file:///etc/passwd"


def test_web_search_returns_results_and_top_result_excerpt():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "duckduckgo.com":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="""
                <html>
                  <body>
                    <a class="result__a" href="/l/?uddg=https%3A%2F%2Fsource.test%2Fstory">
                      Fable 5 export-control pause
                    </a>
                    <a class="result__snippet">
                      Anthropic suspended Fable 5 exports pending review.
                    </a>
                  </body>
                </html>
                """,
            )
        if request.url.host == "source.test":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<main><h1>Fable 5 paused</h1><p>Regulators opened a review.</p></main>",
            )
        return httpx.Response(404)

    result = run(
        tools.execute(
            "web_search",
            {"query": "Anthropic Fable 5 export-control", "max_results": 1},
            {"_http_transport": httpx.MockTransport(handler)},
        )
    )

    assert "Search results for: Anthropic Fable 5 export-control" in result
    assert "Fable 5 export-control pause" in result
    assert "https://source.test/story" in result
    assert "Anthropic suspended Fable 5 exports pending review" in result
    assert "Fetched excerpts:" in result
    assert "Fable 5 paused" in result
    assert "Regulators opened a review" in result
    assert any("duckduckgo.com/html" in url for url in seen)
    assert any("source.test/story" in url for url in seen)


def test_web_tools_are_registered_as_safe_openai_tools():
    names = {spec["function"]["name"] for spec in tools.openai_tools()}

    assert {"web_search", "fetch_url"}.issubset(names)
    assert tools.is_safe("web_search") is True
    assert tools.is_safe("fetch_url") is True
