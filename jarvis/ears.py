"""
Ears - Text Input Module (Wispr Flow Compatible)

Instead of handling audio capture and transcription,
we accept text input directly. Use Wispr Flow for dictation.
"""

import asyncio
import sys
from typing import Optional

from rich.console import Console
from rich.prompt import Prompt

console = Console()


class Ears:
    """Text input handler - works with Wispr Flow dictation."""
    
    def __init__(self, config: dict):
        self.config = config.get('voice_input', {})
        self._current_input: Optional[str] = None
        
    async def initialize(self):
        """Initialize input handler."""
        console.print("[green]✓[/green] Text input ready (use Wispr Flow to dictate)")
    
    async def listen(self) -> str:
        """
        Get text input from user.
        User can type or use Wispr Flow to dictate.
        """
        loop = asyncio.get_event_loop()
        
        # Run input() in thread pool to avoid blocking
        text = await loop.run_in_executor(
            None,
            lambda: Prompt.ask("[cyan]You[/cyan]")
        )
        
        return text.strip()
    
    async def listen_oneshot(self, prompt_text: str = "You") -> str:
        """Single input prompt."""
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(
            None,
            lambda: Prompt.ask(f"[cyan]{prompt_text}[/cyan]")
        )
        return text.strip()
    
    def cleanup(self):
        """Nothing to clean up for text input."""
        pass
