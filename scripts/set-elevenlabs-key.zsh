#!/usr/bin/env zsh
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "$0")/.." && pwd)"
env_file="$repo_dir/.env"
voice_id_default="QPYZDsvgGiT4CMQghb53"
label="ai.darkvector.yennefer-server"

if [[ ! -d "$repo_dir/jarvis" ]]; then
  print -u2 "Could not find Yennefer repo root from $0"
  exit 1
fi

print -n "ElevenLabs API key: " > /dev/tty
stty -echo < /dev/tty
IFS= read -r key < /dev/tty
stty echo < /dev/tty
print "" > /dev/tty

if [[ -z "$key" ]]; then
  print -u2 "No key entered; leaving $env_file unchanged."
  exit 1
fi

umask 077
touch "$env_file"

voice_id="${ELEVENLABS_VOICE_ID:-}"
if [[ -z "$voice_id" ]]; then
  existing_voice_id="$(grep -E '^ELEVENLABS_VOICE_ID=' "$env_file" 2>/dev/null | tail -n 1 | sed 's/^ELEVENLABS_VOICE_ID=//')"
  voice_id="${existing_voice_id:-$voice_id_default}"
fi

tmp="$(mktemp "$env_file.tmp.XXXXXX")"
found_key=0
found_voice=0

while IFS= read -r line || [[ -n "$line" ]]; do
  case "$line" in
    ELEVENLABS_API_KEY=*)
      print -r -- "ELEVENLABS_API_KEY=$key" >> "$tmp"
      found_key=1
      ;;
    ELEVENLABS_VOICE_ID=*)
      print -r -- "ELEVENLABS_VOICE_ID=$voice_id" >> "$tmp"
      found_voice=1
      ;;
    *)
      print -r -- "$line" >> "$tmp"
      ;;
  esac
done < "$env_file"

if [[ "$found_key" -eq 0 ]]; then
  print -r -- "ELEVENLABS_API_KEY=$key" >> "$tmp"
fi

if [[ "$found_voice" -eq 0 ]]; then
  print -r -- "ELEVENLABS_VOICE_ID=$voice_id" >> "$tmp"
fi

mv "$tmp" "$env_file"
chmod 600 "$env_file"

launchctl kickstart -k "gui/$(id -u)/$label" >/dev/null 2>&1 || true

(
  cd "$repo_dir"
  "${repo_dir}/.venv/bin/python" - <<'PY'
from jarvis.config import load_config

voice = load_config().get("voice_output", {})
print("engine:", voice.get("engine"))
print("voice_id_expected:", voice.get("voice_id") == "QPYZDsvgGiT4CMQghb53")
print("api_key_present:", bool(voice.get("api_key")))
print("allow_macos_fallback:", voice.get("allow_macos_fallback"))
PY
)
