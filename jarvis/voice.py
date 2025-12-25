"""
Voice - Text-to-Speech Output Module

Handles speech synthesis using ElevenLabs or macOS TTS.
"""

import asyncio
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from rich.console import Console

console = Console()


class Voice:
    """Text-to-speech handler."""
    
    def __init__(self, config: dict):
        self.config = config.get('voice_output', {})
        self.engine = self.config.get('engine', 'macos')
        self.is_speaking = False
        self._process: Optional[subprocess.Popen] = None
        
        # ElevenLabs settings
        self.elevenlabs_voice_id = self.config.get(
            'elevenlabs_voice_id', 
            '21m00Tcm4TlvDq8ikWAM'  # Rachel
        )
        
        # macOS settings
        self.macos_voice = self.config.get('macos_voice', 'Daniel')
        self.rate = self.config.get('rate', 180)
        
        self._client = None
        
    async def initialize(self):
        """Initialize TTS engine."""
        if self.engine == 'elevenlabs':
            await self._init_elevenlabs()
        else:
            console.print(f"[green]✓[/green] Using macOS voice: {self.macos_voice}")
    
    async def _init_elevenlabs(self):
        """Initialize ElevenLabs client."""
        try:
            from elevenlabs import ElevenLabs
            import os
            
            api_key = os.environ.get('ELEVENLABS_API_KEY')
            if not api_key:
                console.print("[yellow]⚠[/yellow] ELEVENLABS_API_KEY not set, falling back to macOS")
                self.engine = 'macos'
                return
                
            self._client = ElevenLabs(api_key=api_key)
            console.print("[green]✓[/green] ElevenLabs initialized")
            
        except ImportError:
            console.print("[yellow]⚠[/yellow] ElevenLabs not installed, using macOS")
            self.engine = 'macos'
    
    async def speak(self, text: str):
        """Speak the given text."""
        if not text:
            return
            
        self.is_speaking = True
        console.print(f"[green]Jarvis:[/green] {text}")
        
        try:
            if self.engine == 'elevenlabs':
                await self._speak_elevenlabs(text)
            else:
                await self._speak_macos(text)
        finally:
            self.is_speaking = False
    
    async def _speak_elevenlabs(self, text: str):
        """Speak using ElevenLabs API."""
        if not self._client:
            await self._speak_macos(text)
            return
            
        try:
            # Generate audio
            audio = self._client.generate(
                text=text,
                voice=self.elevenlabs_voice_id,
                model="eleven_monolingual_v1"
            )
            
            # Save to temp file and play
            with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
                for chunk in audio:
                    f.write(chunk)
                temp_path = f.name
            
            # Play with afplay (macOS)
            self._process = await asyncio.create_subprocess_exec(
                'afplay', temp_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await self._process.wait()
            
            # Cleanup
            Path(temp_path).unlink(missing_ok=True)
            
        except Exception as e:
            console.print(f"[yellow]ElevenLabs error: {e}, falling back to macOS[/yellow]")
            await self._speak_macos(text)
    
    async def _speak_macos(self, text: str):
        """Speak using macOS say command."""
        # Escape quotes in text
        escaped_text = text.replace('"', '\\"')
        
        self._process = await asyncio.create_subprocess_exec(
            'say',
            '-v', self.macos_voice,
            '-r', str(self.rate),
            escaped_text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await self._process.wait()
    
    def stop(self):
        """Stop current speech."""
        if self._process and self._process.returncode is None:
            self._process.terminate()
            self.is_speaking = False
            console.print("[dim]Speech interrupted[/dim]")
    
    def cleanup(self):
        """Clean up resources."""
        self.stop()
