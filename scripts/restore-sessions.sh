#!/bin/bash
# restore-sessions.sh - Restore Claw Remote / dispatch tmux sessions with CC history
# Usage:
#   ./restore-sessions.sh --name "session1" "session2"    # Restore by name
#   ./restore-sessions.sh --today                          # Restore today's lost sessions
#   ./restore-sessions.sh --since "2026-05-10"             # Restore sessions since date
#   ./restore-sessions.sh --all-lost                       # Restore all non-running sessions
#   ./restore-sessions.sh --dispatch                       # Restore only dispatch sessions
#   ./restore-sessions.sh --with-children                  # Also restore child sessions
#   ./restore-sessions.sh --list                           # List restorable sessions
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
REGISTRY="${SKILL_DIR}/data/session-registry.json"
DEFAULT_SOCKET="/root/clawdbot-tmux-sockets/default-code.sock"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

usage() {
    cat >&2 <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --name NAME [NAME...]   Restore specific session(s) by name
  --today                 Restore all lost sessions created today (Shanghai time)
  --since DATE            Restore sessions created since DATE (YYYY-MM-DD)
  --all-lost              Restore all sessions with state != running
  --dispatch              Restore only dispatch-sourced sessions
  --with-children         Also restore child sessions (by parent_tmux_session)
  --list                  List restorable sessions
  --backend PORT          Claw Remote API port (default: 18086)
  -h, --help              Show this help

Examples:
  $(basename "$0") --name "0510-remote" "0510-hook问题"
  $(basename "$0") --today
  $(basename "$0") --since "2026-05-09" --dispatch
  $(basename "$0") --name "website-create-example-wiki" --with-children
  $(basename "$0") --list
EOF
    exit 1
}

# Args
ACTION=""
NAMES=()
SINCE_DATE=""
BACKEND_PORT="18086"
FILTER_DISPATCH=""
WITH_CHILDREN=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)         ACTION="name"; shift; while [[ $# -gt 0 && ! "$1" =~ ^-- ]]; do NAMES+=("$1"); shift; done ;;
        --today)        ACTION="today"; shift ;;
        --since)        ACTION="since"; SINCE_DATE="$2"; shift 2 ;;
        --all-lost)     ACTION="all-lost"; shift ;;
        --dispatch)     FILTER_DISPATCH="1"; shift ;;
        --with-children) WITH_CHILDREN="1"; shift ;;
        --list)         ACTION="list"; shift ;;
        --backend)      BACKEND_PORT="$2"; shift 2 ;;
        -h|--help)      usage ;;
        *)              echo "Unknown option: $1" >&2; usage ;;
    esac
done

[[ -z "$ACTION" ]] && usage
CLAW_API="http://172.19.0.1:${BACKEND_PORT}"

if [[ ! -f "$REGISTRY" ]]; then
    echo -e "${RED}Registry file not found: ${REGISTRY}${NC}" >&2
    exit 1
fi

# Load Claude runtime env
CLAUDE_ENV=$(python3 "${SCRIPT_DIR}/load_claude_runtime_env.py" manual --config /root/.claude/runtime-routing.json --format shell 2>/dev/null || true)

now_ts() { TZ='Asia/Shanghai' date +%s; }
now_iso() { TZ='Asia/Shanghai' date -Iseconds; }

# Check if tmux session exists on the given socket
session_exists() {
    local name="$1"
    local socket="${2:-$DEFAULT_SOCKET}"
    if [[ -n "$socket" ]]; then
        tmux -S "$socket" has-session -t "$name" 2>/dev/null
    else
        tmux has-session -t "$name" 2>/dev/null
    fi
}

