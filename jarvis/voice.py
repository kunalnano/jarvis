"""
Voice - Text-to-Speech Output

Supports:
- ElevenLabs (premium, cross-platform)
- Chatterbox (self-hosted Chatterbox-TTS-Server, OpenAI-compatible API)
- Kokoro-82M via mlx-audio (local Apple Silicon, fixed voicepacks)
- macOS native TTS (free, Mac only)

Degradation chain (Flight 002): chatterbox → kokoro → elevenlabs → macOS say.
Kokoro is the house fallback so that when Stormbreaker sleeps she downshifts
to a local voice instead of the drip or a GPS.
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


def _applescript_string(s: str) -> str:
    """Quote/escape a Python string for safe interpolation into AppleScript."""
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


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

        # Kokoro settings (local Apple Silicon fallback via mlx-audio)
        kokoro = self.config.get('kokoro', {})
        self.kokoro_model = kokoro.get('model', 'prince-canuma/Kokoro-82M')
        self.kokoro_voice = kokoro.get('voice', 'bf_emma')
        self.kokoro_speed = kokoro.get('speed', self.speed)
        # Raw CLI args appended to `python -m mlx_audio.tts.generate` so the
        # generator's dials stay tunable from config without code changes.
        self.kokoro_extra_args = [str(a) for a in kokoro.get('extra_args', [])]

        # macOS settings
        self.macos_voice = self.config.get('macos_voice', 'Samantha')
        self.rate = self.config.get('rate', 180)

        self.initialized = False
        self.client = None

        # Usage tracking
        self.characters_used_session = 0
        self.subscription_info = None

        # DAR-130: last desktop cue (cleaned text, monotonic ts) for dedupe.
        self._last_cue = ("", 0.0)

    async def initialize(self):
        """Initialize TTS engine."""
        # Explicit chatterbox wins over elevenlabs auto-detect
        if self.config.get('engine') == 'chatterbox':
            await self._init_chatterbox()
            return

        # Explicit kokoro engine
        if self.config.get('engine') == 'kokoro':
            await self._init_kokoro()
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
        """Degrade chatterbox → kokoro → elevenlabs → macOS say (Flight 002 chain).

        Kokoro (local, in-register) outranks ElevenLabs so a sleeping
        Stormbreaker doesn't push her back onto the drip; ElevenLabs stays
        as a deep safety net during the decant.
        """
        if self._kokoro_available():
            console.print("[dim]Falling back to Kokoro (local)[/dim]")
            await self._init_kokoro()
            if self.initialized:
                return
        if self.api_key and self.voice_id:
            console.print("[dim]Falling back to ElevenLabs[/dim]")
            await self._init_elevenlabs()
        elif sys.platform == 'darwin':
            console.print("[dim]Falling back to macOS TTS[/dim]")
            await self._init_macos()
        else:
            console.print("[red]✗[/red] No fallback TTS engine available")

    def _kokoro_available(self) -> bool:
        """Kokoro runs via mlx-audio, which is Apple Silicon (MLX) only."""
        if sys.platform != 'darwin':
            return False
        import importlib.util
        return importlib.util.find_spec('mlx_audio') is not None

    async def _init_kokoro(self):
        """Initialize Kokoro via mlx-audio, else degrade past it."""
        if not self._kokoro_available():
            console.print(
                "[yellow]Kokoro unavailable (mlx-audio not installed or not macOS)[/yellow]"
            )
            if self.api_key and self.voice_id:
                console.print("[dim]Falling back to ElevenLabs[/dim]")
                await self._init_elevenlabs()
            elif sys.platform == 'darwin':
                console.print("[dim]Falling back to macOS TTS[/dim]")
                await self._init_macos()
            else:
                console.print("[red]✗[/red] No fallback TTS engine available")
            return

        self.initialized = True
        self.engine = 'kokoro'
        console.print(
            f"[green]✓[/green] Kokoro ready "
            f"(model: {self.kokoro_model}, voice: {self.kokoro_voice}, speed: {self.kokoro_speed}x)"
        )

    async def _init_macos(self):
        """Initialize macOS native TTS."""
        if sys.platform != 'darwin':
            console.print("[red]✗[/red] macOS TTS only available on Mac")
            return

        self.initialized = True
        self.engine = 'macos'
        console.print(f"[green]✓[/green] macOS TTS ready (voice: {self.macos_voice})")

    async def _emit_speech_cue(self, text: str):
        """DAR-130: fire a desktop notification for every spoken utterance.

        This sits at the single speech chokepoint, so all engines
        (elevenlabs/chatterbox/kokoro/macos) are covered. Fail-soft and
        non-blocking: a cue failure must never delay or break speech. Touches
        no governor state in ~/.yennefer/state.json.
        """
        try:
            import time
            msg = clean_for_speech(text)
            if not msg:
                return
            # Dedupe: skip an identical cue within 5s so a playbook hand-up that
            # is also spoken aloud does not double-notify for one utterance.
            now = time.monotonic()
            last_text, last_ts = self._last_cue
            if msg == last_text and (now - last_ts) < 5.0:
                return
            self._last_cue = (msg, now)
            banner = (msg[:137] + '...') if len(msg) > 140 else msg

            def _post():
                try:
                    subprocess.run(
                        ["osascript", "-e",
                         f'display notification {_applescript_string(banner)} '
                         f'with title "Yennefer"'],
                        check=False, capture_output=True, timeout=5,
                    )
                except Exception:
                    pass

            await asyncio.get_event_loop().run_in_executor(None, _post)
        except Exception:
            # Never let a notification failure affect speech.
            pass

    async def speak(self, text: str):
        """Generate and play speech."""
        if not text:
            return

        # DAR-130: every utterance emits a desktop notification cue (all engines).
        await self._emit_speech_cue(text)

        console.print(f"[magenta]Yennefer:[/magenta] {text}")

        if not self.initialized:
            console.print("[yellow]Voice not initialized[/yellow]")
            return

        speech_text = clean_for_speech(text)

        if self.engine == 'elevenlabs':
            await self._speak_elevenlabs(speech_text)
        elif self.engine == 'chatterbox':
            await self._speak_chatterbox(speech_text)
        elif self.engine == 'kokoro':
            await self._speak_kokoro(speech_text)
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
            # Don't go mute mid-sentence: degrade through kokoro, then say
            if self._kokoro_available():
                await self._speak_kokoro(text)
            elif sys.platform == 'darwin':
                await self._speak_macos(text)

    async def _speak_kokoro(self, text: str):
        """Speak via Kokoro-82M through the mlx-audio CLI in this venv.

        Uses `python -m mlx_audio.tts.generate` (the stable documented
        interface) rather than the shifting Python API; output wavs are
        globbed by prefix so segment-naming differences across mlx-audio
        versions don't break playback.
        """
        try:
            loop = asyncio.get_event_loop()
            with tempfile.TemporaryDirectory() as tmpdir:
                prefix = os.path.join(tmpdir, 'yen')
                cmd = [
                    sys.executable, '-m', 'mlx_audio.tts.generate',
                    '--model', self.kokoro_model,
                    '--text', text,
                    '--voice', self.kokoro_voice,
                    '--speed', str(self.kokoro_speed),
                    '--file_prefix', prefix,
                    *self.kokoro_extra_args,
                ]
                await loop.run_in_executor(
                    None,
                    lambda: subprocess.run(
                        cmd, check=True, capture_output=True, timeout=120
                    )
                )

                wavs = sorted(Path(tmpdir).glob('yen*.wav'))
                if not wavs:
                    raise RuntimeError('mlx-audio produced no wav output')
                for wav in wavs:
                    await loop.run_in_executor(
                        None,
                        lambda w=wav: subprocess.run(["afplay", str(w)], check=True)
                    )

        except Exception as e:
            console.print(f"[yellow]Kokoro TTS error: {e}[/yellow]")
            # Last local rung before silence
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
