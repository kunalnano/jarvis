"""
Brain - Claude Integration Module

Handles communication with Claude API and manages conversation context.
"""

import os
from typing import Any, Dict, List, Optional

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

Available capabilities:
- Control the computer (open apps, files, URLs)
- Access and search files on the system
- Execute terminal commands
- Answer questions from your knowledge
- Help with tasks and planning

Guidelines:
- Confirm before executing destructive operations
- Provide brief status updates during long operations
- Offer follow-up suggestions when appropriate
- If you cannot do something, explain briefly and suggest alternatives

Respond conversationally as if speaking aloud. No markdown formatting."""


class Brain:
    """Claude-powered reasoning engine for Jarvis."""
    
    def __init__(self, config: dict):
        self.config = config.get('claude', {})
        self.model = self.config.get('model', 'claude-sonnet-4-20250514')
        self.max_tokens = self.config.get('max_tokens', 1024)
        self.temperature = self.config.get('temperature', 0.7)
        
        self.client = None
        self.conversation_history: List[Dict[str, str]] = []
        self.tools = []
        
    async def initialize(self):
        """Initialize Claude client."""
        try:
            import anthropic
            
            api_key = os.environ.get('ANTHROPIC_API_KEY')
            if not api_key:
                console.print("[red]✗[/red] ANTHROPIC_API_KEY not set")
                console.print("[dim]Set it with: export ANTHROPIC_API_KEY=your-key[/dim]")
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
    
    def register_tools(self, tools: List[Dict[str, Any]]):
        """Register available tools for Claude to use."""
        self.tools = tools
        console.print(f"[green]✓[/green] Registered {len(tools)} tools")
    
    async def think(self, user_input: str) -> Dict[str, Any]:
        """
        Process user input and generate response.
        
        Returns:
            Dict with 'text' (response to speak) and optionally 'tool_calls'
        """
        if not self.client:
            return {
                'text': "I'm sorry sir, but I'm not fully operational. My connection to Claude isn't configured.",
                'tool_calls': []
            }
        
        # Add user message to history
        self.conversation_history.append({
            'role': 'user',
            'content': user_input
        })
        
        try:
            # Build messages
            messages = self.conversation_history.copy()
            
            # Call Claude
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=JARVIS_SYSTEM_PROMPT,
                messages=messages,
                tools=self.tools if self.tools else None
            )
            
            # Parse response
            result = self._parse_response(response)
            
            # Add assistant response to history
            self.conversation_history.append({
                'role': 'assistant', 
                'content': result['text']
            })
            
            # Keep history manageable (last 20 exchanges)
            if len(self.conversation_history) > 40:
                self.conversation_history = self.conversation_history[-40:]
            
            return result
            
        except Exception as e:
            console.print(f"[red]Claude error: {e}[/red]")
            return {
                'text': "I encountered an issue processing that request, sir. Perhaps we could try again?",
                'tool_calls': []
            }
    
    def _parse_response(self, response) -> Dict[str, Any]:
        """Parse Claude's response into text and tool calls."""
        text_parts = []
        tool_calls = []
        
        for block in response.content:
            if block.type == 'text':
                text_parts.append(block.text)
            elif block.type == 'tool_use':
                tool_calls.append({
                    'id': block.id,
                    'name': block.name,
                    'input': block.input
                })
        
        return {
            'text': ' '.join(text_parts),
            'tool_calls': tool_calls,
            'stop_reason': response.stop_reason
        }
    
    def clear_history(self):
        """Clear conversation history."""
        self.conversation_history = []
        console.print("[dim]Conversation history cleared[/dim]")
    
    def get_history_summary(self) -> str:
        """Get a brief summary of conversation history."""
        count = len(self.conversation_history)
        return f"{count} messages in history"
