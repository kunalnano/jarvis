import os
from pathlib import Path

import yaml

from jarvis import config


def test_load_dotenv_empty_value_does_not_clear_existing_env(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("ELEVENLABS_API_KEY=\nELEVENLABS_VOICE_ID=voice123\n")
    monkeypatch.setattr(config, "__file__", str(tmp_path / "jarvis" / "config.py"))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "real-key")

    config.load_dotenv()

    assert os.environ["ELEVENLABS_API_KEY"] == "real-key"
    assert os.environ["ELEVENLABS_VOICE_ID"] == "voice123"


def test_default_voice_config_targets_local_chatterbox_voice():
    config_path = Path(__file__).parents[1] / "config" / "jarvis.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert data["voice_output"]["engine"] == "chatterbox"
    assert data["voice_output"]["allow_elevenlabs_fallback"] is True
    assert data["voice_output"]["voice_id"] == "${ELEVENLABS_VOICE_ID}"
    assert data["voice_output"]["chatterbox"]["api_base"] == "http://127.0.0.1:8004"
    assert data["voice_output"]["chatterbox"]["startup_wait_seconds"] == 90
    assert data["voice_output"]["chatterbox"]["retry_interval_seconds"] == 5
    assert data["voice_output"]["allow_macos_fallback"] is False


def test_default_llm_config_is_prometheus_first_stormbreaker_second():
    config_path = Path(__file__).parents[1] / "config" / "jarvis.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert data["llm"]["api_base"] == "http://127.0.0.1:1234/v1"
    assert data["llm"]["api_key"] == "${LM_API_TOKEN}"
    assert data["llm"]["fallbacks"][0]["name"] == "stormbreaker-lm-studio"
    assert data["llm"]["fallbacks"][0]["api_base"] == "http://192.168.4.48:1234/v1"
