#!/usr/bin/env bash
# stop-hook: auto-continue until state.md marks DONE.
# Input (stdin JSON): status, loop_count, transcript_path, workspace_roots, ...
# Output (stdout JSON): optional followup_message
set -euo pipefail

input=$(cat)

status=$(printf '%s' "$input" | jq -r '.status // empty')
loop_count=$(printf '%s' "$input" | jq -r '.loop_count // 0')
workspace_root=$(printf '%s' "$input" | jq -r '.workspace_roots[0] // empty')

if [[ -z "$workspace_root" ]]; then
  workspace_root="${CURSOR_PROJECT_DIR:-.}"
fi

state_file="${workspace_root}/state.md"

# Only auto-continue clean completions; respect abort/error.
if [[ "$status" != "completed" ]]; then
  printf '%s\n' '{}'
  exit 0
fi

# Detect DONE in state.md (status line or explicit marker).
is_done=0
if [[ -f "$state_file" ]]; then
  if grep -Eiq '^[[:space:]]*(status[[:space:]]*:[[:space:]]*)?DONE\b' "$state_file" \
    || grep -Eiq '^[[:space:]]*#{1,3}[[:space:]]*status[[:space:]]*:[[:space:]]*DONE\b' "$state_file"; then
    is_done=1
  fi
fi

if [[ "$is_done" -eq 1 ]]; then
  printf '%s\n' '{}'
  exit 0
fi

# Soft cap in addition to hooks.json loop_limit (defensive).
if [[ "$loop_count" -ge 24 ]]; then
  jq -n --arg msg "Stop-hook loop limit reached. Read state.md, fix blockers, set status: DONE when finished, then continue." \
    '{followup_message: $msg}'
  exit 0
fi

jq -n --arg msg "$(cat <<'EOF'
Продолжай. Не останавливайся, пока в state.md не будет status: DONE.

1. Сначала прочитай state.md и scratchpad.md.
2. На любой ошибке / failed render: diagnose → fix → re-run упавший шаг → обнови state.md.
3. Тяжёлый рендер (HyperFrames, Magnific и т.д.) — через GitHub Actions; агент готовит JSON/конфиги/скрипты.
4. Когда критерии успеха выполнены — выставь status: DONE в state.md и заверши ответом DONE.
EOF
)" '{followup_message: $msg}'
