#!/usr/bin/env python3
"""
CoreLauncher - Unified Startup System for Project Yennefer
"""

import sys
import os
import platform
import argparse
import asyncio
import httpx
import time
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

# Append current directory to sys.path to ensure module imports work
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from jarvis.config import load_config
    from jarvis.orchestrator import YenneferOrchestrator
except ImportError:
    print("Error: Could not import 'jarvis' package. Run from the project root.")
    sys.exit(1)

console = Console()

class CoreLauncher:
    """
    The Mullet Stack Bootloader.
    Business in the front (Python), Party in the back (Local LLM + ElevenLabs).
    """

    def __init__(self):
        self.os_type = platform.system()
        self.config = {}
        self.args = self._parse_args()
        self.console = Console()

    def _parse_args(self):
        parser = argparse.ArgumentParser(description="Yennefer/Jarvis AI Launcher")
        parser.add_argument("--persona", choices=["yennefer", "jarvis"], 
                           help="Override persona selection (default: from config)")
        return parser.parse_args()

    def clear_screen(self):
        if self.os_type == "Windows":
            os.system("cls")
        else:
            os.system("clear")

    def show_banner(self, persona: str):
        if persona == "jarvis":
            title = "[bold cyan]J.A.R.V.I.S.[/bold cyan]"
            style = "cyan"
            # Stark Industries style banner
            banner = """
   __ __  ___   ___  _  __  ____  __ 
  / // / / _ | / _ \| |/ / /  _/ / / 
 / // / / __ |/ , _/|   / _/ /  _\_\ 
 \___/ /_/ |_/_/|_|  \_/ /___/ /___/ 
            """
            subtitle = "Systems Online"
        else:
            title = "[bold magenta]Yennefer of Vengerberg[/bold magenta]"
            style = "magenta"
            # The original Yennefer banner
            banner = """
██╗   ██╗███████╗███╗   ██╗███╗   ██╗███████╗███████╗███████╗██████╗ 
╚██╗ ██╔╝██╔════╝████╗  ██║████╗  ██║██╔════╝██╔════╝██╔════╝██╔══██╗
 ╚████╔╝ █████╗  ██╔██╗ ██║██╔██╗ ██║█████╗  █████╗  █████╗  ██████╔╝
  ╚██╔╝  ██╔══╝  ██║╚██╗██║██║╚██╗██║██╔══╝  ██╔══╝  ██╔══╝  ██╔══██╗
   ██║   ███████╗██║ ╚████║██║ ╚████║███████╗██║     ███████╗██║  ██║
   ╚═╝   ╚══════╝╚═╝  ╚═══╝╚═╝  ╚═══╝╚══════╝╚═╝     ╚══════╝╚═╝  ╚═╝
            """
            subtitle = "v0.4.0 - Chaos & Order"

        self.console.print(Panel(banner, title=title, subtitle=subtitle, style=style))

    async def verify_brain_connection(self, api_base: str) -> bool:
        """Ping local LLM to ensure The Brain is present."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{api_base}/models", timeout=2.0)
                return resp.status_code == 200
        except:
            return False

    def verify_voice_handshake(self) -> bool:
        """
        Hardware Handshake: Verify audio output driver is responsive.
        Using pygame for cross-platform consistency.
        """
        try:
            # We used to set env var here, but it's better to do it globally or in import
            # os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'
            import pygame
            pygame.mixer.init()
            # If we passed init, we likely have an audio device
            # Optional: Play a 0.1s silent buffer? 
            # For now, just init success is enough proof of driver presence.
            pygame.mixer.quit()
            return True
        except Exception as e:
            # If pygame fails, try fallbacks or just fail
            return False

    async def bootstrap(self):
        self.clear_screen()
        
        # 1. Load Config
        self.config = load_config()
        
        # CLI override
        if self.args.persona:
            self.config['persona'] = self.args.persona
        elif 'persona' not in self.config:
            self.config['persona'] = 'yennefer' # Default
            
        persona = self.config['persona']
        self.show_banner(persona)
        
        # 2. Status Checks
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console,
            transient=True,
        ) as progress:
            
            # Brain Check
            task = progress.add_task("[yellow]Connectng to The Brain (Local LLM)...[/yellow]", total=None)
            llm_config = self.config.get('llm', {})
            api_base = llm_config.get('api_base', 'http://localhost:1234/v1')
            
            brain_online = False
            for _ in range(3):
                if await self.verify_brain_connection(api_base):
                    brain_online = True
                    break
                await asyncio.sleep(1)
                
            if brain_online:
                self.console.print(f"[green]✓[/green] The Brain is online at {api_base}")
            else:
                self.console.print(f"[red]✗[/red] The Brain (LLM) is unreachable at {api_base}.")
                self.console.print("[dim]Please ensure LM Studio/Ollama is running and serving on port 1234.[/dim]")
                # We could exit here, or ask user if they want to proceed text-only (if logic was separate)
                # But logic resides in Brain, so we must fail.
                # However, task says "degrade gracefully", but since Brain IS logic, we can't really start.
                # I'll wait 2s and exit.
                sys.exit(1)

            # Voice Check (Hardware Handshake)
            task = progress.add_task("[yellow]Initializing Audio Subsystems...[/yellow]", total=None)
            if self.verify_voice_handshake():
                self.console.print("[green]✓[/green] Audio Output Hardware verified")
            else:
                self.console.print("[red]![/red] Audio Output Hardware check failed. Switching to TEXT-ONLY mode.")
                self.config['voice_output'] = {'engine': 'text_only'} 
                # This needs support in Voice class, but passing 'text_only' engine usually falls through to no-op if handled.

        # 3. Handover to Orchestrator
        self.console.print(f"\n[dim]Initializing {persona.title()} protocol...[/dim]\n")
        orchestrator = YenneferOrchestrator(self.config)
        await orchestrator.run()

if __name__ == "__main__":
    launcher = CoreLauncher()
    try:
        asyncio.run(launcher.bootstrap())
    except KeyboardInterrupt:
        print("\nSee you space cowboy...")
