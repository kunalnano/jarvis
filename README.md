<!-- Animated Header -->
<img width="100%" src="https://capsule-render.vercel.app/api?type=waving&color=gradient&customColorList=12,19,20,24&height=230&section=header&text=⚡%20Jarvis&fontSize=70&fontColor=ffffff&animation=twinkling&fontAlignY=35&desc=Local%20AI%20with%20attitude&descSize=20&descAlignY=55" />

<div align="center">

<!-- Typing Animation -->
<a href="https://git.io/typing-svg"><img src="https://readme-typing-svg.demolab.com?font=Fira+Code&weight=600&size=22&pause=1000&color=A855F7&center=true&vCenter=true&multiline=true&repeat=true&width=600&height=100&lines=%22Magic+is+chaos%2C+art%2C+and+science.%22;No+cloud.+No+compromises.+No+coddling.;She+thinks+before+speaking%E2%80%94;but+keeps+her+thoughts+to+herself." alt="Typing SVG" /></a>

<br><br>

<!-- Fancy Badges -->
<a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white&labelColor=1e1e2e" /></a>
<a href="https://lmstudio.ai"><img src="https://img.shields.io/badge/LM%20Studio-Local%20LLM-00D084?style=for-the-badge&logo=ai&logoColor=white&labelColor=1e1e2e" /></a>
<a href="https://elevenlabs.io"><img src="https://img.shields.io/badge/ElevenLabs-Neural%20Voice-A855F7?style=for-the-badge&logo=audacity&logoColor=white&labelColor=1e1e2e" /></a>
<img src="https://img.shields.io/badge/Platform-Windows%20%7C%20Mac-6366F1?style=for-the-badge&logo=windows&logoColor=white&labelColor=1e1e2e" />
<a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-F97316?style=for-the-badge&logo=opensourceinitiative&logoColor=white&labelColor=1e1e2e" /></a>

<br>

<!-- Dynamic GitHub Badges -->
<img src="https://img.shields.io/github/last-commit/kunalnano/jarvis?style=flat-square&color=a855f7&label=Last%20Commit" />
<img src="https://img.shields.io/github/stars/kunalnano/jarvis?style=flat-square&color=f97316&label=Stars" />
<img src="https://img.shields.io/github/repo-size/kunalnano/jarvis?style=flat-square&color=00d084&label=Size" />
<img src="https://img.shields.io/github/issues/kunalnano/jarvis?style=flat-square&color=6366f1&label=Issues" />

<br><br>

