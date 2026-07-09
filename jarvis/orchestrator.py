"""
Orchestrator - Main Conversation Loop
"""

import asyncio
from rich.console import Console

from .ears import Ears
from .voice import Voice
from .brain import Brain
from .presence import Presence

console = Console()


class JarvisOrchestrator:
    """Main orchestrator for Jarvis AI Assistant."""
    
    def __init__(self, config: dict):
        self.config = config
        self.ears = Ears(config)
        self.voice = Voice(config)
        self.brain = Brain(config)
        self.speech_lock = asyncio.Lock()
        self.presence = Presence(config, self.brain, self.voice, self.speech_lock)
        self.is_running = False
        
    async def initialize(self):
        """Initialize all components."""
        await self.ears.initialize()
        await self.voice.initialize()
        return await self.brain.initialize()
    
    async def run(self):
        """Main run loop."""
        self.is_running = True
        
        if not await self.initialize():
            console.print("[red]Failed to initialize. Is LM Studio running?[/red]")
            return
        
        await self.voice.speak("I'm here. What do you need?")
        
        console.print("\n[dim]Commands:[/dim]")
        console.print("[dim]  'quit'    - exit[/dim]")
        console.print("[dim]  'clear'   - reset conversation memory[/dim]")
        console.print("[dim]  'status'  - show LLM token usage[/dim]")
        console.print("[dim]  'credits' - show ElevenLabs usage[/dim]")
        console.print("[dim]  'voice'   - show voice settings[/dim]")
        console.print("[dim]Voice input: Press Win+H to dictate[/dim]\n")

        await self.presence.start()

        try:
            while self.is_running:
                user_input = await self.ears.listen()
                
                # Handle commands
                cmd = user_input.lower().strip()
                
                if cmd in ('quit', 'exit', 'bye'):
                    await self.shutdown()
                    break
                
                if cmd == 'clear':
                    self.brain.clear_history()
                    await self.voice.speak("Memory cleared. A fresh start, then.")
                    continue
                
                if cmd == 'status':
                    self.brain.status()
                    continue
                
                if cmd in ('credits', 'usage'):
                    await self.voice.refresh_credits()
                    continue
                
                if cmd == 'voice':
                    self.voice.status()
                    continue
                
                if not user_input:
                    continue
                
                response = await self.brain.think(user_input)
                
                if response.get('text'):
                    async with self.speech_lock:
                        await self.voice.speak(response['text'])
                    self.presence.notify_spoke()
                
                print()
                
        except (KeyboardInterrupt, EOFError):
            await self.shutdown()
    
    async def shutdown(self):
        """Shutdown Jarvis."""
        self.is_running = False
        await self.presence.stop()
        await self.voice.speak("Until next time.")
        self.ears.cleanup()
        self.voice.cleanup()


# Backwards compatibility alias
JarvisOrchestrator = JarvisOrchestrator
