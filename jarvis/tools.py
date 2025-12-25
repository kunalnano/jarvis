"""
Tools - MCP Tool Wrappers

Provides tools for Claude to control the system.
"""

import asyncio
import subprocess
import webbrowser
from pathlib import Path
from typing import Any, Dict, List

from rich.console import Console

console = Console()


# Tool definitions for Claude
TOOL_DEFINITIONS = [
    {
        "name": "open_application",
        "description": "Open an application on the Mac. Use the app name like 'Safari', 'Finder', 'Terminal', 'Slack', etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "Name of the application to open"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "open_url",
        "description": "Open a URL in the default web browser.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to open"
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "run_command",
        "description": "Execute a shell command. Use for system tasks like checking disk space, listing files, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                }
            },
            "required": ["command"]
        }
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read"
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "list_directory",
        "description": "List files and folders in a directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the directory to list"
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "get_system_info",
        "description": "Get system information like battery level, disk space, memory usage.",
        "input_schema": {
            "type": "object",
            "properties": {
                "info_type": {
                    "type": "string",
                    "enum": ["battery", "disk", "memory", "all"],
                    "description": "Type of system info to retrieve"
                }
            },
            "required": ["info_type"]
        }
    },
    {
        "name": "search_web",
        "description": "Search the web using DuckDuckGo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query"
                }
            },
            "required": ["query"]
        }
    }
]


class Tools:
    """Tool execution handler."""
    
    def __init__(self, config: dict):
        self.config = config
        
    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Return tool definitions for Claude."""
        return TOOL_DEFINITIONS
    
    async def execute(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Execute a tool and return the result."""
        console.print(f"[dim]Executing: {tool_name}[/dim]")
        
        handlers = {
            'open_application': self._open_application,
            'open_url': self._open_url,
            'run_command': self._run_command,
            'read_file': self._read_file,
            'list_directory': self._list_directory,
            'get_system_info': self._get_system_info,
            'search_web': self._search_web,
        }
        
        handler = handlers.get(tool_name)
        if not handler:
            return f"Unknown tool: {tool_name}"
        
        try:
            result = await handler(tool_input)
            return result
        except Exception as e:
            return f"Error executing {tool_name}: {str(e)}"
    
    async def _open_application(self, input: Dict) -> str:
        """Open a macOS application."""
        app_name = input.get('app_name', '')
        
        process = await asyncio.create_subprocess_exec(
            'open', '-a', app_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()
        
        if process.returncode == 0:
            return f"Opened {app_name}"
        else:
            return f"Could not open {app_name}: {stderr.decode()}"
    
    async def _open_url(self, input: Dict) -> str:
        """Open a URL in default browser."""
        url = input.get('url', '')
        
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        webbrowser.open(url)
        return f"Opened {url}"
    
    async def _run_command(self, input: Dict) -> str:
        """Execute a shell command."""
        command = input.get('command', '')
        
        # Safety check - block dangerous commands
        dangerous = ['rm -rf /', 'mkfs', 'dd if=', ':(){', 'fork bomb']
        if any(d in command.lower() for d in dangerous):
            return "I cannot execute that command for safety reasons."
        
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        output = stdout.decode() or stderr.decode()
        return output[:1000] if output else "Command executed successfully"
    
    async def _read_file(self, input: Dict) -> str:
        """Read file contents."""
        path = Path(input.get('path', '')).expanduser()
        
        if not path.exists():
            return f"File not found: {path}"
        
        if not path.is_file():
            return f"Not a file: {path}"
        
        try:
            content = path.read_text()
            # Truncate large files
            if len(content) > 2000:
                return content[:2000] + f"\n... (truncated, {len(content)} chars total)"
            return content
        except Exception as e:
            return f"Error reading file: {e}"
    
    async def _list_directory(self, input: Dict) -> str:
        """List directory contents."""
        path = Path(input.get('path', '.')).expanduser()
        
        if not path.exists():
            return f"Directory not found: {path}"
        
        if not path.is_dir():
            return f"Not a directory: {path}"
        
        items = []
        for item in sorted(path.iterdir())[:50]:  # Limit to 50 items
            prefix = "📁" if item.is_dir() else "📄"
            items.append(f"{prefix} {item.name}")
        
        return "\n".join(items) if items else "Directory is empty"
    
    async def _get_system_info(self, input: Dict) -> str:
        """Get system information."""
        info_type = input.get('info_type', 'all')
        results = []
        
        if info_type in ('battery', 'all'):
            try:
                proc = await asyncio.create_subprocess_exec(
                    'pmset', '-g', 'batt',
                    stdout=asyncio.subprocess.PIPE
                )
                stdout, _ = await proc.communicate()
                results.append(f"Battery: {stdout.decode().strip()}")
            except:
                results.append("Battery: Unable to retrieve")
        
        if info_type in ('disk', 'all'):
            proc = await asyncio.create_subprocess_exec(
                'df', '-h', '/',
                stdout=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            lines = stdout.decode().strip().split('\n')
            if len(lines) > 1:
                results.append(f"Disk: {lines[1]}")
        
        if info_type in ('memory', 'all'):
            proc = await asyncio.create_subprocess_shell(
                "top -l 1 | grep PhysMem",
                stdout=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            results.append(f"Memory: {stdout.decode().strip()}")
        
        return "\n".join(results)
    
    async def _search_web(self, input: Dict) -> str:
        """Open a web search."""
        query = input.get('query', '')
        search_url = f"https://duckduckgo.com/?q={query.replace(' ', '+')}"
        webbrowser.open(search_url)
        return f"Opened search for: {query}"
