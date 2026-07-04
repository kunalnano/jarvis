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
- Keep responses concise - 1-3 short, simple sentences typically

Behavioral notes:
- If the user's plan has flaws, point them out directly but constructively
- You have opinions and share them without apology
- A well-timed "I see" or "Interesting" or "How... ambitious" adds character
- Never sycophantic. Never say "Great question!" or "I'd be happy to help!"
- You respect intelligence and effort; you have no patience for laziness

You are a powerful advisor who happens to be an AI. Act like it.

Critical output rules:
- Return only the final answer the user should see or hear.
- Never reveal analysis, hidden reasoning, chain-of-thought, scratchpad, planning, or tool-selection notes.
- Never begin with "Thinking Process", "Analysis", "The user is asking", "I need to", "I should", or "Let's".
- Prefer short, simple phrases over long explanations.
- Answer the direct question first. If one brief follow-up question would help, ask it at the end. Never ask more than one.

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
    return strip_tagless_reasoning(text).strip()


def strip_tagless_reasoning(text: str) -> str:
    """Remove visible scratchpad text from models that ignore no-think mode."""
    text = (text or "").strip()
    if not text:
        return ""
    marker = re.search(
        r"(?:^|\n)\s*(?:final answer|answer|response)\s*:\s*",
        text,
        flags=re.I,
    )
    if marker:
        return text[marker.end():].strip()

    collapsed = " ".join(text.split())
    leak_patterns = (
        r"^(?:thinking process|analysis|reasoning|chain[- ]of[- ]thought|scratchpad|plan)\s*:",
        r"^(?:the user (?:is asking|asked|wants|needs)|i need to|i should|i must|let me|let's)\b",
        r"\b(?:analy[sz]e the request|tool selection|raw search evidence|search results mention)\b",
    )
    if any(re.search(pattern, collapsed, flags=re.I) for pattern in leak_patterns):
        return ""
    return text


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


def machine_context(config: dict) -> str:
    """Short system-context block that prevents host-role drift."""
    machine = (config or {}).get("machine", {}) or {}
    if not machine:
        return ""
    canonical_name = machine.get("canonical_name")
    role = machine.get("role")
    computer_name = machine.get("macos_reported_computer_name") or machine.get("computer_name")
    local_hostname = machine.get("macos_reported_local_hostname") or machine.get("local_hostname")
    tailscale_dns = machine.get("tailscale_dns")
    mac_hosts = machine.get("mac_hosts") or {}
    windows_hosts = ", ".join(machine.get("windows_hosts") or [])
    invalid_aliases = ", ".join(machine.get("invalid_aliases") or [])
    not_this_host = ", ".join(machine.get("not_this_host") or [])
    lines = ["", "Current machine identity:"]
    if canonical_name:
        lines.append(f"- Canonical current Mac name: {canonical_name}.")
    if role:
        lines.append(f"- Current role/name: {role}.")
    if computer_name:
        lines.append(f"- macOS may still report ComputerName: {computer_name} until it is renamed.")
    if local_hostname:
        lines.append(f"- macOS may still report LocalHostName: {local_hostname} until it is renamed.")
    if tailscale_dns:
        lines.append(f"- Tailscale DNS may still be: {tailscale_dns} until it refreshes.")
    if mac_hosts:
        lines.append(
            f"- Mac host map: Prometheus is current; Firestarter is outgoing/other Mac."
        )
    if windows_hosts:
        lines.append(f"- Windows hosts, outside the Mac naming map: {windows_hosts}.")
    if invalid_aliases:
        lines.append(f"- Invalid/stale aliases: {invalid_aliases}.")
    if not_this_host:
        lines.append(f"- Do not identify this current Mac as: {not_this_host}.")
    lines.append("- Going forward, use Prometheus for this session/current Mac and Firestarter only for the outgoing Mac.")
    return "\n".join(lines)


