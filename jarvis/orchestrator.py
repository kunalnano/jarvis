"""
Orchestrator - Main Conversation Loop

Simplified: Text input → Claude → Voice output
Use Wispr Flow for dictation into the text prompt.
"""

import asyncio
import signal
import sys
from typing import Optional

from rich.console import Console

from .ears import Ears
from .voice import Voice
from .brain import Brain
from .tools import Tools

console = Console()


class JarvisOrchestrator:
    """Main orchestrator for Jarvis."""
    
    def __init__(self, config: dict):
        self.config = config
        
        # Initialize components
        self.ears = Ears(config)
        self.voice = Voice(config)
        self.brain = Brain(config)
        self.tools = Tools(config)
        
        # State
        self.is_running = False
        
    async def initialize(self):
        """Initialize all components."""
        await self.ears.initialize()
        await self.voice.initialize()
        
        success = await self.brain.initialize()
        if success:
            self.brain.register_tools(self.tools.get_tool_definitions())
        
        return success
    
    async def run(self):
        """Main run loop - simple text input."""
        self.is_running = True
        
        # Initialize components
        await self.initialize()
        
        # Greeting
        await self.voice.speak("Jarvis online and ready, sir.")
        
        console.print("\n[dim]Type your commands or use Wispr Flow to dictate.[/dim]")
        console.print("[dim]Type 'quit' or 'exit' to stop.[/dim]\n")
        
        # Main loop
        try:
            while self.is_running:
                # Get input (user can type or dictate via Wispr Flow)
                user_input = await self.ears.listen()
                
                # Check for exit commands
                if user_input.lower() in ('quit', 'exit', 'bye', 'goodbye'):
                    await self.shutdown()
                    break
                
                # Skip empty input
                if not user_input:
                    continue
                
                # Process the input
                await self._process_input(user_input)
                print()  # Blank line between exchanges
                
        except (KeyboardInterrupt, EOFError):
            await self.shutdown()
        except asyncio.CancelledError:
            pass
    
    async def _process_input(self, user_input: str):
        """Process user input through brain and tools."""
        # Get response from Claude
        response = await self.brain.think(user_input)
        
        # Handle tool calls
        if response.get('tool_calls'):
            for tool_call in response['tool_calls']:
                console.print(f"[dim]→ {tool_call['name']}[/dim]")
                result = await self.tools.execute(
                    tool_call['name'],
                    tool_call['input']
                )
                console.print(f"[dim]  {result[:100]}{'...' if len(result) > 100 else ''}[/dim]")
        
        # Speak response
        if response.get('text'):
            await self.voice.speak(response['text'])
    
    async def process_text(self, text: str):
        """Process text input directly (for programmatic use)."""
        await self._process_input(text)
    
    async def shutdown(self):
        """Gracefully shutdown Jarvis."""
        self.is_running = False
        await self.voice.speak("Shutting down. Goodbye, sir.")
        self._cleanup()
    
    def _cleanup(self):
        """Clean up resources."""
        self.ears.cleanup()
        self.voice.cleanup()
