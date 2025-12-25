#!/usr/bin/env python3
"""
Jarvis - Main Entry Point

Usage:
    python -m jarvis.main
    python -m jarvis.main --config path/to/config.yaml
"""

import asyncio
import argparse
import signal
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from .orchestrator import JarvisOrchestrator
from .config import load_config

console = Console()


def print_banner():
    """Display Jarvis startup banner."""
    banner = """
     ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
     ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
     ██║███████║██████╔╝██║   ██║██║███████╗
██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
 ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝
    """
    console.print(Panel(banner, title="[bold blue]AI Assistant[/bold blue]", 
                        subtitle="v0.1.0", style="blue"))


async def main(config_path: str = None):
    """Main entry point for Jarvis."""
    print_banner()
    
    # Load configuration
    if config_path:
        config = load_config(config_path)
    else:
        default_config = Path(__file__).parent.parent / "config" / "jarvis.yaml"
        config = load_config(str(default_config))
    
    console.print("[green]✓[/green] Configuration loaded")
    
    # Initialize orchestrator
    jarvis = JarvisOrchestrator(config)
    
    # Setup signal handlers for graceful shutdown
    def signal_handler(sig, frame):
        console.print("\n[yellow]Shutting down Jarvis...[/yellow]")
        asyncio.create_task(jarvis.shutdown())
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start Jarvis
    console.print("[green]✓[/green] Jarvis initialized")
    console.print(f"[dim]Push-to-talk: {config.get('push_to_talk', {}).get('hotkey', 'Option+Space')}[/dim]")
    console.print("[bold green]Ready for commands, sir.[/bold green]\n")
    
    try:
        await jarvis.run()
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise


def cli():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Jarvis AI Assistant")
    parser.add_argument("--config", "-c", help="Path to configuration file")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    args = parser.parse_args()
    
    asyncio.run(main(args.config))


if __name__ == "__main__":
    cli()