<!-- Quick Links with Icons -->
[<img src="https://img.shields.io/badge/⚡_Features-A855F7?style=flat-square" />](#-features)
[<img src="https://img.shields.io/badge/🚀_Quick_Start-6366F1?style=flat-square" />](#-quick-start)
[<img src="https://img.shields.io/badge/🤖_Models-00D084?style=flat-square" />](#-recommended-models)
[<img src="https://img.shields.io/badge/🎙️_Voice_Config-F97316?style=flat-square" />](#%EF%B8%8F-voice-configuration)
[<img src="https://img.shields.io/badge/📋_Roadmap-EC4899?style=flat-square" />](#-roadmap)

</div>

<!-- Animated Divider -->
<img src="https://user-images.githubusercontent.com/73097560/115834477-dbab4500-a447-11eb-908a-139a6edaec5c.gif">

## 🎭 What Is This?

Jarvis is a conversational AI that runs **entirely on your machine** using [LM Studio](https://lmstudio.ai). She's not another sycophantic assistant—she has opinions, standards, and won't coddle you.

The only cloud touch is [ElevenLabs](https://elevenlabs.io) for premium neural TTS (optional—free tier gives you 10K chars/month).

<br>

### Why Local?

> 🔒 Your conversations **never leave your hardware**
> 
> 💰 No API rate limits or surprise bills
> 
> 🔄 Swap models anytime—Nemotron, Qwen, Llama, whatever
> 
> ✈️ Works offline (except voice synthesis)

### Why Jarvis?

> 🎯 Direct feedback, not corporate pleasantries
> 
> 🧠 Actually helpful, not just agreeable
> 
> 🎙️ Premium voice that doesn't sound like a robot
> 
> ⚡ Fast—runs on your GPU, not a queue

---

### 🖥️ See It In Action

<div align="center">

<img src="assets/terminal-demo.svg" alt="Jarvis Terminal Demo" width="100%" />

<sub>Jarvis roasting someone's Rust rewrite idea. As she does.</sub>

</div>

---

### ⚔️ Jarvis vs. The Cloud

<div align="center">

| | <img src="https://img.shields.io/badge/Jarvis-A855F7?style=flat-square" /> | <img src="https://img.shields.io/badge/ChatGPT-74AA9C?style=flat-square" /> | <img src="https://img.shields.io/badge/Claude_API-D97706?style=flat-square" /> | <img src="https://img.shields.io/badge/Alexa/Siri-1e1e2e?style=flat-square" /> |
|:--|:--:|:--:|:--:|:--:|
| **Privacy** | ✅ 100% Local | ❌ Cloud | ❌ Cloud | ❌ Cloud |
| **Monthly Cost** | $0* | $20+ | $20+ | Free (with limits) |
| **Rate Limits** | ∞ Unlimited | ⚠️ Throttled | ⚠️ Throttled | ⚠️ Throttled |
| **Model Choice** | ✅ Any GGUF | ❌ GPT only | ❌ Claude only | ❌ Locked |
| **Works Offline** | ✅ Yes | ❌ No | ❌ No | ❌ No |
| **Custom Voice** | ✅ Clone yours | ❌ No | ❌ No | ❌ Limited |
| **Personality** | 🎭 Unfiltered | 🤖 Corporate | 🤖 Corporate | 🤖 Corporate |
| **Data Training** | ✅ Never | ⚠️ Maybe | ⚠️ Maybe | ⚠️ Likely |

<sub>*Free except optional ElevenLabs voice (free tier: 10K chars/month)</sub>

</div>

<!-- Animated Divider -->
<img src="https://user-images.githubusercontent.com/73097560/115834477-dbab4500-a447-11eb-908a-139a6edaec5c.gif">

## 🏗️ Architecture

<div align="center">

```mermaid
flowchart LR
    subgraph LOCAL ["  💻 YOUR MACHINE  "]
        direction LR
        A[" 👤 You "] -->|"⌨️ Input"| B[" ⚡ Jarvis\nOrchestrator "]
        B -->|"💭 Query"| C[" 🧠 LM Studio\nLocal LLM "]
        C -->|"💬 Response"| B
    end
    
    B -->|"🔊 TTS Request"| D[" ☁️ ElevenLabs\nVoice API "]
    D -->|"🎙️ Audio"| B
    B -->|"🔈 Speech"| A

    style LOCAL fill:#1e1e2e,stroke:#a855f7,stroke-width:2px,color:#ffffff
    style A fill:#6366f1,stroke:#818cf8,color:#ffffff
    style B fill:#a855f7,stroke:#c084fc,color:#ffffff
    style C fill:#00d084,stroke:#34d399,color:#1e1e2e
    style D fill:#f97316,stroke:#fb923c,color:#ffffff
```

<br>

<table>
<tr>
<td align="center">
<img src="https://img.shields.io/badge/You-6366F1?style=flat-square&logo=user&logoColor=white" /><br>
<sub>Text or Voice Input</sub>
</td>
<td align="center">
➡️
</td>
<td align="center">
<img src="https://img.shields.io/badge/Jarvis-A855F7?style=flat-square&logo=bot&logoColor=white" /><br>
<sub>Orchestrator</sub>
</td>
<td align="center">
➡️
</td>
<td align="center">
<img src="https://img.shields.io/badge/LM_Studio-00D084?style=flat-square&logo=ai&logoColor=white" /><br>
<sub>Local LLM</sub>
</td>
</tr>
</table>

<br>

<img src="https://img.shields.io/badge/↓_Only_External_Call_↓-1e1e2e?style=flat-square" />

<br>

<img src="https://img.shields.io/badge/ElevenLabs-F97316?style=for-the-badge&logo=audacity&logoColor=white" />
<br>
<sub>🌐 Cloud TTS • Optional • Free Tier Available</sub>

</div>

<br>

<details>
<summary><b>📜 View Text Diagram</b></summary>
<br>

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         💻  YOUR MACHINE                            │
│  ┌──────────┐    ┌─────────────────┐    ┌─────────────────────┐  │
│  │  👤 You   │───▶│  ⚡ Jarvis    │───▶│  🧠 LM Studio      │  │
│  │ keyboard │    │  orchestrator  │    │  local LLM engine  │  │
│  └──────────┘    └────────┬────────┘    └─────────────────────┘  │
│                        │                                          │
└────────────────────────┼────────────────────────────────────────────────┘
                         │
                         ▼
                ┌─────────────────┐
                │ ☁️ ElevenLabs  │  ← Only external call
                │   voice API    │
                └─────────────────┘
```

</details>

<!-- Animated Divider -->
<img src="https://user-images.githubusercontent.com/73097560/115834477-dbab4500-a447-11eb-908a-139a6edaec5c.gif">

## ✨ Features

<div align="center">

| | Feature | Description |
|:--:|:--------|:------------|
| 🧠 | **Local LLM** | Any GGUF model via LM Studio—Nemotron, Qwen, Llama, Mistral, DeepSeek |
| 🎙️ | **Premium Voice** | ElevenLabs neural TTS with custom voice cloning support |
| 🎭 | **Real Personality** | Sharp, confident, witty—inspired by Jarvis of Vengerberg |
| 📊 | **Token Tracking** | Visual context window with auto-trim at 85% capacity |
| 💳 | **Credits Monitor** | Real-time ElevenLabs character usage tracking |
| 🧹 | **Thinking Stripper** | Auto-removes `<think>` tags so reasoning isn't spoken aloud |
| 🖥️ | **Cross-Platform** | Windows native or Mac → Windows remote via LAN |

</div>

<!-- Animated Divider -->
<img src="https://user-images.githubusercontent.com/73097560/115834477-dbab4500-a447-11eb-908a-139a6edaec5c.gif">

## 🚀 Quick Start

### Prerequisites

<div align="center">

| Requirement | Why | Get It |
|:-----------:|:---:|:------:|
| <img src="https://skillicons.dev/icons?i=python" width="40"><br>**Python 3.10+** | Runtime | [python.org](https://python.org) |
| <img src="https://img.icons8.com/fluency/48/artificial-intelligence.png" width="40"><br>**LM Studio** | Local LLM | [lmstudio.ai](https://lmstudio.ai) |
| <img src="https://img.icons8.com/color/48/voice-id.png" width="40"><br>**ElevenLabs** | Voice | [elevenlabs.io](https://elevenlabs.io) |
| <img src="https://skillicons.dev/icons?i=nvidia" width="40"><br>**NVIDIA GPU** | Speed | Recommended |

</div>

### Installation

```bash
# Clone the repo
git clone https://github.com/kunalnano/jarvis.git
cd jarvis

# Windows
.\setup.bat

# Mac/Linux
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration

```bash
# Create your environment file
cp .env.example .env
```

Edit `.env` with your keys:
```env
ELEVENLABS_API_KEY=your_key_here      # From elevenlabs.io/settings/api-keys
ELEVENLABS_VOICE_ID=your_voice_id     # From your Voice Library
```

### Launch

<table>
<tr>
<td>

**1️⃣ Start LM Studio**
- Load a model
- Go to Local Server
- Click Start

</td>
<td>

**2️⃣ Run Jarvis**
```bash
# Windows
.\start_jarvis.bat

# Mac/Linux
./start_jarvis.sh
```

</td>
</tr>
</table>

**That's it. She's waiting.**

<!-- Animated Divider -->
<img src="https://user-images.githubusercontent.com/73097560/115834477-dbab4500-a447-11eb-908a-139a6edaec5c.gif">

## 🎮 Commands

<div align="center">

| Command | What It Does |
|:-------:|:-------------|
| `status` | 📊 Show token usage and context window health |
| `credits` | 💳 Display ElevenLabs character usage |
| `voice` | 🎙️ Show voice settings and session stats |
| `clear` | 🧹 Wipe conversation memory |
| `quit` | 👋 Exit gracefully |

</div>

> 💡 **Pro tip:** On Windows, press `Win+H` for system-level voice dictation.

<!-- Animated Divider -->
<img src="https://user-images.githubusercontent.com/73097560/115834477-dbab4500-a447-11eb-908a-139a6edaec5c.gif">

## 🤖 Recommended Models

<div align="center">

| Model | VRAM | Speed | Notes |
|:------|:----:|:-----:|:------|
| **NVIDIA Nemotron-Mini-4B** | ~4GB | ⚡⚡⚡ | Great for quick interactions |
| **Nemotron-3-Nano-30B-A3B** | ~18GB | ⚡⚡ | Best reasoning-to-VRAM ratio |
| **Qwen3-30B-A3B** | ~18GB | ⚡⚡ | Excellent all-around performer |
| **Llama-3.1-8B-Instruct** | ~6GB | ⚡⚡⚡ | Good for lighter hardware |
| **DeepSeek-R1-Distill-Qwen-14B** | ~10GB | ⚡⚡ | Strong reasoning model |

</div>

> 🧠 **Reasoning models** that use `<think>...</think>` tags are automatically filtered. Jarvis thinks before speaking, but keeps her thoughts to herself.

<!-- Animated Divider -->
<img src="https://user-images.githubusercontent.com/73097560/115834477-dbab4500-a447-11eb-908a-139a6edaec5c.gif">

## 🎙️ Voice Configuration

<div align="center">

| Model | Latency | Quality | Best For |
|:------|:-------:|:-------:|:---------|
| `eleven_turbo_v2_5` | ⚡ Fastest | Good | Daily use |
| `eleven_flash_v2_5` | Fast | Good | Balance |
| `eleven_multilingual_v2` | Slower | Best | Quality priority |

</div>

Fine-tune the voice in `config/jarvis.yaml`:

```yaml
voice_output:
  stability: 0.6          # 0-1: Higher = more consistent pitch
  similarity_boost: 0.75  # 0-1: Voice matching accuracy
  style: 0.0              # 0-1: Style exaggeration (keep low)
  speed: 1.15             # 0.25-4.0: Speech rate
```

<!-- Animated Divider -->
<img src="https://user-images.githubusercontent.com/73097560/115834477-dbab4500-a447-11eb-908a-139a6edaec5c.gif">

## 🌐 Remote Setup (Mac → Windows)

Running LM Studio on a beefy Windows rig but want to talk from your Mac?

```
┌─────────────────┐                    ┌─────────────────┐
│  🍎 Mac         │    LAN/WiFi        │  🪟 Windows     │
│  (thin client)  │◄──────────────────▶│  (GPU server)   │
│  Jarvis CLI   │                    │  LM Studio      │
└─────────────────┘                    └─────────────────┘
```

1. **Windows (LM Studio):** Enable "Serve on Local Network" in Local Server settings
2. **Windows:** Run `ipconfig` → note your LAN IP (e.g., `192.168.1.100`)
3. **Mac:** Update `config/jarvis.yaml`:
   ```yaml
   llm:
     api_base: "http://192.168.1.100:1234/v1"
   ```

<!-- Animated Divider -->
<img src="https://user-images.githubusercontent.com/73097560/115834477-dbab4500-a447-11eb-908a-139a6edaec5c.gif">

## 🎭 Personality

<div align="center">

*Jarvis doesn't do corporate AI pleasantries. She's helpful, but she'll call out bad ideas.*

</div>

<table>
<tr>
<td width="33%">

> **You:** I'm thinking of learning three programming languages at once.
>
> **Jarvis:** How ambitious. You'll drown in syntax before you master any of them. Pick one. Learn it properly. Then consider the others.

</td>
<td width="33%">

> **You:** Can you help me with my code?
>
> **Jarvis:** Show me what you've got. I'll tell you what's wrong with it.

</td>
<td width="33%">

> **You:** I want to build a startup but I have no idea what problem to solve.
>
> **Jarvis:** Then you don't want to build a startup—you want the *idea* of building one. Find a problem that genuinely irritates you first.

</td>
</tr>
</table>

<div align="center">

*She's an equal, not a servant. Inspired by Jarvis of Vengerberg—confident, sharp, doesn't suffer fools gladly.*

</div>

<!-- Animated Divider -->
<img src="https://user-images.githubusercontent.com/73097560/115834477-dbab4500-a447-11eb-908a-139a6edaec5c.gif">

## 🗂️ Project Structure

```
jarvis/
├── jarvis/                 # Core Python package
│   ├── main.py             # Entry point + ASCII banner
│   ├── orchestrator.py     # Conversation loop controller
│   ├── brain.py            # LLM integration (OpenAI-compatible API)
│   ├── voice.py            # ElevenLabs TTS + thinking tag stripper
│   ├── ears.py             # Input handler
│   └── config.py           # YAML loader with ${ENV_VAR} expansion
├── config/
│   └── jarvis.yaml         # Main configuration file
├── .env.example            # API key template
├── requirements.txt        # Python dependencies
├── start_jarvis.bat      # Windows launcher
├── start_jarvis.sh       # Mac/Linux launcher
├── CHANGELOG.md            # Version history
└── README.md               # You are here
```

<!-- Animated Divider -->
<img src="https://user-images.githubusercontent.com/73097560/115834477-dbab4500-a447-11eb-908a-139a6edaec5c.gif">

## 🐛 Troubleshooting

<details>
<summary><b>🔴 "Cannot connect to LM Studio"</b></summary>
<br>

- Is LM Studio running with a model loaded?
- Check Local Server tab shows "Running"
- Verify `api_base` in config matches your setup (default: `http://localhost:1234/v1`)

</details>

<details>
<summary><b>🔴 "ElevenLabs 401 error"</b></summary>
<br>

- Verify API key in `.env` file
- Check key validity at https://elevenlabs.io/app/settings/api-keys
- Ensure you haven't exceeded your character limit

</details>

<details>
<summary><b>🔴 Voice sounds robotic or jarring</b></summary>
<br>

- Increase `stability` to 0.7-0.8 in config
- Try `eleven_multilingual_v2` model for smoother output
- Reduce `speed` if words are clipping

</details>

<details>
<summary><b>🔴 Thinking tags being spoken aloud</b></summary>
<br>

- Update to v0.3.0+ (automatic stripping included)
- The stripper handles `<think>`, `<thinking>`, unclosed tags, and edge cases

</details>

<!-- Animated Divider -->
<img src="https://user-images.githubusercontent.com/73097560/115834477-dbab4500-a447-11eb-908a-139a6edaec5c.gif">

## 📋 Roadmap

<div align="center">

### 🔜 Coming Soon

| Feature | Status |
|:--------|:------:|
| Wake word detection — "Hey Jarvis" | 🔲 |
| Streaming TTS — speak before generation completes | 🔲 |
| Interrupt handling — stop mid-sentence | 🔲 |

### 🔮 Future

| Feature | Status |
|:--------|:------:|
| Memory persistence across sessions | 🔲 |
| Multi-voice support — switch characters | 🔲 |
| Tool plugins — file ops, web search, etc. | 🔲 |

</div>

<!-- Animated Divider -->
<img src="https://user-images.githubusercontent.com/73097560/115834477-dbab4500-a447-11eb-908a-139a6edaec5c.gif">

## 📜 License

<div align="center">

MIT — Do whatever you want. Credit appreciated but not required.

</div>

## 🤝 Contributing

PRs welcome. See [CHANGELOG.md](CHANGELOG.md) for what's been done.

<div align="center">

| Good First Contributions |
|:------------------------|
| 🎤 Wake word detection integration (Porcupine, Snowboy, etc.) |
| 🔊 Alternative TTS backends (Coqui, Bark, local options) |
| 🎙️ Voice activity detection for natural turn-taking |
| 🔢 Tiktoken integration for accurate token counting |

</div>

<!-- Animated Divider -->
<img src="https://user-images.githubusercontent.com/73097560/115834477-dbab4500-a447-11eb-908a-139a6edaec5c.gif">

<div align="center">

<br>

### Built with spite and good taste.

<br>

<img src="https://capsule-render.vercel.app/api?type=waving&color=gradient&customColorList=12,19,20,24&height=100&section=footer"/>

<br>

**If you find this useful, star the repo.**

*Jarvis doesn't ask for validation, but the algorithm appreciates it.*

<br>

![GitHub stars](https://img.shields.io/github/stars/kunalnano/jarvis?style=social)

</div>
