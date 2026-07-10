#!/usr/bin/env python3
"""Local Chatterbox voice server for Jarvis.

Exposes the small OpenAI-style endpoint that jarvis.voice already expects:
POST /v1/audio/speech -> WAV bytes.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
from pathlib import Path
from typing import Any

import soundfile as sf
import perth
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

if perth.PerthImplicitWatermarker is None:
    perth.PerthImplicitWatermarker = perth.DummyWatermarker

from chatterbox.tts import ChatterboxTTS


DEFAULT_REFERENCE_AUDIO = Path(
    "/Users/alsharma/Projects/jarvis/.local/voice-clones/"
    "Eva_Jarvis_QPYZDsvgGiT4CMQghb53_5cdYOFp2BNBQ8SJjZAUB_20260627T190405Z.mp3"
)


class SpeechRequest(BaseModel):
    model: str | None = "chatterbox"
    input: str
    voice: str | None = None
    response_format: str | None = "wav"
    speed: float | None = 1.0
    exaggeration: float | None = 0.5
    cfg_weight: float | None = 0.5
    temperature: float | None = 0.8
    repetition_penalty: float | None = 1.2
    min_p: float | None = 0.05
    top_p: float | None = 1.0


class ChatterboxService:
    def __init__(self, reference_audio: Path, device: str):
        self.reference_audio = reference_audio
        self.device = device
        self.model: ChatterboxTTS | None = None
        self.lock = asyncio.Lock()

    def resolve_device(self) -> str:
        if self.device != "auto":
            return self.device
        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    def load(self) -> None:
        if not self.reference_audio.exists():
            raise FileNotFoundError(f"reference audio not found: {self.reference_audio}")
        device = self.resolve_device()
        self.model = ChatterboxTTS.from_pretrained(device=device)
        self.device = device
        # Prime the reference conditionals once. Per-request overrides can still
        # pass a different voice path, but the normal Jarvis path is warm.
        self.model.prepare_conditionals(str(self.reference_audio))

    def synthesize(self, request: SpeechRequest) -> bytes:
        if self.model is None:
            raise RuntimeError("model is not loaded")
        voice_path = Path(request.voice).expanduser() if request.voice else self.reference_audio
        if not voice_path.exists():
            voice_path = self.reference_audio

        wav = self.model.generate(
            request.input,
            repetition_penalty=request.repetition_penalty or 1.2,
            min_p=request.min_p or 0.05,
            top_p=request.top_p or 1.0,
            audio_prompt_path=str(voice_path),
            exaggeration=request.exaggeration if request.exaggeration is not None else 0.5,
            cfg_weight=request.cfg_weight if request.cfg_weight is not None else 0.5,
            temperature=request.temperature if request.temperature is not None else 0.8,
        )
        audio = wav.squeeze().detach().cpu().numpy()
        buffer = io.BytesIO()
        sf.write(buffer, audio, self.model.sr, format="WAV")
        return buffer.getvalue()


def create_app(service: ChatterboxService) -> FastAPI:
    app = FastAPI(title="Jarvis Chatterbox Voice")
    state: dict[str, Any] = {"ready": False, "error": None}

    @app.on_event("startup")
    async def startup() -> None:
        try:
            await asyncio.to_thread(service.load)
            state["ready"] = True
        except Exception as exc:  # pragma: no cover - startup diagnostics
            state["error"] = str(exc)
            raise

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ready": state["ready"],
            "error": state["error"],
            "device": service.device,
            "reference_audio": str(service.reference_audio),
        }

    @app.post("/v1/audio/speech")
    async def speech(request: SpeechRequest) -> Response:
        if not state["ready"]:
            raise HTTPException(status_code=503, detail=state["error"] or "model not ready")
        async with service.lock:
            try:
                audio = await asyncio.to_thread(service.synthesize, request)
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
        return Response(content=audio, media_type="audio/wav")

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("JARVIS_CHATTERBOX_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("JARVIS_CHATTERBOX_PORT", "8004")))
    parser.add_argument("--device", default=os.environ.get("JARVIS_CHATTERBOX_DEVICE", "auto"))
    parser.add_argument(
        "--reference-audio",
        default=os.environ.get("JARVIS_CHATTERBOX_REFERENCE", str(DEFAULT_REFERENCE_AUDIO)),
    )
    args = parser.parse_args()

    service = ChatterboxService(Path(args.reference_audio), args.device)
    app = create_app(service)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
