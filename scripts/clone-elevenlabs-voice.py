#!/usr/bin/env python3
"""Clone or duplicate Yennefer's ElevenLabs voice when the account tier allows it."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from elevenlabs import VoiceSettings
from elevenlabs.client import ElevenLabs
from elevenlabs.core.api_error import ApiError

from jarvis.config import load_config


OUT_DIR = Path("/Users/alsharma/Projects/yennefer/.local/voice-clones")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def api_error_message(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict):
            return detail.get("message") or str(detail)
    return str(exc)


def save_source_sample(client: ElevenLabs, source, source_voice_id: str, out_dir: Path) -> Path:
    if not source.samples:
        raise RuntimeError(f"source voice {source_voice_id} has no samples")

    sample = source.samples[0]
    safe_name = source.name.replace(" ", "_")
    existing = sorted(out_dir.glob(f"{safe_name}_{source_voice_id}_{sample.sample_id}_*.mp3"))
    if existing:
        return existing[-1]

    path = out_dir / f"{safe_name}_{source_voice_id}_{sample.sample_id}_{stamp()}.mp3"
    with path.open("wb") as handle:
        for chunk in client.voices.samples.audio.get(source_voice_id, sample.sample_id):
            handle.write(chunk)
    return path


def copy_settings(client: ElevenLabs, source_voice_id: str, clone_voice_id: str) -> None:
    settings = client.voices.settings.get(source_voice_id)
    client.voices.settings.update(
        clone_voice_id,
        request=VoiceSettings(
            stability=getattr(settings, "stability", None),
            similarity_boost=getattr(settings, "similarity_boost", None),
            style=getattr(settings, "style", None),
            use_speaker_boost=getattr(settings, "use_speaker_boost", None),
            speed=getattr(settings, "speed", None),
        ),
    )


def try_ivc(client: ElevenLabs, source, source_voice_id: str, sample_path: Path, clone_name: str):
    with sample_path.open("rb") as audio:
        return client.voices.ivc.create(
            name=clone_name,
            files=[("source-sample.mp3", audio, "audio/mpeg")],
            remove_background_noise=False,
            description=f"Clone of {source.name} ({source_voice_id}) created from its source sample.",
            labels=json.dumps({
                "source_voice_id": source_voice_id,
                "source_voice_name": source.name,
                "clone_method": "instant_voice_clone",
            }),
        )


def try_remix(client: ElevenLabs, source, source_voice_id: str, out_dir: Path, clone_name: str):
    preview_text = (
        "Prometheus is online, and Yennefer is speaking with the same composed, precise, lower register voice. "
        "This is a short verification sample for the duplicated ElevenLabs voice."
    )
    response = client.text_to_voice.remix(
        source_voice_id,
        voice_description=source.description or "A confident British female voice with precise articulation.",
        text=preview_text,
        auto_generate_text=False,
        guidance_scale=8.0,
        prompt_strength=0.8,
    )
    if not response.previews:
        raise RuntimeError("remix returned no previews")

    preview = response.previews[0]
    ext = "mp3" if "mpeg" in preview.media_type else "wav"
    preview_path = out_dir / f"remix_preview_{preview.generated_voice_id}_{stamp()}.{ext}"
    preview_path.write_bytes(base64.b64decode(preview.audio_base_64))

    created = client.text_to_voice.create(
        voice_name=clone_name,
        voice_description=source.description or "Generated Yennefer voice clone.",
        generated_voice_id=preview.generated_voice_id,
        labels={
            **{str(k): str(v) for k, v in (source.labels or {}).items() if v is not None},
            "source_voice_id": source_voice_id,
            "source_voice_name": source.name,
            "clone_method": "text_to_voice_remix",
        },
    )
    return created, preview_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-voice-id", default=None)
    parser.add_argument("--method", choices=("auto", "ivc", "remix"), default="auto")
    parser.add_argument("--name", default=None)
    args = parser.parse_args()

    config = load_config()["voice_output"]
    source_voice_id = args.source_voice_id or config["voice_id"]
    client = ElevenLabs(api_key=config["api_key"])
    out_dir = OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    source = client.voices.get(source_voice_id, with_settings=True)
    clone_name = args.name or f"{source.name} Clone {stamp()}"
    sample_path = save_source_sample(client, source, source_voice_id, out_dir)

    failures: list[str] = []
    created = None
    method = None
    preview_path = None

    if args.method in ("auto", "ivc"):
        try:
            created = try_ivc(client, source, source_voice_id, sample_path, clone_name)
            method = "instant_voice_clone"
        except ApiError as exc:
            failures.append(f"instant_voice_clone: {api_error_message(exc)}")
            if args.method == "ivc":
                raise

    if created is None and args.method in ("auto", "remix"):
        try:
            created, preview_path = try_remix(client, source, source_voice_id, out_dir, clone_name)
            method = "text_to_voice_remix"
        except ApiError as exc:
            failures.append(f"text_to_voice_remix: {api_error_message(exc)}")
            if args.method == "remix":
                raise

    if created is None:
        record = {
            "created_at_utc": stamp(),
            "source_voice_id": source_voice_id,
            "source_voice_name": source.name,
            "source_sample_path": str(sample_path),
            "clone_voice_id": None,
            "failures": failures,
        }
        record_path = out_dir / f"clone_failed_{source_voice_id}_{stamp()}.json"
        record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        print("clone_created: false")
        print("source_sample_path:", sample_path)
        print("record_path:", record_path)
        for failure in failures:
            print("failure:", failure)
        return 2

    clone_voice_id = created.voice_id
    copy_settings(client, source_voice_id, clone_voice_id)
    verified = client.voices.get(clone_voice_id, with_settings=True)

    record = {
        "created_at_utc": stamp(),
        "method": method,
        "source_voice_id": source_voice_id,
        "source_voice_name": source.name,
        "source_category": source.category,
        "source_sample_path": str(sample_path),
        "preview_path": str(preview_path) if preview_path else None,
        "clone_voice_id": clone_voice_id,
        "clone_voice_name": verified.name,
        "clone_category": verified.category,
        "settings_copied": True,
    }
    record_path = out_dir / f"clone_{clone_voice_id}_{stamp()}.json"
    record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print("clone_created: true")
    print("method:", method)
    print("clone_voice_id:", clone_voice_id)
    print("clone_voice_name:", verified.name)
    print("source_sample_path:", sample_path)
    print("record_path:", record_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
