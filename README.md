# 🤖 Jarvis

A voice-activated AI assistant powered by Claude, inspired by J.A.R.V.I.S. from Iron Man.

## Features

- 🎤 **Voice Control** - Use Wispr Flow for dictation into text prompt
- 🧠 **Claude Brain** - Powered by Anthropic's Claude
- 🔧 **Tools** - Control your computer, open apps, run commands
- 📢 **Natural Speech** - ElevenLabs or macOS TTS for responses

## Quick Start

```bash
# Clone and setup
cd ~/Projects/jarvis
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set your API key
export ANTHROPIC_API_KEY=your-key-here

# Run
python -m jarvis.main
```

## Usage

1. Run Jarvis
2. When you see `You:` prompt, either:
   - Type your command, OR
   - Activate Wispr Flow and dictate
3. Jarvis responds with voice + text

### Example Commands

```
You: What time is it?
You: Open Safari
You: Search the web for weather in Austin
You: List files in my Downloads folder
You: Check my battery level
```

## Configuration

Edit `config/jarvis.yaml`:

```yaml
voice_output:
  engine: macos           # macos (free) or elevenlabs (better quality)
  macos_voice: Daniel     # British voice
  rate: 180

claude:
  model: claude-sonnet-4-20250514
  max_tokens: 1024
```

## Architecture

```
┌──────────────────────────────────────────┐
│              Jarvis                       │
├──────────────────────────────────────────┤
│  Text Input ──→ Claude ──→ Voice Out     │
│  (Wispr Flow)     │          (TTS)       │
│                   ↓                       │
│               Tools                       │
│         (apps, files, web)               │
└──────────────────────────────────────────┘
```

## Available Tools

| Tool | Description |
|------|-------------|
| `open_application` | Open any macOS app |
| `open_url` | Open URL in browser |
| `run_command` | Execute shell commands |
| `read_file` | Read file contents |
| `list_directory` | List folder contents |
| `get_system_info` | Battery, disk, memory |
| `search_web` | Open web search |

## Requirements

- Python 3.10+
- macOS (primary target)
- Anthropic API key
- Wispr Flow (for voice dictation)
- ElevenLabs API key (optional, for better voice)

## License

MIT
