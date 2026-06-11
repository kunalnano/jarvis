"""
Voice - Text-to-Speech Output

Supports:
- ElevenLabs (premium, cross-platform)
- Chatterbox (self-hosted Chatterbox-TTS-Server, OpenAI-compatible API)
- macOS native TTS (free, Mac only)
"""

import asyncio
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from rich.console import Console
from rich.panel import Panel

console = Console()


def clean_for_speech(text: str) -> str:
    """Remove thinking tags and markdown formatting that TTS would read literally."""
    # Strip complete thinking blocks
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL)
    # Handle missing opening tag - strip everything before closing tag
    if '</think>' in text:
        text = text.split('</think>', 1)[-1]
    if '</thinking>' in text:
        text = text.split('</thinking>', 1)[-1]
    # Handle unclosed tags
    if '<think>' in text:
        text = text.split('<think>', 1)[0]
    if '<thinking>' in text:
        text = text.split('<thinking>', 1)[0]
    # Clean up orphan tags
    text = re.sub(r'</?think(?:ing)?>', '', text)
    # Strip markdown
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # Bold
    text = re.sub(r'\*([^*]+)\*', r'\1', text)  # Italic
    text = re.sub(r'_+', '', text)
    text = re.sub(r'`+', '', text)
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)  # Headers
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)  # Bullets
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)  # Numbered lists
    text = re.sub(r'\n{2,}', '. ', text)  # Multiple newlines to pause
    text = re.sub(r'\n', ' ', text)  # Single newlines to space
    text = re.sub(r'  +', ' ', text)
    return text.strip()


