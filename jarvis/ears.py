"""
Ears - Voice Input Module

Handles audio capture and speech-to-text transcription using Whisper.
"""

import asyncio
import queue
import threading
from typing import Callable, Optional

import numpy as np
import sounddevice as sd
from rich.console import Console

console = Console()

# Audio settings
SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = np.float32


class Ears:
    """Voice input handler using Whisper for transcription."""
    
    def __init__(self, config: dict):
        self.config = config.get('voice_input', {})
        self.model_name = self.config.get('model', 'base.en')
        self.language = self.config.get('language', 'en')
        
        self.model = None
        self.audio_queue = queue.Queue()
        self.is_recording = False
        self._stream = None
        
    async def initialize(self):
        """Load Whisper model."""
        console.print("[dim]Loading Whisper model...[/dim]")
        
        try:
            import whisper
            self.model = whisper.load_model(self.model_name)
            console.print(f"[green]✓[/green] Whisper model '{self.model_name}' loaded")
        except Exception as e:
            console.print(f"[yellow]⚠[/yellow] Whisper not available: {e}")
            console.print("[dim]Falling back to keyboard input[/dim]")
            self.model = None
    
    def start_recording(self):
        """Start capturing audio."""
        if self.is_recording:
            return
            
        self.is_recording = True
        self.audio_queue = queue.Queue()
        
        def audio_callback(indata, frames, time, status):
            if status:
                console.print(f"[yellow]Audio status: {status}[/yellow]")
            self.audio_queue.put(indata.copy())
        
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            callback=audio_callback
        )
        self._stream.start()
        console.print("[blue]🎤 Listening...[/blue]")
    
    def stop_recording(self) -> np.ndarray:
        """Stop recording and return audio data."""
        if not self.is_recording:
            return np.array([])
            
        self.is_recording = False
        
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        
        # Collect all audio chunks
        chunks = []
        while not self.audio_queue.empty():
            chunks.append(self.audio_queue.get())
        
        if not chunks:
            return np.array([])
            
        audio_data = np.concatenate(chunks, axis=0).flatten()
        console.print(f"[dim]Captured {len(audio_data) / SAMPLE_RATE:.1f}s of audio[/dim]")
        
        return audio_data
    
    async def transcribe(self, audio_data: np.ndarray) -> str:
        """Transcribe audio to text using Whisper."""
        if self.model is None:
            # Fallback: prompt for text input
            return await self._fallback_input()
        
        if len(audio_data) == 0:
            return ""
        
        console.print("[dim]Transcribing...[/dim]")
        
        # Run Whisper in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self.model.transcribe(
                audio_data,
                language=self.language,
                fp16=False
            )
        )
        
        text = result.get('text', '').strip()
        console.print(f"[cyan]You:[/cyan] {text}")
        
        return text
    
    async def _fallback_input(self) -> str:
        """Fallback to keyboard input if Whisper unavailable."""
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(
            None,
            lambda: input("[cyan]You:[/cyan] ")
        )
        return text.strip()
    
    async def listen(self) -> str:
        """
        Complete listen cycle: record → transcribe → return text.
        Used with push-to-talk.
        """
        self.start_recording()
        
        # Wait for key release (handled by orchestrator)
        # For now, record for a fixed duration
        await asyncio.sleep(0.1)  # Small buffer
        
        return ""  # Orchestrator will call stop_recording and transcribe
    
    async def listen_for_wake_word(self, callback: Callable[[str], None]):
        """
        Continuously listen for wake word.
        Calls callback with transcribed text when wake word detected.
        """
        # TODO: Implement continuous listening with Porcupine or similar
        pass
    
    def cleanup(self):
        """Release audio resources."""
        if self._stream:
            self._stream.stop()
            self._stream.close()
