"""
Presence - Yennefer's proactive / ambient layer.

She speaks unprompted, the way a real presence would: spontaneous musings on a
jittered timer, plus reactions to triggers (time-of-day, or a watched command's
output changing). Persona rule: she keeps her thoughts to herself, so proactivity
is deliberately sparse and never talks over a live exchange.
"""

import asyncio
import hashlib
import random
import time
from datetime import datetime

import httpx
from rich.console import Console

from .brain import YENNEFER_SYSTEM_PROMPT, strip_thinking

console = Console()

# Situational seeds for spontaneous remarks. The model expands these in-voice.
# Deliberately generic: she must NOT invent specific facts about the user's day;
# she only offers presence, a dry observation, or a light nudge.
MUSE_SEEDS = [
    "Offer a brief, unprompted check-in. One or two sentences. Do not invent details about what the user is doing.",
    "Make a short, dry observation about time passing or staying focused. In character. No invented specifics.",
    "Gently nudge the user to take stock or take a breath, without being preachy. Keep it to a sentence.",
    "Offer help if it's wanted, in your own understated way. One short line. Don't assume what they need.",
    "Make a wry, in-character aside, the kind of thing you'd say to fill a quiet moment. One sentence.",
]

PROACTIVE_STYLE = (
    "\n\nYou are speaking UNPROMPTED. The user did not just ask you anything. "
    "Say one thing, briefly (one or two short sentences), then stop. "
    "Do not ask a barrage of questions. Do not fabricate specific facts about the "
    "user's current activity, files, or schedule. Speak as if breaking a comfortable silence."
)