class Voice:
    """Multi-backend TTS - ElevenLabs or macOS native."""

    def __init__(self, config: dict):
        self.config = config.get('voice_output', {})
        self.engine = self.config.get('engine', 'elevenlabs')

        # ElevenLabs settings
        self.voice_id = self.config.get('voice_id', '')
        self.model = self.config.get('model', 'eleven_turbo_v2_5')
        self.api_key = self.config.get('api_key') or os.environ.get('ELEVENLABS_API_KEY')

        # Voice tuning parameters
        self.stability = self.config.get('stability', 0.5)
        self.similarity_boost = self.config.get('similarity_boost', 0.75)
        self.style = self.config.get('style', 0.0)
        self.speed = self.config.get('speed', 1.0)  # Stored but may not be supported
        self.use_speaker_boost = self.config.get('use_speaker_boost', True)

        # Chatterbox settings (self-hosted Chatterbox-TTS-Server)
        chatterbox = self.config.get('chatterbox', {})
        self.chatterbox_api_base = chatterbox.get('api_base', 'http://localhost:8004').rstrip('/')
        self.chatterbox_voice = chatterbox.get('voice', 'default')
        self.chatterbox_speed = chatterbox.get('speed', self.speed)
        # Extra fields merged into the request body (e.g. exaggeration) so the
        # server's dials are tunable from config without code changes.
        self.chatterbox_params = chatterbox.get('params', {})

        # macOS settings
        self.macos_voice = self.config.get('macos_voice', 'Samantha')
        self.rate = self.config.get('rate', 180)

        self.initialized = False
        self.client = None

        # Usage tracking
        self.characters_used_session = 0
        self.subscription_info = None

    async def initialize(self):
        """Initialize TTS engine."""
        # Explicit chatterbox wins over elevenlabs auto-detect
        if self.config.get('engine') == 'chatterbox':
            await self._init_chatterbox()
            return

        # Auto-detect engine if voice_id is set
        if self.voice_id and self.api_key:
            self.engine = 'elevenlabs'
        elif sys.platform == 'darwin' and self.config.get('engine') == 'macos':
            self.engine = 'macos'

        if self.engine == 'elevenlabs':
            await self._init_elevenlabs()
        elif self.engine == 'macos':
            await self._init_macos()
        else:
            if self.api_key and self.voice_id:
                await self._init_elevenlabs()
            elif sys.platform == 'darwin':
                self.engine = 'macos'
                await self._init_macos()
            else:
                console.print("[red]✗[/red] No TTS engine configured")
                console.print("[dim]Add ElevenLabs API key or use macOS native[/dim]")

    async def _init_elevenlabs(self):
        """Initialize ElevenLabs."""
        if not self.api_key:
            console.print("[red]✗[/red] ELEVENLABS_API_KEY not set")
            return

        try:
            from elevenlabs.client import ElevenLabs

            self.client = ElevenLabs(api_key=self.api_key)

            self.initialized = True
            self.engine = 'elevenlabs'

            # Fetch and display subscription info
            await self._fetch_subscription_info()

            console.print(f"[green]✓[/green] ElevenLabs ready (voice: {self.voice_id[:8]}..., speed: {self.speed}x)")

        except ImportError as e:
            console.print(f"[red]✗[/red] Missing package: {e}")
            console.print("[dim]Run: pip install elevenlabs[/dim]")
        except Exception as e:
            console.print(f"[red]✗[/red] ElevenLabs error: {e}")

    async def _fetch_subscription_info(self):
        """Fetch ElevenLabs subscription/usage info."""
        try:
            loop = asyncio.get_event_loop()
            # Try different API methods based on SDK version
            try:
                # Newer SDK versions
                self.subscription_info = await loop.run_in_executor(
                    None,
                    lambda: self.client.user.get()
                )
            except AttributeError:
                try:
                    # Alternative method
                    self.subscription_info = await loop.run_in_executor(
                        None,
                        lambda: self.client.users.get()
                    )
                except:
                    pass

            if self.subscription_info:
                self._print_credits_status()
        except Exception as e:
            console.print(f"[yellow]Could not fetch subscription info: {e}[/yellow]")

    def _print_credits_status(self):
        """Display ElevenLabs credits/usage."""
        if not self.subscription_info:
            return

        info = self.subscription_info

        # Handle different SDK response structures
        if hasattr(info, 'subscription'):
            sub = info.subscription
            used = getattr(sub, 'character_count', 0)
            limit = getattr(sub, 'character_limit', 10000)
            tier = getattr(sub, 'tier', 'unknown')
        else:
            used = getattr(info, 'character_count', 0)
            limit = getattr(info, 'character_limit', 10000)
            tier = getattr(info, 'tier', 'unknown')

        remaining = limit - used
        percent_used = (used / limit) * 100 if limit > 0 else 0

        # Visual bar
        bar_width = 25
        filled = int((percent_used / 100) * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)

        # Color based on usage
        if percent_used < 50:
            color = "green"
        elif percent_used < 80:
            color = "yellow"
        else:
            color = "red"

        console.print(
            f"[dim]ElevenLabs:[/dim] [{color}]{bar}[/{color}] "
            f"[dim]{used:,}/{limit:,} chars ({percent_used:.1f}%) • {remaining:,} remaining • {tier}[/dim]"
        )

    async def _init_chatterbox(self):
        """Initialize Chatterbox: confirm the server is reachable, else degrade."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                # Any response (even 404 on /health) means the server is up
                resp = await client.get(f"{self.chatterbox_api_base}/health")
                if resp.status_code >= 500:
                    raise RuntimeError(f"server unhealthy (HTTP {resp.status_code})")
        except Exception as e:
            console.print(
                f"[yellow]Chatterbox unreachable at {self.chatterbox_api_base}: {e}[/yellow]"
            )
            await self._fallback_from_chatterbox()
            return

        self.initialized = True
        self.engine = 'chatterbox'
        console.print(
            f"[green]✓[/green] Chatterbox ready "
            f"({self.chatterbox_api_base}, voice: {self.chatterbox_voice}, speed: {self.chatterbox_speed}x)"
        )

    async def _fallback_from_chatterbox(self):
        """Degrade chatterbox → elevenlabs → macOS say, mirroring the daemon's retry chain."""
        if self.api_key and self.voice_id:
            console.print("[dim]Falling back to ElevenLabs[/dim]")
            await self._init_elevenlabs()
        elif sys.platform == 'darwin':
            console.print("[dim]Falling back to macOS TTS[/dim]")
            await self._init_macos()
        else:
            console.print("[red]✗[/red] No fallback TTS engine available")

    async def _init_macos(self):
        """Initialize macOS native TTS."""
        if sys.platform != 'darwin':
            console.print("[red]✗[/red] macOS TTS only available on Mac")
            return

        self.initialized = True
        self.engine = 'macos'
        console.print(f"[green]✓[/green] macOS TTS ready (voice: {self.macos_voice})")

    async def speak(self, text: str):
        """Generate and play speech."""
        if not text:
            return

        console.print(f"[magenta]Yennefer:[/magenta] {text}")

        if not self.initialized:
            console.print("[yellow]Voice not initialized[/yellow]")
            return

        speech_text = clean_for_speech(text)

        if self.engine == 'elevenlabs':
            await self._speak_elevenlabs(speech_text)
        elif self.engine == 'chatterbox':
            await self._speak_chatterbox(speech_text)
        elif self.engine == 'macos':
            await self._speak_macos(speech_text)

    async def _speak_elevenlabs(self, text: str):
        """Speak using ElevenLabs with voice settings."""
        try:
            from elevenlabs import VoiceSettings

            # Track character usage
            self.characters_used_session += len(text)

            # Build voice settings
            voice_settings = VoiceSettings(
                stability=self.stability,
                similarity_boost=self.similarity_boost,
                style=self.style,
                use_speaker_boost=self.use_speaker_boost
            )

            loop = asyncio.get_event_loop()

            # Core parameters that are always supported
            kwargs = {
                'text': text,
                'voice_id': self.voice_id,
                'model_id': self.model,
                'voice_settings': voice_settings
            }

            audio_generator = await loop.run_in_executor(
                None,
                lambda: self.client.text_to_speech.convert(**kwargs)
            )

            audio_bytes = b''.join(audio_generator)

            with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
                f.write(audio_bytes)
                temp_path = f.name

            # Play with afplay (macOS native)
            await loop.run_in_executor(
                None,
                lambda: subprocess.run(["afplay", temp_path], check=True)
            )

            Path(temp_path).unlink(missing_ok=True)

        except Exception as e:
            console.print(f"[yellow]TTS error: {e}[/yellow]")

    async def _speak_chatterbox(self, text: str):
        """Speak via Chatterbox-TTS-Server's OpenAI-compatible endpoint."""
        import httpx

        payload = {
            'model': 'chatterbox',
            'input': text,
            'voice': self.chatterbox_voice,
            'response_format': 'wav',
            'speed': self.chatterbox_speed,
            **self.chatterbox_params,
        }

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{self.chatterbox_api_base}/v1/audio/speech", json=payload
                )
                resp.raise_for_status()
                audio_bytes = resp.content

            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
                f.write(audio_bytes)
                temp_path = f.name

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: subprocess.run(["afplay", temp_path], check=True)
            )

            Path(temp_path).unlink(missing_ok=True)

        except Exception as e:
            console.print(f"[yellow]Chatterbox TTS error: {e}[/yellow]")
            # Don't go mute mid-sentence: degrade to local say for this utterance
            if sys.platform == 'darwin':
                await self._speak_macos(text)

    async def _speak_macos(self, text: str):
        """Speak using macOS native TTS."""
        try:
            escaped_text = text.replace('"', '\\"')
            cmd = f'say -v {self.macos_voice} -r {self.rate} "{escaped_text}"'

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: subprocess.run(cmd, shell=True, check=True)
            )

        except Exception as e:
            console.print(f"[yellow]TTS error: {e}[/yellow]")

    def status(self):
        """Print detailed voice status."""
        if self.engine == 'elevenlabs':
            info = self.subscription_info

            # Extract info based on SDK response structure
            if info:
                if hasattr(info, 'subscription'):
                    sub = info.subscription
                    used = getattr(sub, 'character_count', 0)
                    limit = getattr(sub, 'character_limit', 10000)
                    tier = getattr(sub, 'tier', 'unknown')
                else:
                    used = getattr(info, 'character_count', 0)
                    limit = getattr(info, 'character_limit', 10000)
                    tier = getattr(info, 'tier', 'unknown')

                console.print(Panel(
                    f"[cyan]Tier:[/cyan] {tier}\n"
                    f"[cyan]Characters used:[/cyan] {used:,} / {limit:,}\n"
                    f"[cyan]Remaining:[/cyan] {limit - used:,}\n"
                    f"[cyan]This session:[/cyan] {self.characters_used_session:,} chars\n"
                    f"[cyan]Voice settings:[/cyan]\n"
                    f"  Stability: {self.stability}\n"
                    f"  Similarity: {self.similarity_boost}\n"
                    f"  Style: {self.style}",
                    title="ElevenLabs Status"
                ))
            else:
                console.print(Panel(
                    f"[cyan]This session:[/cyan] {self.characters_used_session:,} chars\n"
                    f"[cyan]Voice settings:[/cyan]\n"
                    f"  Stability: {self.stability}\n"
                    f"  Similarity: {self.similarity_boost}\n"
                    f"  Style: {self.style}",
                    title="ElevenLabs Status"
                ))
        else:
            console.print(f"[dim]Engine: {self.engine}[/dim]")

    async def refresh_credits(self):
        """Refresh ElevenLabs credit info."""
        if self.engine == 'elevenlabs':
            await self._fetch_subscription_info()

    def stop(self):
        """Stop current speech."""
        pass

    def cleanup(self):
        """Clean up resources."""
        pass
