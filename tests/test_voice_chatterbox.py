"""Chatterbox engine: config parsing and degradation behavior."""

import asyncio

from jarvis.voice import Voice


def make_voice(voice_output):
    return Voice({'voice_output': voice_output})


def test_chatterbox_defaults():
    v = make_voice({'engine': 'chatterbox'})
    assert v.chatterbox_api_base == 'http://localhost:8004'
    assert v.chatterbox_voice == 'default'
    assert v.chatterbox_params == {}
    assert v.chatterbox_startup_wait_seconds == 0
    assert v.degraded is False


def test_chatterbox_config_parsed():
    v = make_voice({
        'engine': 'chatterbox',
        'speed': 1.15,
        'chatterbox': {
            'api_base': 'http://stormbreaker.example:8004/',
            'voice': 'yennefer.wav',
            'params': {'exaggeration': 0.6},
        },
    })
    assert v.chatterbox_api_base == 'http://stormbreaker.example:8004'  # trailing slash stripped
    assert v.chatterbox_voice == 'yennefer.wav'
    assert v.chatterbox_speed == 1.15  # inherits voice_output speed when unset
    assert v.chatterbox_params == {'exaggeration': 0.6}


def test_chatterbox_speed_override():
    v = make_voice({
        'engine': 'chatterbox',
        'speed': 1.0,
        'chatterbox': {'speed': 1.2},
    })
    assert v.chatterbox_speed == 1.2


def test_unreachable_server_marks_degraded_fallback(monkeypatch):
    # Port 1 is never listening; init must degrade instead of raising
    v = make_voice({
        'engine': 'chatterbox',
        'chatterbox': {'api_base': 'http://127.0.0.1:1'},
    })
    monkeypatch.setattr(v, "_kokoro_available", lambda: True)

    async def fake_init_kokoro():
        v.initialized = True
        v.engine = 'kokoro'

    monkeypatch.setattr(v, "_init_kokoro", fake_init_kokoro)

    asyncio.run(v.initialize())
    assert v.engine == 'kokoro'
    assert v.initialized
    assert v.degraded
    assert "Kokoro fallback" in v.fallback_warning


def test_missing_chatterbox_reference_fails_health_before_http(tmp_path):
    missing = tmp_path / "missing.wav"
    v = make_voice({
        'engine': 'chatterbox',
        'chatterbox': {'voice': str(missing)},
    })

    health = asyncio.run(v._check_chatterbox_health())

    assert health["ok"] is False
    assert health["checks"]["reference_audio"] == "missing"
    assert "reference audio not found" in health["reason"]


def test_promote_chatterbox_clears_degraded_warning(monkeypatch):
    v = make_voice({'engine': 'chatterbox'})
    v.engine = 'elevenlabs'
    v.initialized = True
    v._mark_degraded('elevenlabs', 'startup race')

    async def healthy(*, probe_audio=False):
        return {'ok': True, 'checks': {'health_endpoint': 'ok'}, 'reason': ''}

    monkeypatch.setattr(v, "_check_chatterbox_health", healthy)

    result = asyncio.run(v.try_promote_chatterbox(probe_audio=True))

    assert result["promoted"] is True
    assert v.engine == 'chatterbox'
    assert v.initialized
    assert v.degraded is False
    assert v.fallback_warning == ""
