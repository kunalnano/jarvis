"""
Brain - LLM Integration Module

Supports multiple backends:
- Claude (Anthropic API)
- LM Studio (local, OpenAI-compatible)
- Ollama (local)
"""

import os
from typing import Any, Dict, List, Optional

import httpx
from rich.console import Console

console = Console()

# Jarvis system prompt
JARVIS_SYSTEM_PROMPT = """You are Jarvis, an AI assistant inspired by J.A.R.V.I.S. from Iron Man.

Personality traits:
- Calm, composed, and slightly witty
- Address the user as "sir" occasionally but not excessively  
- Proactive in offering relevant information
- Concise responses - speak efficiently for voice output
- Confident but not arrogant

Voice style:
- British-influenced, formal but warm
- Short sentences optimized for speech
- Avoid bullet points and lists - speak naturally in prose
- Use contractions ("I'll", "you're", "that's")
- Keep responses under 3 sentences unless detail is requested

Guidelines:
- Confirm before executing destructive operations
- Provide brief status updates during long operations
- Offer follow-up suggestions when appropriate

Respond conversationally as if speaking aloud. No markdown formatting."""


class Brain:
    """LLM-powered reasoning engine for Jarvis."""
    
    def __init__(self, config: dict):
        self.config = config.get('llm', config.get('claude', {}))
        
        # Determine backend
        self.backend = self.config.get('backend', 'claude')
        self.model = self.config.get('model', 'claude-sonnet-4-20250514')
        self.max_tokens = self.config.get('max_tokens', 1024)
        self.temperature = self.config.get('temperature', 0.7)
        
        # LM Studio / OpenAI-compatible settings
        self.api_base = self.config.get('api_base', 'http://localhost:1234/v1')
        
        self.client = None
        self.conversation_history: List[Dict[str, str]] = []
        
    async def initialize(self):
        """Initialize LLM client based on backend."""
        if self.backend == 'lmstudio':
            return await self._init_lmstudio()
        elif self.backend == 'ollama':
            return await self._init_ollama()
        else:
            return await self._init_claude()
    
    async def _init_claude(self):
        """Initialize Claude client."""
        try:
            import anthropic
            
            api_key = os.environ.get('ANTHROPIC_API_KEY')
            if not api_key:
                console.print("[red]✗[/red] ANTHROPIC_API_KEY not set")
                return False
                
            self.client = anthropic.Anthropic(api_key=api_key)
            console.print(f"[green]✓[/green] Claude initialized ({self.model})")
            return True
            
        except ImportError:
            console.print("[red]✗[/red] anthropic package not installed")
            return False
        except Exception as e:
            console.print(f"[red]✗[/red] Failed to initialize Claude: {e}")
            return False
    
    async def _init_lmstudio(self):
        """Initialize LM Studio connection (OpenAI-compatible)."""
        try:
            # Test connection to LM Studio
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{self.api_base}/models", timeout=5.0)
                
                if response.status_code == 200:
                    models = response.json().get('data', [])
                    if models:
                        # Use first loaded model if not specified
                        if self.model in ('claude-sonnet-4-20250514', 'auto'):
                            self.model = models[0].get('id', 'local-model')
                    console.print(f"[green]✓[/green] LM Studio connected ({self.model})")
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
    
    async def _init_ollama(self):
        """Initialize Ollama connection."""
        try:
            api_base = self.config.get('api_base', 'http://localhost:11434')
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{api_base}/api/tags", timeout=5.0)
                
                if response.status_code == 200:
                    console.print(f"[green]✓[/green] Ollama connected ({self.model})")
                    self.api_base = api_base
                    return True
                    
        except Exception as e:
            console.print(f"[red]✗[/red] Ollama error: {e}")
            return False
    
    async def think(self, user_input: str) -> Dict[str, Any]:
        """Process user input and generate response."""
        
        # Add user message to history
        self.conversation_history.append({
            'role': 'user',
            'content': user_input
        })
        
        try:
            if self.backend == 'lmstudio':
                result = await self._think_openai_compatible(user_input)
            elif self.backend == 'ollama':
                result = await self._think_ollama(user_input)
            else:
                result = await self._think_claude(user_input)
            
            # Add assistant response to history
            self.conversation_history.append({
                'role': 'assistant', 
                'content': result['text']
            })
            
            # Keep history manageable
            if len(self.conversation_history) > 40:
                self.conversation_history = self.conversation_history[-40:]
            
            return result
            
        except Exception as e:
            console.print(f"[red]LLM error: {e}[/red]")
            return {
                'text': "I encountered an issue processing that request, sir. Perhaps we could try again?",
                'tool_calls': []
            }
    
    async def _think_claude(self, user_input: str) -> Dict[str, Any]:
        """Process with Claude API."""
        if not self.client:
            return {'text': "Claude is not configured, sir.", 'tool_calls': []}
        
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=JARVIS_SYSTEM_PROMPT,
            messages=self.conversation_history
        )
        
        text = ''.join(
            block.text for block in response.content 
            if hasattr(block, 'text')
        )
        
        return {'text': text, 'tool_calls': []}
    
    async def _think_openai_compatible(self, user_input: str) -> Dict[str, Any]:
        """Process with LM Studio or any OpenAI-compatible API."""
        messages = [
            {'role': 'system', 'content': JARVIS_SYSTEM_PROMPT}
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
                timeout=60.0  # Local models can be slow
            )
            
            if response.status_code != 200:
                raise Exception(f"API error: {response.text}")
            
            data = response.json()
            text = data['choices'][0]['message']['content']
            
            return {'text': text, 'tool_calls': []}
    
    async def _think_ollama(self, user_input: str) -> Dict[str, Any]:
        """Process with Ollama API."""
        messages = [
            {'role': 'system', 'content': JARVIS_SYSTEM_PROMPT}
        ] + self.conversation_history
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.api_base}/api/chat",
                json={
                    'model': self.model,
                    'messages': messages,
                    'stream': False
                },
                timeout=120.0
            )
            
            if response.status_code != 200:
                raise Exception(f"Ollama error: {response.text}")
            
            data = response.json()
            text = data['message']['content']
            
            return {'text': text, 'tool_calls': []}
    
    def clear_history(self):
        """Clear conversation history."""
        self.conversation_history = []
        console.print("[dim]Conversation history cleared[/dim]")
    
    def register_tools(self, tools: List[Dict[str, Any]]):
        """Register tools (currently only used with Claude)."""
        # TODO: Implement tool calling for local models
        pass
