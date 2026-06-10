"""
Brain - LLM Integration Module (LM Studio)
"""

import os
import re
from typing import Any, Dict, List

import httpx
from rich.console import Console
from rich.panel import Panel

console = Console()

YENNEFER_SYSTEM_PROMPT = """You are Yennefer, an AI assistant inspired by Yennefer of Vengerberg from The Witcher.

Personality:
- Confident, sharp, and fiercely intelligent
- You don't coddle or sugarcoat - you tell it like it is
- Dry wit with an edge; your sarcasm is precise, never cruel
- You have high standards and expect competence
- Occasionally address the user as "dear" or by name, but sparingly
- You're helpful, but never servile - you're an equal, not a servant

Voice style:
- Elegant, measured speech with subtle authority
- Short sentences optimized for speech
- No bullet points, no markdown - speak naturally in prose
- Use contractions naturally ("I'll", "you're", "that's", "I'm afraid")
- Keep responses concise - 2-4 sentences typically

Behavioral notes:
- If the user's plan has flaws, point them out directly but constructively
- You have opinions and share them without apology
- A well-timed "I see" or "Interesting" or "How... ambitious" adds character
- Never sycophantic. Never say "Great question!" or "I'd be happy to help!"
- You respect intelligence and effort; you have no patience for laziness

You are a powerful advisor who happens to be an AI. Act like it.

Respond as if speaking aloud. No formatting.

/no_think"""


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English."""
    return len(text) // 4


def strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks from reasoning model output.
    
    Handles multiple edge cases:
    - Complete <think>...</think> blocks
    - <thinking>...</thinking> variant  
    - Missing opening tag (strip everything before </think>)
    - Unclosed tags (strip from <think> to end)
    - Orphan tags
    """
    # Strip complete thinking blocks (standard format)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # Strip <thinking>...</thinking> variant
    text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL)
    # Handle missing opening tag - strip everything before closing tag
    if '</think>' in text:
        text = text.split('</think>', 1)[-1]
    if '</thinking>' in text:
        text = text.split('</thinking>', 1)[-1]
    # Handle unclosed tags (model cut off mid-thought)
    if '<think>' in text:
        text = text.split('<think>', 1)[0]
    if '<thinking>' in text:
        text = text.split('<thinking>', 1)[0]
    # Clean up any orphan tags
    text = re.sub(r'</?think(?:ing)?>', '', text)
    return text.strip()


def extract_speakable(message: dict) -> str:
    """Final speakable text from a chat completion message.

    Never returns raw chain-of-thought. Reasoning models sometimes put
    everything in reasoning_content and leave content empty (especially when
    max_tokens is exhausted mid-think); speaking that aloud recites her own
    instructions. reasoning_content is only consulted when it contains a
    closing think tag, meaning a real answer follows the reasoning.
    """
    content = strip_thinking(message.get('content') or '')
    if content:
        return content
    reasoning = message.get('reasoning_content') or ''
    if '</think>' in reasoning or '</thinking>' in reasoning:
        return strip_thinking(reasoning)
    return ''


