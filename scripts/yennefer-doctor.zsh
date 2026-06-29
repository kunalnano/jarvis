#!/usr/bin/env zsh
set -uo pipefail
setopt typeset_silent

ROOT="${YENNEFER_ROOT:-/Users/alsharma/Projects/yennefer}"
OVERLAY_APP="${YENNEFER_OVERLAY_APP:-/Users/alsharma/Projects/yennefer-overlay/build/Build/Products/Debug/YenneferOverlay.app}"
ENV_FILE="${YENNEFER_ENV_FILE:-$ROOT/.env}"
UID_VALUE="$(id -u)"
REPAIR=false

for arg in "$@"; do
  case "$arg" in
    --repair) REPAIR=true ;;
    --check) REPAIR=false ;;
    -h|--help)
      print "usage: scripts/yennefer-doctor.zsh [--check|--repair]"
      exit 0
      ;;
  esac
done

say_line() { print -- "$1"; }
ok() { say_line "OK    $1"; }
warn() { say_line "WARN  $1"; }
act() { say_line "ACT   $1"; }
fail() { say_line "FAIL  $1"; }

clean_value() {
  local value="${1:-}"
  value="${value#\"}"
  value="${value%\"}"
  value="${value#\'}"
  value="${value%\'}"
  [[ -n "$value" && "$value" != \${*} && "$value" != "your_"* ]] && print -- "$value"
}

load_env() {
  [[ -f "$ENV_FILE" ]] || return 0
  while IFS='=' read -r key value; do
    key="${key%%[[:space:]]*}"
    [[ -z "$key" || "$key" == \#* || "$key" != [A-Za-z_]* ]] && continue
    case "$key" in
      ELEVENLABS_API_KEY|ELEVENLABS_VOICE_ID|SERPER_API_KEY|LM_API_TOKEN|LM_STUDIO_API_KEY|LM_API_KEY|LM_STUDIO_API_BASE|LM_STUDIO_MODEL|STORMBREAKER_LM_STUDIO_API_BASE|STORMBREAKER_LM_STUDIO_API_KEY|STORMBREAKER_LM_STUDIO_MODEL|WINDOWS_LM_STUDIO_API_BASE|WINDOWS_LM_STUDIO_API_KEY|WINDOWS_LM_STUDIO_MODEL)
        local env_value
        env_value="$(clean_value "$value")"
        [[ -n "$env_value" ]] && export "${key}=${env_value}" >/dev/null
        ;;
    esac
  done < "$ENV_FILE"
}

http_status() {
  local url="$1"
  local token="${2:-}"
  local args=(-sS -o /dev/null -w "%{http_code}" --max-time 5)
  [[ -n "$token" ]] && args+=(-H "Authorization: Bearer $token")
  local code
  code="$(curl "${args[@]}" "$url" 2>/dev/null)" || code="000"
  print -- "$code"
}

api_base_models_url() {
  local base="${1%/}"
  print -- "$base/models"
}

check_lmstudio() {
  local name="$1"
  local base="$2"
  local token="${3:-}"
  local code
  code="$(http_status "$(api_base_models_url "$base")" "$token")"
  case "$code" in
    200)
      ok "$name LM Studio reachable at $base"
      return 0
      ;;
    401|403)
      warn "$name LM Studio reachable but requires a valid API token ($code)"
      return 2
      ;;
    000)
      warn "$name LM Studio not reachable at $base"
      return 1
      ;;
    *)
      warn "$name LM Studio returned HTTP $code at $base"
      return 1
      ;;
  esac
}

kickstart_agent() {
  local label="$1"
  local plist="$2"
  if launchctl print "gui/$UID_VALUE/$label" >/dev/null 2>&1; then
    act "kickstarting $label"
    launchctl kickstart -k "gui/$UID_VALUE/$label" >/dev/null 2>&1 || warn "kickstart failed for $label"
  elif [[ -f "$plist" ]]; then
    act "bootstrapping $label"
    launchctl bootstrap "gui/$UID_VALUE" "$plist" >/dev/null 2>&1 || warn "bootstrap failed for $label"
    launchctl kickstart -k "gui/$UID_VALUE/$label" >/dev/null 2>&1 || true
  else
    warn "missing LaunchAgent plist for $label at $plist"
  fi
}

check_url() {
  local name="$1"
  local url="$2"
  local code
  code="$(http_status "$url")"
  if [[ "$code" == "200" ]]; then
    ok "$name ready"
    return 0
  fi
  warn "$name not ready (HTTP $code)"
  return 1
}

load_env >/dev/null

say_line "Yennefer doctor ($(date '+%Y-%m-%d %H:%M:%S'))"
say_line "mode: $([[ "$REPAIR" == true ]] && print repair || print check)"

if ! check_url "Yennefer backend" "http://127.0.0.1:4343/api/commands"; then
  if [[ "$REPAIR" == true ]]; then
    kickstart_agent "ai.darkvector.yennefer-server" "$HOME/Library/LaunchAgents/ai.darkvector.yennefer-server.plist"
    sleep 1
    check_url "Yennefer backend" "http://127.0.0.1:4343/api/commands" || true
  fi
fi

if ! check_url "Chatterbox voice" "http://127.0.0.1:8004/health"; then
  if [[ "$REPAIR" == true ]]; then
    kickstart_agent "ai.darkvector.yennefer-chatterbox" "$HOME/Library/LaunchAgents/ai.darkvector.yennefer-chatterbox.plist"
    sleep 2
    check_url "Chatterbox voice" "http://127.0.0.1:8004/health" || true
  fi
fi

if pgrep -fx "$OVERLAY_APP/Contents/MacOS/YenneferOverlay" >/dev/null 2>&1 || pgrep -f "YenneferOverlay.app/Contents/MacOS/YenneferOverlay" >/dev/null 2>&1; then
  ok "Yennefer overlay process running"
else
  warn "Yennefer overlay process not running"
  if [[ "$REPAIR" == true ]]; then
    if [[ -d "$OVERLAY_APP" ]]; then
      act "opening Yennefer overlay"
      open "$OVERLAY_APP"
    else
      warn "overlay app bundle not found at $OVERLAY_APP"
    fi
  fi
fi

local_base="${LM_STUDIO_API_BASE:-http://127.0.0.1:1234/v1}"
local_token="${LM_API_TOKEN:-${LM_STUDIO_API_KEY:-${LM_API_KEY:-}}}"
windows_base="${WINDOWS_LM_STUDIO_API_BASE:-${STORMBREAKER_LM_STUDIO_API_BASE:-http://192.168.4.48:1234/v1}}"
windows_token="${WINDOWS_LM_STUDIO_API_KEY:-${STORMBREAKER_LM_STUDIO_API_KEY:-}}"

if check_lmstudio "Prometheus" "$local_base" "$local_token"; then
  ok "LLM route: Prometheus first"
else
  local_status=$?
  if [[ "$REPAIR" == true && "$local_status" == "1" ]]; then
    act "opening LM Studio on Prometheus"
    open -a "LM Studio" >/dev/null 2>&1 || warn "could not open LM Studio"
    sleep 2
    check_lmstudio "Prometheus" "$local_base" "$local_token" || true
  fi
  if check_lmstudio "Stormbreaker/Windows" "$windows_base" "$windows_token"; then
    ok "LLM route: Stormbreaker fallback"
  else
    fail "no LM Studio endpoint is ready for freeform chat"
    warn "For auth failures, create/load LM_API_TOKEN or STORMBREAKER_LM_STUDIO_API_KEY; token values were not printed."
    warn "Credential helper: $ROOT/scripts/set-lm-studio-key.zsh"
  fi
fi
