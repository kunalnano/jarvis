"""
Voice - Text-to-Speech Output

Supports:
- ElevenLabs (premium, cross-platform)
- Chatterbox (self-hosted Chatterbox-TTS-Server, OpenAI-compatible API)
- Kokoro-82M via mlx-audio (local Apple Silicon, fixed voicepacks)
- macOS native TTS (free, Mac only)

Degradation chain: chatterbox → explicit elevenlabs/11 → kokoro → optional macOS say.
Fallbacks are never silent; degraded engines carry a visible warning.
"""

import asyncio
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from rich.console import Console
from rich.panel import Panel

from .brain import strip_tagless_reasoning

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
    return strip_tagless_reasoning(text).strip()


def clean_for_kokoro(text: str) -> str:
    """Kokoro is sensitive to code-ish strings; make them pronounceable."""
    text = text.replace("_", " ")
    text = clean_for_speech(text)
    text = text.replace("==", " ")
    text = text.replace("-", " ")
    text = re.sub(r'https?://\S+', 'link', text)
    text = re.sub(r'\b[\w.-]+\.(?:local|net|com|org|io)\b', 'hostname', text)
    text = re.sub(r'\b(\d+(?:\.\d+)?)\s*Ti\b', r'\1 terabytes', text)
    text = re.sub(r'\b(\d+(?:\.\d+)?)\s*Gi\b', r'\1 gigabytes', text)
    text = re.sub(r'\b(\d+(?:\.\d+)?)\s*Mi\b', r'\1 megabytes', text)
    text = re.sub(r'[/\\|{}[\]<>]+', ' ', text)
    text = re.sub(r'[:=]+', ': ', text)
    text = re.sub(r'\b[a-zA-Z]+(?:\.[a-zA-Z0-9]+)+\b', 'hostname', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def tts_chunks(text: str, limit: int = 180) -> list[str]:
    """Split text into short chunks for local TTS engines."""
    text = text.strip()
    if not text:
        return []
    pieces = re.split(r'(?<=[.!?])\s+', text)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if len(piece) > limit:
            if current:
                chunks.append(current.strip())
                current = ""
            words = piece.split()
            bucket = ""
            for word in words:
                if len(bucket) + len(word) + 1 > limit:
                    if bucket:
                        chunks.append(bucket.strip())
                    bucket = word
                else:
                    bucket = f"{bucket} {word}".strip()
            if bucket:
                chunks.append(bucket.strip())
            continue
        candidate = f"{current} {piece}".strip()
        if current and len(candidate) > limit:
            chunks.append(current.strip())
            current = piece
        else:
            current = candidate
    if current:
        chunks.append(current.strip())
    return chunks


def kokoro_chunks(text: str) -> list[str]:
    """Kokoro is most reliable with one short sentence per generation."""
    chunks: list[str] = []
    for piece in re.split(r'(?<=[.!?])\s+', text.strip()):
        chunks.extend(tts_chunks(piece, limit=55))
    return [chunk for chunk in chunks if chunk]


def _applescript_string(s: str) -> str:
    """Quote/escape a Python string for safe interpolation into AppleScript."""
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


class Voice:
    """Multi-backend TTS - ElevenLabs or macOS native."""

    def __init__(self, config: dict):
        self.config = config.get('voice_output', {})
        self.engine = self.config.get('engine', 'elevenlabs')
        self.preferred_engine = self.engine

        # ElevenLabs settings
        self.voice_id = self.config.get('voice_id', '')
        self.model = self.config.get('model', 'eleven_turbo_v2_5')
        self.api_key = self.config.get('api_key') or os.environ.get('ELEVENLABS_API_KEY')
        self.allow_elevenlabs_fallback = self.config.get('allow_elevenlabs_fallback', False)

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
        self.chatterbox_startup_wait_seconds = float(chatterbox.get('startup_wait_seconds', 0))
        self.chatterbox_retry_interval_seconds = float(chatterbox.get('retry_interval_seconds', 2))
        self.chatterbox_probe_on_start = bool(chatterbox.get('probe_on_start', False))
        self.chatterbox_probe_text = chatterbox.get(
            'probe_text',
            'Jarvis local voice health check.',
        )
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
        self.degraded = False
        self.degraded_reason = ""
        self.fallback_warning = ""
        self.last_chatterbox_health: dict = {}

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
            if self.allow_elevenlabs_fallback and self.api_key and self.voice_id:
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
        """Initialize Chatterbox: confirm the server is healthy, else degrade."""
        health = await self._wait_for_chatterbox()
        self.last_chatterbox_health = health
        if not health.get('ok'):
            reason = health.get('reason') or "Chatterbox did not become healthy"
            console.print(
                f"[yellow]Chatterbox degraded at {self.chatterbox_api_base}: {reason}[/yellow]"
            )
            await self._fallback_from_chatterbox(reason)
            return

        self.initialized = True
        self.engine = 'chatterbox'
        self._clear_degraded()
        console.print(
            f"[green]✓[/green] Chatterbox ready "
            f"({self.chatterbox_api_base}, voice: {self.chatterbox_voice}, speed: {self.chatterbox_speed}x)"
        )

    def _chatterbox_voice_path(self) -> Path | None:
        """Return the configured voice path when it looks like a local file."""
        voice = str(self.chatterbox_voice or "").strip()
        if not voice or voice == "default":
            return None
        path = Path(voice).expanduser()
        if path.is_absolute() or path.suffix.lower() in {'.wav', '.mp3', '.m4a', '.flac', '.ogg'}:
            return path
        return None

    async def _check_chatterbox_health(
        self,
        *,
        probe_audio: bool = False,
        timeout: float = 5.0,
    ) -> dict:
        """Check Chatterbox service, reference file, and optionally synthesis."""
        import httpx

        checks = {
            'reference_audio': 'skipped',
            'health_endpoint': 'pending',
            'audio_probe': 'skipped',
        }
        result = {
            'ok': False,
            'api_base': self.chatterbox_api_base,
            'voice': self.chatterbox_voice,
            'checks': checks,
            'reason': '',
        }

        voice_path = self._chatterbox_voice_path()
        if voice_path is not None:
            if not voice_path.exists():
                checks['reference_audio'] = 'missing'
                result['reason'] = f"reference audio not found: {voice_path}"
                return result
            checks['reference_audio'] = 'ok'

        try:
            request_timeout = max(timeout, 120.0) if probe_audio else timeout
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(request_timeout, connect=5.0)
            ) as client:
                resp = await client.get(f"{self.chatterbox_api_base}/health")
                if resp.status_code != 200:
                    checks['health_endpoint'] = f"http_{resp.status_code}"
                    result['reason'] = f"health endpoint returned HTTP {resp.status_code}"
                    return result
                try:
                    payload = resp.json()
                except ValueError:
                    payload = {}
                ready = payload.get('ready', True)
                error = payload.get('error')
                if ready is False or error:
                    checks['health_endpoint'] = 'not_ready'
                    result['reason'] = error or "health endpoint reports not ready"
                    return result
                checks['health_endpoint'] = 'ok'

                if probe_audio:
                    probe_payload = {
                        'model': 'chatterbox',
                        'input': self.chatterbox_probe_text,
                        'voice': self.chatterbox_voice,
                        'response_format': 'wav',
                        'speed': self.chatterbox_speed,
                        **self.chatterbox_params,
                    }
                    probe = await client.post(
                        f"{self.chatterbox_api_base}/v1/audio/speech",
                        json=probe_payload,
                    )
                    if probe.status_code != 200:
                        checks['audio_probe'] = f"http_{probe.status_code}"
                        result['reason'] = f"audio probe returned HTTP {probe.status_code}"
                        return result
                    if len(probe.content or b"") < 64:
                        checks['audio_probe'] = 'empty'
                        result['reason'] = "audio probe returned no playable bytes"
                        return result
                    checks['audio_probe'] = 'ok'

        except Exception as e:
            checks['health_endpoint'] = 'error'
            result['reason'] = f"{type(e).__name__}: {e}".rstrip()
            return result

        result['ok'] = True
        result['reason'] = ''
        return result

    async def _wait_for_chatterbox(self) -> dict:
        """Wait briefly for a cold Chatterbox model load before falling back."""
        deadline = time.monotonic() + max(0.0, self.chatterbox_startup_wait_seconds)
        last_health: dict = {}
        while True:
            last_health = await self._check_chatterbox_health(
                probe_audio=self.chatterbox_probe_on_start,
            )
            if last_health.get('ok'):
                return last_health
            if time.monotonic() >= deadline:
                return last_health
            await asyncio.sleep(max(0.1, self.chatterbox_retry_interval_seconds))

    def _mark_degraded(self, engine: str, reason: str):
        self.degraded = True
        self.degraded_reason = reason
        if engine == 'elevenlabs':
            self.fallback_warning = (
                "VOICE WARNING: using ElevenLabs/11 fallback because Chatterbox is degraded "
                f"({reason}). This may spend paid credits."
            )
        elif engine == 'kokoro':
            self.fallback_warning = (
                "VOICE WARNING: using Kokoro fallback, not the Jarvis clone voice, "
                f"because Chatterbox is degraded ({reason})."
            )
        elif engine == 'macos':
            self.fallback_warning = (
                "VOICE WARNING: using macOS fallback voice, not the Jarvis clone voice, "
                f"because Chatterbox is degraded ({reason})."
            )
        else:
            self.fallback_warning = f"VOICE WARNING: using {engine} because Chatterbox is degraded ({reason})."
        console.print(f"[yellow]{self.fallback_warning}[/yellow]")

    def _clear_degraded(self):
        self.degraded = False
        self.degraded_reason = ""
        self.fallback_warning = ""

    async def _fallback_from_chatterbox(self, reason: str = "Chatterbox unavailable"):
        """Degrade chatterbox → explicit ElevenLabs/11 → Kokoro → optional macOS say.

        ElevenLabs/11 is allowed only when config explicitly opts in, and every
        fallback path records a visible warning so the active voice is never
        silently mistaken for the local clone.
        """
        if self.allow_elevenlabs_fallback and self.api_key and self.voice_id:
            console.print("[dim]Falling back to ElevenLabs/11[/dim]")
            await self._init_elevenlabs()
            if self.initialized:
                self._mark_degraded('elevenlabs', reason)
                return
        if self._kokoro_available():
            console.print("[dim]Falling back to Kokoro (local)[/dim]")
            await self._init_kokoro()
            if self.initialized:
                self._mark_degraded('kokoro', reason)
                return
        if sys.platform == 'darwin' and self.config.get('allow_macos_fallback', False):
            console.print("[dim]Falling back to macOS TTS[/dim]")
            await self._init_macos()
            if self.initialized:
                self._mark_degraded('macos', reason)
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
            if self.allow_elevenlabs_fallback and self.api_key and self.voice_id:
                console.print("[dim]Falling back to ElevenLabs[/dim]")
                await self._init_elevenlabs()
            elif sys.platform == 'darwin' and self.config.get('allow_macos_fallback', False):
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

    async def try_promote_chatterbox(self, *, probe_audio: bool = False) -> dict:
        """Promote a degraded voice back to Chatterbox when the local clone recovers."""
        if self.preferred_engine != 'chatterbox':
            return {
                'promoted': False,
                'engine': self.engine,
                'reason': 'preferred engine is not chatterbox',
                'voice': self.runtime_status(),
            }
        health = await self._check_chatterbox_health(probe_audio=probe_audio)
        self.last_chatterbox_health = health
        if not health.get('ok'):
            return {
                'promoted': False,
                'engine': self.engine,
                'reason': health.get('reason') or 'Chatterbox is not healthy',
                'health': health,
                'voice': self.runtime_status(),
            }
        previous = self.engine
        self.engine = 'chatterbox'
        self.initialized = True
        self._clear_degraded()
        if previous != 'chatterbox':
            console.print("[green]✓[/green] Chatterbox recovered; promoted voice back to local clone")
        return {
            'promoted': previous != 'chatterbox',
            'engine': self.engine,
            'previous_engine': previous,
            'health': health,
            'voice': self.runtime_status(),
        }

    async def runtime_status_async(self, *, probe_chatterbox: bool = False) -> dict:
        if self.preferred_engine == 'chatterbox':
            self.last_chatterbox_health = await self._check_chatterbox_health(
                probe_audio=probe_chatterbox,
            )
        return self.runtime_status()

    def runtime_status(self) -> dict:
        return {
            'engine': self.engine,
            'preferred_engine': self.preferred_engine,
            'initialized': self.initialized,
            'degraded': self.degraded,
            'degraded_reason': self.degraded_reason,
            'fallback_warning': self.fallback_warning,
            'chatterbox': {
                'api_base': self.chatterbox_api_base,
                'voice': self.chatterbox_voice,
                'health': self.last_chatterbox_health,
            },
            'kokoro': {
                'voice': self.kokoro_voice,
                'model': self.kokoro_model,
            },
            'elevenlabs': {
                'configured': bool(self.api_key and self.voice_id),
                'fallback_allowed': self.allow_elevenlabs_fallback,
                'voice_id_ok': bool(self.voice_id),
            },
            'macos_fallback_allowed': bool(self.config.get('allow_macos_fallback', False)),
        }

    async def _emit_speech_cue(self, text: str):
        """DAR-130: fire a desktop notification for every spoken utterance.

        This sits at the single speech chokepoint, so all engines
        (elevenlabs/chatterbox/kokoro/macos) are covered. Fail-soft and
        non-blocking: a cue failure must never delay or break speech. Touches
        no governor state in ~/.jarvis/state.json.
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
                         f'with title "Jarvis"'],
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

        console.print(f"[magenta]Jarvis:[/magenta] {text}")

        if self.preferred_engine == 'chatterbox' and (self.engine != 'chatterbox' or not self.initialized):
            await self.try_promote_chatterbox(probe_audio=False)

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
        played_any = False
        try:
            loop = asyncio.get_event_loop()
            chunks = kokoro_chunks(clean_for_kokoro(text))
            if not chunks:
                return
            with tempfile.TemporaryDirectory() as tmpdir:
                for idx, chunk in enumerate(chunks):
                    prefix = f'yen_{idx}'
                    cmd = [
                        sys.executable, '-m', 'mlx_audio.tts.generate',
                        '--model', self.kokoro_model,
                        '--text', chunk,
                        '--voice', self.kokoro_voice,
                        '--speed', str(self.kokoro_speed),
                        '--output_path', tmpdir,
                        '--file_prefix', prefix,
                        *self.kokoro_extra_args,
                    ]
                    completed = await loop.run_in_executor(
                        None,
                        lambda c=cmd: subprocess.run(
                            c, check=True, capture_output=True, timeout=120, text=True
                        )
                    )

                    wavs = sorted(Path(tmpdir).glob(f'{prefix}*.wav'))
                    if not wavs:
                        detail = (completed.stderr or completed.stdout or "").strip()
                        detail = detail[-500:] if detail else "no generator output"
                        raise RuntimeError(f"mlx-audio produced no wav output: {detail}")
                    for wav in wavs:
                        await loop.run_in_executor(
                            None,
                            lambda w=wav: subprocess.run(["afplay", str(w)], check=True)
                        )
                        played_any = True

        except Exception as e:
            console.print(f"[yellow]Kokoro TTS error: {e}[/yellow]")
            # Avoid the jarring two-voice effect: if Kokoro spoke any chunk, do
            # not hand the remainder to macOS `say`. Explicit opt-in keeps the
            # local voice from silently becoming Samantha.
            if (
                not played_any
                and sys.platform == 'darwin'
                and self.config.get('allow_macos_fallback', False)
            ):
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
