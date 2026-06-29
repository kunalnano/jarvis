# Changelog

All notable changes to Yennefer AI Assistant will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.3] - 2026-06-29

### Added
- Explicit Chatterbox health state for the local Mac clone voice, including reference-audio checks, `/health` readiness, optional audio probe, and runtime status API.
- Voice status and Chatterbox reload controls in the local Yennefer chat UI.
- Automatic promotion back to Chatterbox when the local clone voice recovers after a degraded fallback.

### Changed
- Configured local Mac fallback order to prefer explicit ElevenLabs/11 fallback after Chatterbox, with visible warnings, before Kokoro or macOS fallback.
- Yennefer now waits for Chatterbox during local startup using the configured warm-up window before declaring voice degradation.

## [0.3.2] - 2026-01-03

### Added
- **Terminal demo mockup** - SVG screenshot showing Yennefer in action with ASCII banner, startup, and sample conversation
- **"Yennefer vs. The Cloud" comparison table** - Side-by-side vs ChatGPT, Claude API, Alexa/Siri
- **Dynamic GitHub badges** - Live last commit, stars, repo size, issues

### Changed
- **README overhaul** - Full MySpace energy 🔮
  - Animated waving gradient header via capsule-render
  - Typing SVG animation with rotating taglines
  - Animated purple dividers throughout
  - Skill icons for prerequisites section
  - Centered hero layout with for-the-badge style badges
  - Collapsible troubleshooting with `<details>` tags
  - Three-column personality examples
  - Visual roadmap tables with status indicators
  - Animated footer with star counter
- **Mermaid architecture diagram** - Color-coded flowchart with styled nodes replacing ASCII art
- Added NVIDIA Nemotron-Mini-4B to recommended models
- Remote setup now includes ASCII network diagram

## [0.3.1] - 2026-01-02

### Added
- **Environment variable support** - API keys now loaded from `.env` file
- `.env.example` template for easy setup
- `${VAR}` syntax expansion in YAML config

### Changed
- Removed hardcoded API keys from config files
- Updated README with proper setup instructions for open source usage
- All config files now use environment variable placeholders

### Security
- API keys no longer committed to repository
- Added `.env` to `.gitignore`

## [0.3.0] - 2026-01-02

### Added
- **ElevenLabs credits tracking** - Display subscription usage on startup with visual progress bar
- **Voice tuning parameters** - Configure stability, speed, similarity_boost, and style in config
- **New commands**:
  - `credits` - Refresh and display ElevenLabs character usage
  - `voice` - Show detailed voice settings and session stats
- **Session character tracking** - Track how many characters used in current session
- **Robust thinking tag stripping** - Handle `<think>`, `<thinking>`, unclosed tags, and missing opening tags

### Changed
- Upgraded speech cleaning to strip bullets, numbered lists, and headers before TTS
- Cleaned responses stored in conversation history (no thinking tokens wasting context)
- Default speech speed set to 1.15x for snappier responses
- Default stability set to 0.6 (adjustable for pitch consistency)

### Fixed
- Duplicate API key bug in config
- Thinking tags leaking into speech output when opening tag missing
- Markdown formatting being read aloud by TTS

## [0.2.1] - 2026-01-01

### Fixed
- ElevenLabs model deprecation error (`eleven_monolingual_v1` removed from free tier)
- Updated default model to `eleven_turbo_v2_5`

### Changed
- Renamed project from Jarvis to Yennefer
- Updated launchers: `start_yennefer.bat`, `start_yennefer.sh`

## [0.2.0] - 2025-12-31

### Added
- Token usage tracking with visual progress bar
- Context window management with automatic history trimming at 85% capacity
- `status` command to show detailed token breakdown
- `clear` command to reset conversation memory
- Conversation time remaining estimate

### Changed
- Improved Yennefer personality prompt for voice-optimized responses

## [0.1.0] - 2025-12-30

### Added
- Initial release
- Dual platform support (Windows + macOS)
- LM Studio backend integration (OpenAI-compatible API)
- ElevenLabs TTS with custom voice support
- macOS native TTS fallback
- Yennefer personality based on The Witcher character
- Basic conversation loop with text input
- Windows voice typing support (Win+H)

---

## Roadmap

### Planned for v0.4.0
- [ ] Dual-mode output (structured text + prose speech)
- [ ] Wake word detection
- [ ] Streaming TTS for faster response start
- [ ] Interrupt handling (stop speaking mid-sentence)

### Planned for v0.5.0
- [ ] Memory persistence across sessions
- [ ] Multi-voice support (switch characters)
- [ ] Plugin system for tools/actions
