"""Kokoro engine: config parsing and degraded fallback behavior."""

import asyncio

from jarvis.voice import Voice, clean_for_kokoro, kokoro_chunks, tts_chunks


def make_voice(voice_output):
    return Voice({'voice_output': voice_output})


def test_kokoro_defaults():
    v = make_voice({'engine': 'kokoro'})
    assert v.kokoro_model == 'prince-canuma/Kokoro-82M'
    assert v.kokoro_voice == 'bf_emma'
    assert v.kokoro_extra_args == []


def test_kokoro_config_parsed():
    v = make_voice({
        'engine': 'kokoro',
        'speed': 1.15,
        'kokoro': {
            'model': 'prince-canuma/Kokoro-82M-bf16',
            'voice': 'af_heart',
            'extra_args': ['--temperature', 0.7],
        },
    })
    assert v.kokoro_model == 'prince-canuma/Kokoro-82M-bf16'
    assert v.kokoro_voice == 'af_heart'
    assert v.kokoro_speed == 1.15  # inherits voice_output speed when unset
    assert v.kokoro_extra_args == ['--temperature', '0.7']  # coerced to str


def test_kokoro_speed_override():
    v = make_voice({
        'engine': 'kokoro',
        'speed': 1.0,
        'kokoro': {'speed': 1.2},
    })
    assert v.kokoro_speed == 1.2


def test_clean_for_kokoro_simplifies_status_text():
    text = (
        "Ran system_status. == identity == canonical_name: Prometheus "
        "tailscale_dns: als-macbook-pro.taild13a57.ts.net"
    )

    cleaned = clean_for_kokoro(text)

    assert "system status" in cleaned
    assert "==" not in cleaned
    assert "tailscale dns" in cleaned
    assert "taild13a57" not in cleaned
    assert clean_for_kokoro("608Gi available.") == "608 gigabytes available."


def test_tts_chunks_splits_long_text():
    chunks = tts_chunks("word " * 80, limit=60)

    assert len(chunks) > 1
    assert all(len(chunk) <= 60 for chunk in chunks)


def test_kokoro_chunks_prefers_sentence_boundaries():
    chunks = kokoro_chunks("Ran system status. Prometheus is online. Disk space is healthy.")

    assert chunks == [
        "Ran system status.",
        "Prometheus is online.",
        "Disk space is healthy.",
    ]


def test_chatterbox_fallback_prefers_elevenlabs_when_explicitly_allowed():
    """11 may be used as fallback only when explicitly configured and warned."""
    v = make_voice({
        'engine': 'chatterbox',
        'voice_id': 'abc123',
        'api_key': 'k-test',
        'allow_elevenlabs_fallback': True,
    })
    called = []

    v._kokoro_available = lambda: True

    async def fake_init_kokoro():
        called.append('kokoro')
        v.initialized = True
        v.engine = 'kokoro'

    async def fake_init_elevenlabs():
        called.append('elevenlabs')
        v.initialized = True
        v.engine = 'elevenlabs'

    v._init_kokoro = fake_init_kokoro
    v._init_elevenlabs = fake_init_elevenlabs

    asyncio.run(v._fallback_from_chatterbox())
    assert called == ['elevenlabs']
    assert v.engine == 'elevenlabs'
    assert v.degraded
    assert "ElevenLabs/11 fallback" in v.fallback_warning


def test_chatterbox_fallback_skips_elevenlabs_when_not_explicitly_allowed():
    v = make_voice({
        'engine': 'chatterbox',
        'voice_id': 'abc123',
        'api_key': 'k-test',
    })
    called = []

    v._kokoro_available = lambda: True

    async def fake_init_elevenlabs():
        called.append('elevenlabs')
        v.initialized = True
        v.engine = 'elevenlabs'

    async def fake_init_kokoro():
        called.append('kokoro')
        v.initialized = True
        v.engine = 'kokoro'

    v._init_elevenlabs = fake_init_elevenlabs
    v._init_kokoro = fake_init_kokoro

    asyncio.run(v._fallback_from_chatterbox())
    assert called == ['kokoro']
    assert v.engine == 'kokoro'
    assert "Kokoro fallback" in v.fallback_warning


def test_chatterbox_fallback_uses_kokoro_when_elevenlabs_missing():
    """Without the ElevenLabs key, the chain continues to local Kokoro."""
    v = make_voice({'engine': 'chatterbox'})
    called = []

    v._kokoro_available = lambda: True

    async def fake_init_kokoro():
        called.append('kokoro')
        v.initialized = True
        v.engine = 'kokoro'

    v._init_kokoro = fake_init_kokoro

    asyncio.run(v._fallback_from_chatterbox())
    assert called == ['kokoro']
    assert v.engine == 'kokoro'
