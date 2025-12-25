# 🤖 Jarvis

A voice-activated AI assistant powered by local LLMs or Claude, inspired by J.A.R.V.I.S. from Iron Man.

## Features

- 🎤 **Voice Control** - Use Wispr Flow for dictation into text prompt
- 🧠 **Multiple LLM Backends** - LM Studio, Ollama, or Claude
- 🔧 **Tools** - Control your computer, open apps, run commands
- 📢 **Natural Speech** - ElevenLabs or macOS TTS for responses

## Quick Start

```bash
cd ~/Projects/jarvis
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m jarvis.main
```

## LLM Backend Options

### Option 1: LM Studio (Recommended for Free Local)

1. **On your Windows GPU machine:**
   - Install [LM Studio](https://lmstudio.ai/)
   - Download a model (recommended: Llama 3.2, Mistral, or Qwen)
   - Load the model
   - Start the server: Local Server → Start Server
   - Enable network access in settings (to access from Mac)

2. **Find your Windows IP:**
   ```cmd
   ipconfig
   ```
   Look for IPv4 Address (e.g., `192.168.1.100`)

3. **Configure Jarvis** (`config/jarvis.yaml`):
   ```yaml
   llm:
     backend: lmstudio
     api_base: "http://192.168.1.100:1234/v1"
     model: auto
   ```

### Option 2: Ollama (Local on Mac)

```bash
# Install Ollama
brew install ollama

# Pull a model
ollama pull llama3.2

# Run (auto-starts server)
ollama serve
```

Configure:
```yaml
llm:
  backend: ollama
  api_base: "http://localhost:11434"
  model: llama3.2
```

### Option 3: Claude API (Best Quality, Paid)

```bash
export ANTHROPIC_API_KEY=your-key-here
```

Configure:
```yaml
llm:
  backend: claude
  model: claude-sonnet-4-20250514
```

## Usage

1. Run Jarvis
2. When you see `You:` prompt, either:
   - Type your command, OR
   - Activate Wispr Flow and dictate
3. Jarvis responds with voice + text

### Example Commands

```
You: What day is today?
You: Tell me a joke
You: Explain quantum computing in simple terms
You: Open Safari
You: What's the weather like? (will search web)
```

## Configuration

Edit `config/jarvis.yaml`:

```yaml
# LLM Backend
llm:
  backend: lmstudio        # lmstudio, ollama, or claude
  api_base: "http://192.168.1.100:1234/v1"
  model: auto
  max_tokens: 1024
  temperature: 0.7

# Voice output
voice_output:
  engine: macos            # macos (free) or elevenlabs
  macos_voice: Daniel      # British voice
  rate: 180
```

## Architecture

```
┌──────────────────────────────────────────┐
│              Jarvis                       │
├──────────────────────────────────────────┤
│  Text Input ──→ LLM Backend ──→ Voice Out│
│  (Wispr Flow)      │            (TTS)    │
│                    ↓                      │
│    ┌───────────────────────────┐         │
│    │ LM Studio │ Ollama │ Claude │        │
│    └───────────────────────────┘         │
└──────────────────────────────────────────┘
```

## Recommended Models for LM Studio

| Model | Size | Speed | Quality |
|-------|------|-------|---------|
| Llama 3.2 3B | 2GB | Fast | Good |
| Mistral 7B | 4GB | Medium | Great |
| Qwen 2.5 7B | 4GB | Medium | Great |
| Llama 3.1 8B | 5GB | Medium | Excellent |

For a Jarvis personality, Mistral or Qwen work great with the system prompt.

## Requirements

- Python 3.10+
- macOS (for TTS and Wispr Flow)
- One of:
  - LM Studio on Windows/Linux with GPU
  - Ollama locally
  - Anthropic API key

## Network Setup for LM Studio

If running LM Studio on a different machine:

1. **Windows Firewall:** Allow port 1234
2. **LM Studio Settings:** Enable "Serve on Local Network"
3. **Router:** Ensure both machines on same network

Test connection:
```bash
curl http://YOUR_WINDOWS_IP:1234/v1/models
```

## License

MIT
