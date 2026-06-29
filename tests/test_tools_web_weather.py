import asyncio

from jarvis import tools


def test_web_html_extractor_skips_scripts_and_keeps_title():
    title, text = tools._html_to_text(
        "<html><head><title>Example Page</title><script>bad()</script></head>"
        "<body><h1>Hello</h1><p>Readable text.</p></body></html>"
    )

    assert title == "Example Page"
    assert "Hello" in text
    assert "Readable text." in text
    assert "bad()" not in text


def test_web_search_uses_bing_rss_fallback_without_api_keys(monkeypatch):
    for key in (
        "SERPER_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_SEARCH_API_KEY",
        "GOOGLE_SEARCH_ENGINE_ID",
        "GOOGLE_CSE_ID",
        "BRAVE_API_KEY",
        "BRAVE_SEARCH_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    def fake_fetch(url, timeout=12.0, max_bytes=1_000_000):
        assert "bing.com/search" in url
        return "application/rss+xml", """
        <rss><channel>
          <item>
            <title>FIFA World Cup fixtures today</title>
            <link>https://example.com/world-cup</link>
            <description>England will face Panama, Portugal will take on Colombia.</description>
            <pubDate>Sat, 27 Jun 2026 17:11:00 GMT</pubDate>
          </item>
        </channel></rss>
        """

    monkeypatch.setattr(tools, "_fetch_url", fake_fetch)

    result = asyncio.run(tools.web_search({"query": "world cup today"}, {}))

    assert "Source: Bing RSS fallback" in result
    assert "Likely fixtures: England vs Panama; Portugal vs Colombia." in result
    assert "England will face Panama" in result
    assert "https://example.com/world-cup" in result


def test_fixture_highlights_strip_kickoff_timezones_from_team_names():
    results = [{
        "title": "Match Schedule - FIFA World Cup 26 Dallas",
        "snippet": (
            "JUNE 27, 2026. DALLAS STADIUM, ARLINGTON, TEXAS | "
            "9:00 PM CT Jordan vs. Argentina delivers a Group Stage matchup."
        ),
    }]

    fixtures = tools._fixture_highlights(results, "FIFA World Cup fixtures June 27 2026")

    assert fixtures == ["Jordan vs Argentina"]


def test_fixture_highlights_strip_venue_prefixes_from_team_names():
    results = [{
        "title": "World Cup 2026 | Match schedule, fixtures & stadiums - FIFA",
        "snippet": (
            "Saturday, 27 June 2026 Panama v England - Group L - "
            "New York New Jersey Stadium Croatia v Ghana - Group L - Philadelphia."
        ),
    }]

    fixtures = tools._fixture_highlights(results, "FIFA World Cup soccer fixtures June 27 2026")

    assert "Panama vs England" in fixtures
    assert "Croatia vs Ghana" in fixtures
    assert all("Stadium" not in fixture for fixture in fixtures)


def test_fifa_fixture_highlights_ignore_non_soccer_world_cup_results():
    results = [{
        "title": "World Cup cricket fixtures today",
        "snippet": "Ireland vs India is scheduled for today.",
    }]

    fixtures = tools._fixture_highlights(results, "FIFA World Cup soccer fixtures June 27 2026")

    assert fixtures == []


def test_weather_browser_and_search_tools_are_registered_safe():
    assert tools.is_safe("web_search")
    assert tools.is_safe("weather")
    assert tools.is_safe("web_fetch")
    assert tools.is_safe("observe_browser")

    names = {item["function"]["name"] for item in tools.openai_tools()}
    assert {"web_search", "weather", "web_fetch", "observe_browser"} <= names
