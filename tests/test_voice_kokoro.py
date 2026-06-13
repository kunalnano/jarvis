"""Kokoro engine: config parsing and Flight 002 fallback-chain ordering."""

import asyncio

from jarvis.voice import Voice


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


def test_chatterbox_fallback_prefers_kokoro():
    """Flight 002: chatterbox degrades to kokoro before the drip."""
    v = make_voice({'engine': 'chatterbox'})
    called = []

    v._kokoro_available = lambda: True

    async def fake_init_kokoro():
        called.append('kokoro')
        v.initialized = True
        v.engine = 'kokoro'

    async def fake_init_elevenlabs():
        called.append('elevenlabs')

    v._init_kokoro = fake_init_kokoro
    v._init_elevenlabs = fake_init_elevenlabs

    asyncio.run(v._fallback_from_chatterbox())
    assert called == ['kokoro']
    assert v.engine == 'kokoro'


def test_chatterbox_fallback_skips_unavailable_kokoro():
    """Without mlx-audio the chain continues past kokoro instead of dying."""
    v = make_voice({
        'engine': 'chatterbox',
        'voice_id': 'abc123',
        'api_key': 'k-test',
    })
    called = []

    v._kokoro_available = lambda: False

    async def fake_init_kokoro():
        called.append('kokoro')

    async def fake_init_elevenlabs():
        called.append('elevenlabs')
        v.initialized = True
        v.engine = 'elevenlabs'

    v._init_kokoro = fake_init_kokoro
    v._init_elevenlabs = fake_init_elevenlabs

    asyncio.run(v._fallback_from_chatterbox())
    assert called == ['elevenlabs']
    assert v.engine == 'elevenlabs'