# Create and restore a single session
# Usage: restore_one <name> <uuid> <cwd> <socket> <source> <task_id> <workdir> <parent>
restore_one() {
    local name="$1"
    local uuid="$2"
    local cwd="${3:-/root}"
    local socket="${4:-$DEFAULT_SOCKET}"
    local source="${5:-claw-remote}"
    local task_id="${6:-}"
    local workdir="${7:-$cwd}"
    local parent="${8:-}"

    # Check if already exists on the correct socket
    if session_exists "$name" "$socket"; then
        echo -e "  ${YELLOW}SKIP${NC} $name (already exists on ${socket##*/})"
        return 0
    fi

    # Check CC history exists (only if uuid is non-empty)
    if [[ -n "$uuid" ]]; then
        local jsonl="/root/.claude/projects/-root/${uuid}.jsonl"
        if [[ ! -f "$jsonl" ]]; then
            echo -e "  ${RED}FAIL${NC} $name (CC history not found: ${uuid:0:8}...)"
            return 1
        fi
    fi

    local socket_dir="${socket%/*}"
    mkdir -p "$socket_dir"

    # Restore directly through tmux. The Claw API intentionally rejects raw
    # bash session creation in model-specific modes.
    tmux -S "$socket" new-session -d -s "$name" -c "$cwd" 2>/dev/null || {
        echo -e "  ${RED}FAIL${NC} $name (tmux create failed on ${socket##*/})"
        return 1
    }

    sleep 1

    # Set Claw Remote environment variables
    tmux -S "$socket" set-environment -t "$name" CLAW_REMOTE_TMUX_SESSION "$name" 2>/dev/null || true
    tmux -S "$socket" set-environment -t "$name" CLAW_REMOTE_SOURCE "$source" 2>/dev/null || true

    # Apply Claude runtime env as tmux environment
    if [[ -n "$CLAUDE_ENV" ]]; then
        while IFS= read -r line; do
            [[ "$line" =~ ^export\ ([^=]+)=(.*)$ ]] || continue
            tmux -S "$socket" set-environment -t "$name" "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" 2>/dev/null || true
        done <<< "$CLAUDE_ENV"
    fi
    tmux -S "$socket" set-environment -t "$name" CLAUDE_CODE_AUTO_COMPACT_WINDOW "350000" 2>/dev/null || true

    # Inject dispatch-specific environment variables
    if [[ "$source" == "dispatch" && -n "$task_id" ]]; then
        tmux -S "$socket" set-environment -t "$name" CODING_AGENT_TASK_ID "$task_id" 2>/dev/null || true
        tmux -S "$socket" set-environment -t "$name" CODING_AGENT_TMUX_SESSION "$name" 2>/dev/null || true
        tmux -S "$socket" set-environment -t "$name" CODING_AGENT_SESSION_DIR "/root/clawd/data/claude-code-results/sessions/$name" 2>/dev/null || true
        tmux -S "$socket" set-environment -t "$name" CODING_AGENT_WORKDIR "$workdir" 2>/dev/null || true
        tmux -S "$socket" set-environment -t "$name" CODING_AGENT_FEISHU_TARGET "" 2>/dev/null || true
        # Set ANTHROPIC env vars for dispatch socket
        if [[ -n "$ANTHROPIC_BASE_URL" ]]; then
            tmux -S "$socket" set-environment -t "$name" ANTHROPIC_BASE_URL "$ANTHROPIC_BASE_URL" 2>/dev/null || true
        fi
    fi

    # Launch claude --resume or claude (new session if no uuid)
    local quoted_cwd
    quoted_cwd="$(printf '%q' "$cwd")"
    if [[ -n "$uuid" ]]; then
        tmux -S "$socket" send-keys -t "$name" "cd ${quoted_cwd} && claude --resume ${uuid} --permission-mode acceptEdits" Enter
    else
        tmux -S "$socket" send-keys -t "$name" "cd ${quoted_cwd} && claude --permission-mode acceptEdits" Enter
    fi

    echo -e "  ${GREEN}OK${NC}   $name -> ${uuid:0:8}... (socket=${socket##*/} source=$source)"
    return 0
}

# Auto-confirm workspace trust after delay
auto_confirm_trust() {
    local name="$1"
    local socket="${2:-$DEFAULT_SOCKET}"
    # Wait for claude to start (up to 15s)
    for i in $(seq 1 15); do
        sleep 1
        local cmd
        cmd=$(tmux -S "$socket" list-panes -t "$name" -F "#{pane_current_command}" 2>/dev/null || true)
        if [[ "$cmd" == "claude" || "$cmd" == "node" ]]; then
            sleep 2
            tmux -S "$socket" send-keys -t "$name" Enter 2>/dev/null || true
            return 0
        fi
    done
}

# List sessions from registry
list_sessions() {
    echo "Sessions in registry:"
    echo ""
    python3 -c "
import json, sys, subprocess
from pathlib import Path

with open('${REGISTRY}') as f:
    data = json.load(f)

sessions = data.get('sessions', {})
items = sorted(sessions.items(), key=lambda x: x[1].get('created_at_ts', 0))

for name, s in items:
    state = s.get('state', '?')
    uuid = s.get('cc_session_id', '?')
    created = s.get('created_at', '?')[:19]
    fp = s.get('first_prompt', '')[:60]
    cmd = s.get('command', '?')
    socket = s.get('tmux_socket', '')
    source = s.get('source', 'claw-remote')
    parent = s.get('parent_tmux_session', '')
    task_id = s.get('task_id', '')

    # Check if tmux session exists on its socket
    sock = socket or '${DEFAULT_SOCKET}'
    exists = subprocess.run(['tmux', '-S', sock, 'has-session', '-t', name], capture_output=True).returncode == 0
    alive = 'ALIVE' if exists else 'LOST'

    print(f'  [{alive:5}] [{state:8}] {name}')
    print(f'          uuid={uuid[:16] if len(uuid) > 16 else uuid}')
    src_info = f'source={source}'
    if parent:
        src_info += f' parent={parent}'
    if task_id:
        src_info += f' task={task_id[:40]}'
    print(f'          {src_info}')
    print(f'          created={created}  cmd={cmd}  socket={Path(sock).name if sock else \"default\"}')
    if fp:
        print(f'          first_prompt: {fp}')
    print()
" 2>/dev/null
}

