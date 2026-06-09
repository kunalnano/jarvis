"""
Yennefer daemon - headless ambient presence (no interactive prompt).

Runs ONLY the proactive Presence layer, for always-on / LaunchAgent use:
she boots on login and speaks on her own (random musings + time/command
triggers) without needing a terminal. Conversational use stays in jarvis.main.
"""

import asyncio
import signal

from rich.console import Console

from .voice import Voice
from .brain import Brain
from .presence import Presence
from .config import load_config

console = Console()


class YenneferDaemon:
    """Headless presence-only runtime for always-on operation."""

    def __init__(self, config: dict):
        self.config = config
        self.voice = Voice(config)
        self.brain = Brain(config)
        self.speech_lock = asyncio.Lock()
        self.presence = Presence(config, self.brain, self.voice, self.speech_lock)
        self._stop = asyncio.Event()

    async def run(self):
        await self.voice.initialize()

        # The brain lives on a remote box (Stormbreaker) that may be asleep at
        # login. Retry with backoff rather than dying, so she comes alive when
        # it's reachable and idles quietly otherwise.
        attempt = 0
        while not self._stop.is_set():
            if await self.brain.initialize():
                break
            attempt += 1
            wait = min(30 * attempt, 300)
            console.print(f"[yellow]Brain unreachable; retrying in {wait}s...[/yellow]")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
        if self._stop.is_set():
            return

        running_loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                running_loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass

        await self.presence.start()
        console.print("[green]Yennefer ambient daemon running (headless).[/green]")

        await self._stop.wait()
        await self.presence.stop()
        self.voice.cleanup()
        console.print("[dim]Yennefer daemon stopped.[/dim]")


def main():
    asyncio.run(YenneferDaemon(load_config()).run())


if __name__ == "__main__":
    main()
