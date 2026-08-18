#!/bin/bash
# auto-restore-on-boot.sh - Auto-restore tmux sessions after system reboot
# Called by openclaw-claw-restore.service after all backends are up
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
SNAPSHOT_FILE="${SKILL_DIR}/data/session-snapshot.json"
SOCKET_DIR="/root/clawdbot-tmux-sockets"
RESTORE_SCRIPT="${SCRIPT_DIR}/restore-sessions.sh"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo "[$(date -Iseconds)] $*"; }

BACKENDS=("18084" "18085" "18086")

backend_for_socket() {
    local socket="$1"
    case "$socket" in
        *claude-code.sock) echo "18084" ;;
        *codex-code.sock) echo "18085" ;;
        *default-code.sock) echo "18086" ;;
        *) echo "18086" ;;
    esac
}

# Step 1: Wait for all backends to be healthy
MAX_WAIT=120
log "Waiting for all backends to be healthy..."
for port in "${BACKENDS[@]}"; do
    local_wait=0
    while ! curl -sf "http://172.19.0.1:${port}/api/data" -o /dev/null 2>/dev/null; do
        sleep 2
        local_wait=$((local_wait + 2))
        if [[ $local_wait -ge $MAX_WAIT ]]; then
            log "ERROR: Backend on port $port not healthy after ${MAX_WAIT}s, skipping"
            break
        fi
    done
    if [[ $local_wait -lt $MAX_WAIT ]]; then
        log "Backend on port $port is healthy"
    fi
done

# Step 2: Ensure socket directory exists
mkdir -p "$SOCKET_DIR"

# Step 3: Restore from snapshot
if [[ ! -f "$SNAPSHOT_FILE" ]]; then
    log "No snapshot file found at $SNAPSHOT_FILE"
    log "Falling back to restore-sessions.sh --all-lost"
    if bash "$RESTORE_SCRIPT" --all-lost 2>&1; then
        log "Fallback restore completed"
        exit 0
    fi
    log "Fallback restore failed"
    exit 1
fi

SESSION_COUNT=$(python3 -c "import json; print(json.load(open('${SNAPSHOT_FILE}')).get('session_count', 0))" 2>/dev/null || echo "0")
log "Snapshot has $SESSION_COUNT sessions to restore"

if [[ "$SESSION_COUNT" -eq 0 ]]; then
    log "No sessions to restore"
    exit 0
fi

# Step 4: Restore each session
OK=0
SKIP=0
FAIL=0

while IFS= read -r session_json; do
    [[ -z "$session_json" ]] && continue

    name=$(echo "$session_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['name'])" 2>/dev/null || continue)
    socket=$(echo "$session_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['socket'])" 2>/dev/null || echo "$SOCKET_DIR/default-code.sock")
    uuid=$(echo "$session_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cc_session_id',''))" 2>/dev/null || echo "")
    cwd=$(echo "$session_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd','/root'))" 2>/dev/null || echo "/root")
    source=$(echo "$session_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('source','claw-remote'))" 2>/dev/null || echo "claw-remote")
    task_id=$(echo "$session_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('task_id',''))" 2>/dev/null || echo "")
    workdir=$(echo "$session_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('workdir','/root'))" 2>/dev/null || echo "/root")
    parent=$(echo "$session_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('parent_tmux_session',''))" 2>/dev/null || echo "")

    log "Restoring: $name (socket=${socket##*/} source=$source)"

    backend="$(backend_for_socket "$socket")"
    if bash "$RESTORE_SCRIPT" --name "$name" --backend "$backend" 2>&1; then
        OK=$((OK + 1))
    else
        # restore-sessions.sh may not have the session in registry for --name mode
        # Fall back to manual restore using restore_one logic
        if tmux -S "$socket" has-session -t "$name" 2>/dev/null; then
            log "  SKIP $name (already exists)"
            SKIP=$((SKIP + 1))
            continue
        fi

        # Create tmux session directly
        mkdir -p "$(dirname "$socket")"
        if tmux -S "$socket" new-session -d -s "$name" -c "$cwd" 2>/dev/null; then
            sleep 1
            tmux -S "$socket" set-environment -t "$name" CLAW_REMOTE_TMUX_SESSION "$name" 2>/dev/null || true
            tmux -S "$socket" set-environment -t "$name" CLAW_REMOTE_SOURCE "$source" 2>/dev/null || true

            if [[ "$source" == "dispatch" && -n "$task_id" ]]; then
                tmux -S "$socket" set-environment -t "$name" CODING_AGENT_TASK_ID "$task_id" 2>/dev/null || true
                tmux -S "$socket" set-environment -t "$name" CODING_AGENT_TMUX_SESSION "$name" 2>/dev/null || true
                tmux -S "$socket" set-environment -t "$name" CODING_AGENT_WORKDIR "$workdir" 2>/dev/null || true
            fi

            quoted_cwd="$(printf '%q' "$cwd")"
            if [[ -n "$uuid" ]]; then
                tmux -S "$socket" send-keys -t "$name" "cd ${quoted_cwd} && claude --resume ${uuid} --permission-mode acceptEdits" Enter
            else
                tmux -S "$socket" send-keys -t "$name" "cd ${quoted_cwd} && claude --permission-mode acceptEdits" Enter
            fi

            log "  OK $name restored manually"
            OK=$((OK + 1))
        else
            log "  FAIL $name (tmux create failed)"
            FAIL=$((FAIL + 1))
        fi
    fi
done < <(python3 -c "
import json
data = json.load(open('${SNAPSHOT_FILE}'))
for s in data.get('sessions', []):
    print(json.dumps(s))
" 2>/dev/null)

log "Restore completed: OK=$OK SKIP=$SKIP FAIL=$FAIL (total=$SESSION_COUNT)"
if [[ "$FAIL" -gt 0 ]]; then
    exit 1
fi
