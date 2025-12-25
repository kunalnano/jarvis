"""
Orchestrator - Main Conversation Loop

Coordinates ears, brain, voice, and tools into a cohesive assistant.
"""

import asyncio
from typing import Optional

from pynput import keyboard
from rich.console import Console
import threading

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
        self.is_listening = False
        self._keyboard_listener = None
        self._hotkey = self.config.get('push_to_talk', {}).get('hotkey', '<alt>+<space>')
        self._loop = None  # Store reference to main event loop
        
    async def initialize(self):
        """Initialize all components."""
        await self.ears.initialize()
        await self.voice.initialize()
        
        success = await self.brain.initialize()
        if success:
            # Register tools with brain
            self.brain.register_tools(self.tools.get_tool_definitions())
        
        return success
    
    async def run(self):
        """Main run loop."""
        self.is_running = True
        self._loop = asyncio.get_running_loop()  # Store event loop reference
        
        # Initialize components
        await self.initialize()
        
        # Setup keyboard listener for push-to-talk
        self._setup_hotkey()
        
        # Greeting
        await self.voice.speak("Jarvis online and ready, sir.")
        
        # Main loop
        try:
            while self.is_running:
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        finally:
            self._cleanup()
    
    def _setup_hotkey(self):
        """Setup push-to-talk hotkey listener."""
        # Track modifier state
        self._alt_pressed = False
        self._recording_task: Optional[asyncio.Task] = None
        
        def on_press(key):
            try:
                # Check for Alt (Option on Mac)
                if key == keyboard.Key.alt or key == keyboard.Key.alt_l or key == keyboard.Key.alt_r:
                    self._alt_pressed = True
                # Check for Space while Alt is held
                elif key == keyboard.Key.space and self._alt_pressed:
                    if not self.is_listening:
                        self._start_listening()
            except Exception as e:
                console.print(f"[red]Hotkey error: {e}[/red]")
        
        def on_release(key):
            try:
                if key == keyboard.Key.alt or key == keyboard.Key.alt_l or key == keyboard.Key.alt_r:
                    self._alt_pressed = False
                    if self.is_listening:
                        self._stop_listening()
                elif key == keyboard.Key.space:
                    if self.is_listening:
                        self._stop_listening()
            except Exception as e:
                console.print(f"[red]Hotkey error: {e}[/red]")
        
        self._keyboard_listener = keyboard.Listener(
            on_press=on_press,
            on_release=on_release
        )
        self._keyboard_listener.start()
        console.print(f"[dim]Hotkey active: Option+Space to talk[/dim]")
    
    def _start_listening(self):
        """Start recording audio."""
        if self.is_listening:
            return
            
        self.is_listening = True
        
        # Stop any current speech
        self.voice.stop()
        
        # Start recording
        self.ears.start_recording()
    
    def _stop_listening(self):
        """Stop recording and process audio."""
        if not self.is_listening:
            return
            
        self.is_listening = False
        
        # Get audio and transcribe
        audio_data = self.ears.stop_recording()
        
        # Schedule coroutine from keyboard thread to main event loop
        if self._loop and audio_data is not None and len(audio_data) > 0:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._process_audio(audio_data))
            )
    
    async def _process_audio(self, audio_data):
        """Process recorded audio through the full pipeline."""
        # Transcribe
        text = await self.ears.transcribe(audio_data)
        
        if not text or len(text.strip()) < 2:
            return
        
        # Process with Claude
        await self._process_input(text)
    
    async def _process_input(self, user_input: str):
        """Process user input through brain and tools."""
        # Get response from Claude
        response = await self.brain.think(user_input)
        
        # Handle tool calls
        if response.get('tool_calls'):
            for tool_call in response['tool_calls']:
                result = await self.tools.execute(
                    tool_call['name'],
                    tool_call['input']
                )
                console.print(f"[dim]Tool result: {result[:100]}...[/dim]")
        
        # Speak response
        if response.get('text'):
            await self.voice.speak(response['text'])
    
    async def process_text(self, text: str):
        """Process text input directly (for testing without voice)."""
        await self._process_input(text)
    
    async def shutdown(self):
        """Gracefully shutdown Jarvis."""
        self.is_running = False
        await self.voice.speak("Shutting down. Goodbye, sir.")
        self._cleanup()
    
    def _cleanup(self):
        """Clean up resources."""
        if self._keyboard_listener:
            self._keyboard_listener.stop()
        
        self.ears.cleanup()
        self.voice.cleanup()
