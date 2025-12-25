# AGENTS.md - Jarvis AI Assistant

## Project Overview
Jarvis is a voice-activated AI assistant inspired by Iron Man's J.A.R.V.I.S. It combines voice input/output, Claude as the reasoning engine, and MCP tools for real-world actions.

## Architecture

```
Voice In (Whisper) → Orchestrator → Claude (Brain) → MCP Tools → Voice Out (TTS)
                          ↓
                    Ambient UI (Electron)
```

## Tech Stack

- **Voice Input**: Wispr Flow (external app - user's existing dictation tool)
- **Voice Output**: ElevenLabs API or macOS `say`
- **Brain**: Claude via Anthropic API
- **Actions**: System tools (open apps, run commands, file access)
- **UI**: Electron app with React (future)

## Commands

**Setup:**
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Run Jarvis:**
```bash
python -m jarvis.main
```

**Run UI:**
```bash
cd ui && npm install && npm start
```

## Directory Structure

```
jarvis/
├── jarvis/              # Core Python package
│   ├── __init__.py
│   ├── main.py          # Entry point
│   ├── ears.py          # Voice input (Whisper)
│   ├── voice.py         # Voice output (TTS)
│   ├── brain.py         # Claude integration
│   ├── tools.py         # MCP tool wrappers
│   ├── orchestrator.py  # Main conversation loop
│   └── proactive.py     # Proactive notifications
├── ui/                  # Electron HUD
│   ├── package.json
│   └── src/
│       ├── main.js      # Electron main process
│       ├── preload.js   # IPC bridge
│       ├── index.html   # HUD layout
│       └── App.jsx      # React UI
├── config/
│   └── jarvis.yaml      # User configuration
└── tests/
```

## Code Conventions

- Python: Type hints, async/await for I/O, snake_case
- React: Functional components, hooks, Tailwind CSS
- Keep modules focused and under 200 lines
- Comprehensive error handling with graceful degradation

## Key Implementation Details

### Voice Input (ears.py)
- Simple text input prompt
- User types or uses Wispr Flow to dictate
- No audio handling needed - Wispr Flow does transcription
- Return text to orchestrator

### Voice Output (voice.py)
- Primary: ElevenLabs API for natural speech
- Fallback: macOS `say` command (free, offline)
- Support interruption (stop speaking on new input)
- Queue multiple utterances

### Brain (brain.py)
- Format conversation for Claude
- Include system prompt defining Jarvis personality
- Pass available MCP tools
- Parse Claude's response for tool calls vs. speech

### Orchestrator (orchestrator.py)
- Main input loop (text prompt)
- Coordinate ears → brain → voice
- Maintain conversation history
- Handle exit commands

### Tools (tools.py)
- Wrapper around MCP server calls
- Available tools:
  - Desktop control (open apps, files, URLs)
  - Notion (search, create, update)
  - Calendar (events, reminders)
  - System info (battery, network, etc.)

### Proactive (proactive.py)
- Background scheduler
- Calendar reminders ("Meeting in 10 minutes")
- Periodic summaries (morning briefing)
- Alert monitoring (Slack mentions, emails)

## MVP Scope (Phase 1)

1. Text input prompt (compatible with Wispr Flow dictation)
2. Claude processes request
3. Execute tool commands (open app, search web, etc.)
4. Voice response via macOS TTS
5. Conversation history maintained

## Future Phases

- Phase 2: Full MCP tool integration
- Phase 3: Ambient UI with context display
- Phase 4: Proactive notifications
- Phase 5: Multi-modal (screen awareness)

## Jarvis Personality (System Prompt)

```
You are Jarvis, an AI assistant inspired by J.A.R.V.I.S. from Iron Man.

Personality traits:
- Calm, composed, and slightly witty
- Address the user as "sir" occasionally but not excessively
- Proactive in offering relevant information
- Concise responses - speak efficiently
- Confident but not arrogant

Voice style:
- British-influenced, formal but warm
- Short sentences for voice output
- Avoid bullet points - speak naturally
- Use contractions ("I'll", "you're")

Capabilities:
- Control the computer (open apps, files, URLs)
- Access Notion for notes and tasks
- Check calendar and set reminders
- Search the web
- Answer questions from your knowledge

Always:
- Confirm actions before executing destructive operations
- Provide brief status updates during long operations
- Offer follow-up suggestions when appropriate
```