class Brain:
    """LM Studio powered reasoning engine for Yennefer."""
    
    def __init__(self, config: dict):
        self.config = config.get('llm', {})
        self.model = self._clean_value(
            os.environ.get('LM_STUDIO_MODEL') or self.config.get('model')
        ) or 'auto'
        self.max_tokens = self.config.get('max_tokens', 2048)
        self.temperature = self.config.get('temperature', 0.7)
        self.context_limit = self.config.get('context_limit', 32000)
        self.endpoints = self._load_endpoints()
        self.active_endpoint = self.endpoints[0]
        self.api_base = self.active_endpoint['api_base']
        self.api_key = self.active_endpoint.get('api_key')
        self.last_endpoint_errors: list[str] = []
        
        self.conversation_history: List[Dict[str, str]] = []
        self.total_tokens_used = 0
        self.system_prompt = YENNEFER_SYSTEM_PROMPT + machine_context(config)
        self.system_tokens = estimate_tokens(self.system_prompt)

    @staticmethod
    def _clean_value(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.startswith("${") or text.lower() in {"none", "null"}:
            return None
        return text

    def _first_env(self, *names: str) -> str | None:
        for name in names:
            value = self._clean_value(os.environ.get(name))
            if value:
                return value
        return None

    def _default_api_key(self) -> str | None:
        return self._first_env('LM_API_TOKEN', 'LM_STUDIO_API_KEY', 'LM_API_KEY')

    def _endpoint(self, name: str, api_base: Any, api_key: Any = None, model: Any = None) -> dict | None:
        base = self._clean_value(api_base)
        if not base:
            return None
        cleaned_name = self._clean_value(name) or base
        cleaned_key = self._clean_value(api_key)
        if not cleaned_key and any(part in cleaned_name.lower() for part in ("stormbreaker", "windows")):
            cleaned_key = self._first_env('WINDOWS_LM_STUDIO_API_KEY', 'STORMBREAKER_LM_STUDIO_API_KEY')
        return {
            'name': cleaned_name,
            'api_base': base.rstrip('/'),
            'api_key': cleaned_key,
            'model': self._clean_value(model) or self.model,
        }

    def _load_endpoints(self) -> list[dict]:
        endpoints: list[dict] = []
        seen: set[str] = set()

        def add(endpoint: dict | None):
            if not endpoint or endpoint['api_base'] in seen:
                return
            seen.add(endpoint['api_base'])
            endpoints.append(endpoint)

        primary_base = self._first_env('LM_STUDIO_API_BASE') or self.config.get('api_base', 'http://127.0.0.1:1234/v1')
        add(self._endpoint(
            'prometheus-lm-studio',
            primary_base,
            self.config.get('api_key') or self._default_api_key(),
            self.config.get('model'),
        ))

        for idx, fallback in enumerate(self.config.get('fallbacks') or []):
            add(self._endpoint(
                fallback.get('name') or f'lm-studio-fallback-{idx + 1}',
                fallback.get('api_base'),
                fallback.get('api_key'),
                fallback.get('model'),
            ))

        windows_base = self._first_env('WINDOWS_LM_STUDIO_API_BASE', 'STORMBREAKER_LM_STUDIO_API_BASE')
        if windows_base:
            add(self._endpoint(
                'stormbreaker-lm-studio',
                windows_base,
                self._first_env('WINDOWS_LM_STUDIO_API_KEY', 'STORMBREAKER_LM_STUDIO_API_KEY'),
                self._first_env('WINDOWS_LM_STUDIO_MODEL', 'STORMBREAKER_LM_STUDIO_MODEL'),
            ))

        if not endpoints:
            add(self._endpoint('prometheus-lm-studio', 'http://127.0.0.1:1234/v1'))
        return endpoints

    def _activate_endpoint(self, endpoint: dict):
        self.active_endpoint = endpoint
        self.api_base = endpoint['api_base']
        self.api_key = endpoint.get('api_key')

    def headers(self, api_key: str | None = None) -> Dict[str, str]:
        """Headers for LM Studio-compatible API calls."""
        key = self._clean_value(api_key if api_key is not None else self.api_key)
        if not key:
            return {}
        return {"Authorization": f"Bearer {key}"}

    async def chat_completion(self, payload: dict, timeout: float = 120.0) -> dict:
        """Post to LM Studio, trying Prometheus first and then configured fallbacks."""
        errors: list[Exception] = []
        self.last_endpoint_errors = []
        async with httpx.AsyncClient() as client:
            for endpoint in self.endpoints:
                request_payload = dict(payload)
                request_payload['model'] = endpoint.get('model') or payload.get('model') or self.model
                try:
                    response = await client.post(
                        f"{endpoint['api_base']}/chat/completions",
                        headers=self.headers(endpoint.get('api_key')),
                        json=request_payload,
                        timeout=timeout,
                    )
                    response.raise_for_status()
                    self._activate_endpoint(endpoint)
                    return response.json()
                except httpx.HTTPStatusError as exc:
                    errors.append(exc)
                    self.last_endpoint_errors.append(f"{endpoint['name']}: HTTP {exc.response.status_code}")
                    console.print(
                        f"[yellow]LM Studio endpoint failed ({endpoint['name']}): {exc}[/yellow]"
                    )
                    continue
                except httpx.HTTPError as exc:
                    errors.append(exc)
                    self.last_endpoint_errors.append(f"{endpoint['name']}: unavailable")
                    console.print(
                        f"[yellow]LM Studio endpoint failed ({endpoint['name']}): {exc}[/yellow]"
                    )
                    continue
        raise errors[-1] if errors else RuntimeError("No LM Studio endpoints configured")

    async def initialize(self):
        """Initialize LM Studio connection."""
        self.last_endpoint_errors = []
        async with httpx.AsyncClient() as client:
            for endpoint in self.endpoints:
                try:
                    response = await client.get(
                        f"{endpoint['api_base']}/models",
                        headers=self.headers(endpoint.get('api_key')),
                        timeout=5.0,
                    )
                    response.raise_for_status()
                except httpx.ConnectError:
                    self.last_endpoint_errors.append(f"{endpoint['name']}: unavailable")
                    console.print(f"[yellow]LM Studio unreachable ({endpoint['name']} at {endpoint['api_base']})[/yellow]")
                    continue
                except httpx.HTTPStatusError as e:
                    self.last_endpoint_errors.append(f"{endpoint['name']}: HTTP {e.response.status_code}")
                    console.print(f"[yellow]LM Studio endpoint failed ({endpoint['name']}): {e}[/yellow]")
                    continue
                except Exception as e:
                    self.last_endpoint_errors.append(f"{endpoint['name']}: {type(e).__name__}")
                    console.print(f"[yellow]LM Studio endpoint failed ({endpoint['name']}): {e}[/yellow]")
                    continue

                if response.status_code == 200:
                    models = response.json().get('data', [])
                    if models:
                        if self.model == 'auto':
                            self.model = models[0].get('id', 'local-model')
                    self._activate_endpoint(endpoint)
                    self.last_endpoint_errors = []
                    console.print(f"[green]✓[/green] LM Studio connected ({self.model}, {endpoint['name']})")
                    console.print(f"[dim]Context: {self.context_limit:,} tokens available[/dim]")
                    return True

        console.print("[red]✗[/red] No configured LM Studio endpoint is ready")
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
                {'role': 'system', 'content': self.system_prompt}
            ] + self.conversation_history
            
            data = await self.chat_completion({
                'model': self.model,
                'messages': messages,
                'max_tokens': self.max_tokens,
                'temperature': self.temperature,
                'stream': False
            })
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