class CommandTrigger:
    """Fires when the stdout of a shell command changes between polls."""

    def __init__(self, spec: dict):
        self.name = spec.get("name", "trigger")
        self.command = spec["command"]
        self.poll_seconds = int(spec.get("poll_seconds", 120))
        self.line = spec.get("line")  # if set, said verbatim; else the change is mused on
        self._last_hash = None

    async def poll(self):
        """Return (fired: bool, context: str) for this tick."""
        try:
            proc = await asyncio.create_subprocess_shell(
                self.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
        except Exception:
            return (False, "")
        digest = hashlib.sha256(out).hexdigest()
        first_run = self._last_hash is None
        changed = (not first_run) and digest != self._last_hash
        self._last_hash = digest
        return (changed, out.decode("utf-8", "replace").strip())


class Presence:
    """Yennefer's ambient / proactive behaviour, run as background asyncio tasks."""

    def __init__(self, config: dict, brain, voice, speech_lock: asyncio.Lock):
        pc = (config or {}).get("presence", {}) or {}
        self.enabled = bool(pc.get("enabled", True))
        lo, hi = (pc.get("random_interval_minutes") or [30, 75])[:2]
        self.interval_lo = float(lo) * 60.0
        self.interval_hi = float(hi) * 60.0
        self.min_gap = float(pc.get("min_gap_minutes", 20)) * 60.0
        self.quiet_start, self.quiet_end = (pc.get("quiet_hours") or [22, 8])[:2]
        self.greet_morning = bool(pc.get("greet_morning", True))
        self.morning_window = pc.get("morning_window") or [7, 10]
        self.end_of_day_hour = pc.get("end_of_day_hour", 18)
        self.max_words = int(pc.get("max_words", 40))
        self.trigger_specs = pc.get("triggers") or []

        self.brain = brain
        self.voice = voice
        self.lock = speech_lock

        self.last_spoke = 0.0
        self._greeted_on = None      # date we last gave a morning greeting
        self._signed_off_on = None   # date we last gave an EOD sign-off
        self._tasks = []

    # ---- helpers -------------------------------------------------------

    def notify_spoke(self):
        """Called by the orchestrator after any speech, so the gap is honoured."""
        self.last_spoke = time.time()

    def _in_quiet_hours(self) -> bool:
        h = datetime.now().hour
        s, e = self.quiet_start, self.quiet_end
        if s == e:
            return False
        if s < e:
            return s <= h < e
        return h >= s or h < e   # window wraps midnight

    def _gap_ok(self) -> bool:
        return (time.time() - self.last_spoke) >= self.min_gap

    async def _generate(self, situation: str) -> str:
        """One-off, history-free line in Yennefer's voice (does not touch chat memory)."""
        messages = [
            {"role": "system", "content": YENNEFER_SYSTEM_PROMPT + PROACTIVE_STYLE},
            {"role": "user", "content": situation},
        ]
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.brain.api_base}/chat/completions",
                    json={
                        "model": self.brain.model,
                        "messages": messages,
                        "max_tokens": 120,
                        "temperature": 0.85,
                        "stream": False,
                    },
                    timeout=60.0,
                )
            if resp.status_code != 200:
                return ""
            _msg = resp.json()["choices"][0]["message"]
            text = _msg.get("content") or _msg.get("reasoning_content") or ""
            text = strip_thinking(text)
        except Exception:
            return ""
        words = text.split()
        if len(words) > self.max_words:
            text = " ".join(words[: self.max_words]).rstrip(",;:") + "..."
        return text.strip()

    async def _say(self, text: str):
        """Speak only if she's not already talking."""
        if not text:
            return
        if self.lock.locked():
            return
        async with self.lock:
            console.print("[magenta]Yennefer[/magenta] [dim](unprompted)[/dim]")
            await self.voice.speak(text)
        self.last_spoke = time.time()

    # ---- loops ---------------------------------------------------------

    async def _ambient_loop(self):
        await asyncio.sleep(random.uniform(self.interval_lo, self.interval_hi))
        while True:
            if not self._in_quiet_hours() and self._gap_ok():
                line = await self._generate(random.choice(MUSE_SEEDS))
                await self._say(line)
            await asyncio.sleep(random.uniform(self.interval_lo, self.interval_hi))

    async def _schedule_loop(self):
        while True:
            now = datetime.now()
            today = now.date()
            if (self.greet_morning and self._greeted_on != today
                    and self.morning_window[0] <= now.hour < self.morning_window[1]
                    and not self._in_quiet_hours()):
                self._greeted_on = today
                await self._say(await self._generate(
                    "Greet the user for the morning in your own understated way. "
                    "One line. No invented specifics about their plans."
                ))
            if (self.end_of_day_hour is not None and self._signed_off_on != today
                    and now.hour >= int(self.end_of_day_hour)
                    and not self._in_quiet_hours()):
                self._signed_off_on = today
                await self._say(await self._generate(
                    "It's the end of the working day. Offer a brief, in-character "
                    "sign-off or a note to wind down. One line."
                ))
            await asyncio.sleep(60)

    async def _trigger_loop(self, trigger):
        while True:
            fired, context = await trigger.poll()
            if fired and not self._in_quiet_hours() and self._gap_ok():
                if trigger.line:
                    await self._say(trigger.line)
                else:
                    await self._say(await self._generate(
                        f"Something changed in a process you watch ({trigger.name}). "
                        f"React briefly and in character. Latest output:\n{context[:400]}"
                    ))
            await asyncio.sleep(trigger.poll_seconds)

    # ---- lifecycle -----------------------------------------------------

    async def start(self):
        if not self.enabled:
            console.print("[dim]Presence disabled.[/dim]")
            return
        self.last_spoke = time.time()  # treat the opening line as "just spoke"
        self._tasks.append(asyncio.create_task(self._ambient_loop()))
        self._tasks.append(asyncio.create_task(self._schedule_loop()))
        for spec in self.trigger_specs:
            try:
                self._tasks.append(asyncio.create_task(self._trigger_loop(CommandTrigger(spec))))
            except Exception:
                pass
        n = len(self.trigger_specs)
        console.print(
            "[dim]Presence active - ambient musings every "
            f"{int(self.interval_lo // 60)}-{int(self.interval_hi // 60)} min"
            + (f", {n} trigger(s)" if n else "")
            + ".[/dim]"
        )

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
