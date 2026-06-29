#!/usr/bin/env zsh
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "$0")/.." && pwd)"
env_file="$repo_dir/.env"
label="ai.darkvector.yennefer-server"
default_windows_base="http://192.168.4.48:1234/v1"

if [[ ! -d "$repo_dir/jarvis" ]]; then
  print -u2 "Could not find Yennefer repo root from $0"
  exit 1
fi

read_secret() {
  local prompt="$1"
  local value
  print -n "$prompt" > /dev/tty
  stty -echo < /dev/tty
  IFS= read -r value < /dev/tty
  stty echo < /dev/tty
  print "" > /dev/tty
  print -r -- "$value"
}

print "Leave a field blank to keep the existing value." > /dev/tty
mac_key="$(read_secret "Prometheus LM Studio API key: ")"
windows_key="$(read_secret "Stormbreaker/Windows LM Studio API key: ")"

print -n "Stormbreaker/Windows LM Studio API base [$default_windows_base]: " > /dev/tty
IFS= read -r windows_base < /dev/tty
windows_base="${windows_base:-$default_windows_base}"

if [[ -z "$mac_key" && -z "$windows_key" ]]; then
  print -u2 "No LM Studio keys entered; leaving $env_file unchanged."
  exit 1
fi

umask 077
touch "$env_file"

tmp="$(mktemp "$env_file.tmp.XXXXXX")"
found_mac_key=0
found_windows_key=0
found_windows_base=0

while IFS= read -r line || [[ -n "$line" ]]; do
  case "$line" in
    LM_API_TOKEN=*)
      if [[ -n "$mac_key" ]]; then
        print -r -- "LM_API_TOKEN=$mac_key" >> "$tmp"
      else
        print -r -- "$line" >> "$tmp"
      fi
      found_mac_key=1
      ;;
    STORMBREAKER_LM_STUDIO_API_KEY=*|WINDOWS_LM_STUDIO_API_KEY=*)
      if [[ -n "$windows_key" ]]; then
        print -r -- "STORMBREAKER_LM_STUDIO_API_KEY=$windows_key" >> "$tmp"
      else
        print -r -- "$line" >> "$tmp"
      fi
      found_windows_key=1
      ;;
    STORMBREAKER_LM_STUDIO_API_BASE=*|WINDOWS_LM_STUDIO_API_BASE=*)
      print -r -- "STORMBREAKER_LM_STUDIO_API_BASE=$windows_base" >> "$tmp"
      found_windows_base=1
      ;;
    *)
      print -r -- "$line" >> "$tmp"
      ;;
  esac
done < "$env_file"

if [[ "$found_mac_key" -eq 0 && -n "$mac_key" ]]; then
  print -r -- "LM_API_TOKEN=$mac_key" >> "$tmp"
fi

if [[ "$found_windows_key" -eq 0 && -n "$windows_key" ]]; then
  print -r -- "STORMBREAKER_LM_STUDIO_API_KEY=$windows_key" >> "$tmp"
fi

if [[ "$found_windows_base" -eq 0 ]]; then
  print -r -- "STORMBREAKER_LM_STUDIO_API_BASE=$windows_base" >> "$tmp"
fi

mv "$tmp" "$env_file"
chmod 600 "$env_file"

launchctl kickstart -k "gui/$(id -u)/$label" >/dev/null 2>&1 || true

"$repo_dir/scripts/yennefer-doctor.zsh" --check