# Main logic
case "$ACTION" in
    list)
        list_sessions
        ;;

    name|today|since|all-lost)
        # Build name filter for --name
        NAME_FILTER=""
        if [[ "$ACTION" == "name" ]]; then
            NAME_FILTER=$(printf '%s\n' "${NAMES[@]}" | python3 -c "import sys,json; print(json.dumps([l.strip() for l in sys.stdin]))" 2>/dev/null)
        fi

        # Collect names from --with-children expansion
        EXTRA_NAMES="[]"
        if [[ -n "$WITH_CHILDREN" && "$ACTION" == "name" ]]; then
            EXTRA_NAMES=$(python3 -c "
import json
with open('${REGISTRY}') as f:
    data = json.load(f)
sessions = data.get('sessions', {})
extra = set()
names = ${NAME_FILTER:-[]}
for name, s in sessions.items():
    parent = s.get('parent_tmux_session', '')
    if parent and parent in names:
        extra.add(name)
    # Also check if any name is a parent of this session
    for n in names:
        if parent == n:
            extra.add(name)
print(json.dumps(list(extra)))
" 2>/dev/null)
        fi

        # Get session list from registry
        TARGETS=$(python3 -c "
import json, sys, subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

with open('${REGISTRY}') as f:
    data = json.load(f)

sessions = data.get('sessions', {})
results = []
filter_dispatch = bool(${FILTER_DISPATCH:-0})
extra_names = ${EXTRA_NAMES:-[]}

for name, s in sessions.items():
    state = s.get('state', 'unknown')
    uuid = s.get('cc_session_id', '')
    cwd = s.get('cwd', '/root')
    ts = s.get('created_at_ts', 0)
    socket = s.get('tmux_socket', '') or '${DEFAULT_SOCKET}'
    source = s.get('source', 'claw-remote')
    task_id = s.get('task_id', '')
    workdir = s.get('cwd', '/root')
    parent = s.get('parent_tmux_session', '')

    # Check if tmux session already exists on its socket
    exists = subprocess.run(['tmux', '-S', socket, 'has-session', '-t', name], capture_output=True).returncode == 0
    if exists:
        continue

    # Filter: dispatch only
    if filter_dispatch and source != 'dispatch':
        continue

    # Filter logic
    action = '${ACTION}'
    if action == 'name':
        names = ${NAME_FILTER:-[]} + extra_names
        if name not in names:
            continue
    elif action == 'today':
        from zoneinfo import ZoneInfo
        now_sh = datetime.now(ZoneInfo('Asia/Shanghai'))
        today_start = datetime(now_sh.year, now_sh.month, now_sh.day, tzinfo=ZoneInfo('Asia/Shanghai'))
        if ts < today_start.timestamp():
            continue
    elif action == 'since':
        from zoneinfo import ZoneInfo
        since_date = '${SINCE_DATE}' or '2026-01-01'
        since_dt = datetime.strptime(since_date, '%Y-%m-%d').replace(tzinfo=ZoneInfo('Asia/Shanghai'))
        if ts < since_dt.timestamp():
            continue
    elif action == 'all-lost':
        pass

    results.append({
        'name': name, 'uuid': uuid, 'cwd': cwd, 'socket': socket,
        'source': source, 'task_id': task_id, 'workdir': workdir, 'parent': parent,
    })

print(json.dumps(results))
" 2>/dev/null)

        COUNT=$(echo "$TARGETS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")

        if [[ "$COUNT" == "0" ]]; then
            echo "No sessions to restore."
            exit 0
        fi

        echo "Restoring $COUNT session(s)..."
        echo ""

        RESTORED=0
        FAILED=0

        while IFS='|' read -r name uuid cwd socket source task_id workdir parent; do
            if restore_one "$name" "$uuid" "$cwd" "$socket" "$source" "$task_id" "$workdir" "$parent"; then
                RESTORED=$((RESTORED + 1))
                auto_confirm_trust "$name" "$socket" &
            else
                FAILED=$((FAILED + 1))
            fi
        done < <(echo "$TARGETS" | python3 -c "
import json, sys
for item in json.load(sys.stdin):
    print(f\"{item['name']}|{item['uuid']}|{item['cwd']}|{item['socket']}|{item['source']}|{item.get('task_id', '')}|{item.get('workdir', '')}|{item.get('parent', '')}\")
")

        echo ""
        echo "Done. Restored: $RESTORED, Failed: $FAILED"
        if [[ "$FAILED" -gt 0 ]]; then
            exit 1
        fi

        # Update registry states
        sleep 2
        ;;
esac