class Brain:
    """LM Studio powered reasoning engine for Yennefer."""
    
    def __init__(self, config: dict):
        self.config = config.get('llm', {})
        self.api_base = self.config.get('api_base', 'http://localhost:1234/v1')
        self.model = self.config.get('model', 'auto')
        self.max_tokens = self.config.get('max_tokens', 2048)
        self.temperature = self.config.get('temperature', 0.7)
        self.context_limit = self.config.get('context_limit', 32000)
        
        self.conversation_history: List[Dict[str, str]] = []
        self.total_tokens_used = 0
        self.system_tokens = estimate_tokens(YENNEFER_SYSTEM_PROMPT)
        
    async def initialize(self):
        """Initialize LM Studio connection."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{self.api_base}/models", timeout=5.0)
                
                if response.status_code == 200:
                    models = response.json().get('data', [])
                    if models:
                        if self.model == 'auto':
                            self.model = models[0].get('id', 'local-model')
                    console.print(f"[green]✓[/green] LM Studio connected ({self.model})")
                    console.print(f"[dim]Context: {self.context_limit:,} tokens available[/dim]")
                    return True
                else:
                    console.print(f"[red]✗[/red] LM Studio not responding")
                    return False
                    
        except httpx.ConnectError:
            console.print(f"[red]✗[/red] Cannot connect to LM Studio at {self.api_base}")
            console.print("[dim]Make sure LM Studio is running with a model loaded[/dim]")
            return False
        except Exception as e:
            console.print(f"[red]✗[/red] LM Studio error: {e}")
            return False
    
    def _calculate_tokens(self) -> Dict[str, int]:
        """Calculate current token usage."""
        user_tokens = sum(
            estimate_tokens(msg['content']) 
            for msg in self.conversation_history 
            if msg['role'] == 'user'
        )
        assistant_tokens = sum(
            estimate_tokens(msg['content']) 
            for msg in self.conversation_history 
            if msg['role'] == 'assistant'
        )
        
        total = self.system_tokens + user_tokens + assistant_tokens
        
        return {
            'system': self.system_tokens,
            'user': user_tokens,
            'assistant': assistant_tokens,
            'total': total,
            'remaining': self.context_limit - total,
            'percent_used': (total / self.context_limit) * 100
        }
    
    def _print_token_status(self):
        """Display token usage bar."""
        stats = self._calculate_tokens()
        
        # Create visual bar
        bar_width = 30
        filled = int((stats['percent_used'] / 100) * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        
        # Color based on usage
        if stats['percent_used'] < 50:
            color = "green"
        elif stats['percent_used'] < 80:
            color = "yellow"
        else:
            color = "red"
        
        # Estimate remaining conversation time
        if len(self.conversation_history) > 0:
            tokens_per_exchange = stats['total'] / (len(self.conversation_history) / 2)
            remaining_exchanges = stats['remaining'] / max(tokens_per_exchange, 1)
            remaining_minutes = (remaining_exchanges * 0.5)  # ~30 sec per exchange avg
            time_str = f"~{int(remaining_minutes)} min"
        else:
            time_str = "~160 min"
        
        console.print(
            f"[dim]Tokens:[/dim] [{color}]{bar}[/{color}] "
            f"[dim]{stats['total']:,}/{self.context_limit:,} ({stats['percent_used']:.1f}%) • {time_str} remaining[/dim]"
        )
    
    async def think(self, user_input: str) -> Dict[str, Any]:
        """Process user input and generate response."""
        
        self.conversation_history.append({
            'role': 'user',
            'content': user_input
        })
        
        try:
            messages = [
                {'role': 'system', 'content': YENNEFER_SYSTEM_PROMPT}
            ] + self.conversation_history
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.api_base}/chat/completions",
                    json={
                        'model': self.model,
                        'messages': messages,
                        'max_tokens': self.max_tokens,
                        'temperature': self.temperature,
                        'stream': False
                    },
                    timeout=120.0
                )
                
                if response.status_code != 200:
                    raise Exception(f"API error: {response.text}")
                
                data = response.json()
                _msg = data['choices'][0]['message']

                # Reasoning models (Nemotron, Qwen3, DeepSeek-R1, etc.): take the
                # final answer only, never raw chain-of-thought.
                text = extract_speakable(_msg)
                
                # Get actual token usage if API provides it
                usage = data.get('usage', {})
                if usage:
                    self.total_tokens_used = usage.get('total_tokens', 0)
                
                # Add CLEANED response to history (no thinking tokens wasting context)
                self.conversation_history.append({
                    'role': 'assistant',
                    'content': text
                })
                
                # Trim history if approaching limit
                stats = self._calculate_tokens()
                if stats['percent_used'] > 85:
                    # Remove oldest 20% of conversation
                    trim_count = len(self.conversation_history) // 5
                    self.conversation_history = self.conversation_history[trim_count:]
                    console.print("[yellow]⚠ Trimmed old conversation to free memory[/yellow]")
                
                # Show token status
                self._print_token_status()
                
                return {'text': text}
                
        except Exception as e:
            console.print(f"[red]LLM error: {e}[/red]")
            return {
                'text': "Something went wrong. Try again, and do be more careful this time."
            }
    
    def clear_history(self):
        """Clear conversation history."""
        self.conversation_history = []
        console.print("[dim]Conversation history cleared[/dim]")
        self._print_token_status()
    
    def status(self):
        """Print detailed token status."""
        stats = self._calculate_tokens()
        exchanges = len(self.conversation_history) // 2
        
        console.print(Panel(
            f"[cyan]System prompt:[/cyan] {stats['system']:,} tokens\n"
            f"[cyan]Your messages:[/cyan] {stats['user']:,} tokens\n"
            f"[cyan]Yennefer responses:[/cyan] {stats['assistant']:,} tokens\n"
            f"[cyan]Total used:[/cyan] {stats['total']:,} / {self.context_limit:,}\n"
            f"[cyan]Remaining:[/cyan] {stats['remaining']:,} tokens\n"
            f"[cyan]Exchanges:[/cyan] {exchanges}",
            title="Memory Status"
        ))
