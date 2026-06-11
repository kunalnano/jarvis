"""Chatterbox engine: config parsing and degradation behavior."""

import asyncio
import sys

import pytest

from jarvis.voice import Voice


def make_voice(voice_output):
    return Voice({'voice_output': voice_output})


def test_chatterbox_defaults():
    v = make_voice({'engine': 'chatterbox'})
    assert v.chatterbox_api_base == 'http://localhost:8004'
    assert v.chatterbox_voice == 'default'
    assert v.chatterbox_params == {}


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


@pytest.mark.skipif(sys.platform != 'darwin', reason='fallback target is macOS say')
def test_unreachable_server_falls_back_to_macos():
    # Port 1 is never listening; init must degrade instead of raising
    v = make_voice({
        'engine': 'chatterbox',
        'chatterbox': {'api_base': 'http://127.0.0.1:1'},
    })
    asyncio.run(v.initialize())
    assert v.engine == 'macos'
    assert v.initialized
