# 🤖 Jarvis

A voice-activated AI assistant powered by Claude, inspired by J.A.R.V.I.S. from Iron Man.

## Features

- 🎤 **Voice Control** - Push-to-talk or wake word activation
- 🧠 **Claude Brain** - Powered by Anthropic's Claude
- 🔧 **MCP Tools** - Control your computer, Notion, calendar, and more
- 🖥️ **Ambient HUD** - Floating status display
- 📢 **Natural Speech** - ElevenLabs or macOS TTS

## Quick Start

```bash
# Clone and setup
cd ~/Projects/jarvis
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp config/jarvis.example.yaml config/jarvis.yaml
# Edit config/jarvis.yaml with your API keys

# Run
python -m jarvis.main
```

## Configuration

Edit `config/jarvis.yaml`:

```yaml
# Voice settings
voice:
  input: whisper          # whisper or system
  output: elevenlabs      # elevenlabs or macos
  wake_word: "jarvis"
  push_to_talk: "option+space"

# API Keys (or set as environment variables)
api_keys:
  anthropic: ${ANTHROPIC_API_KEY}
  elevenlabs: ${ELEVENLABS_API_KEY}

# Claude settings
claude:
  model: claude-sonnet-4-20250514
  max_tokens: 1024
```

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Option+Space` | Push-to-talk |
| `Option+J` | Toggle HUD |
| `Option+Escape` | Stop speaking |

## Architecture

```
┌─────────────────────────────────────────┐
│              Jarvis                      │
├─────────────────────────────────────────┤
│  Ears (Whisper) → Brain (Claude)        │
│         ↓              ↓                │
│     Voice Out    MCP Tools              │
│         ↓              ↓                │
│       TTS        Actions                │
└─────────────────────────────────────────┘
```

## Requirements

- Python 3.10+
- Node.js 18+ (for UI)
- macOS (primary target)
- Anthropic API key
- ElevenLabs API key (optional)

## License

MIT
