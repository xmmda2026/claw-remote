#!/bin/bash
# snapshot-sessions.sh - Snapshot alive tmux sessions for auto-restore after reboot
# Called by cron every 5 minutes
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
SNAPSHOT_FILE="${SKILL_DIR}/data/session-snapshot.json"
REGISTRY="${SKILL_DIR}/data/session-registry.json"
LOG_DIR="/root/.openclaw/logs"
SOCKET_DIR="/root/clawdbot-tmux-sockets"

mkdir -p "$(dirname "$SNAPSHOT_FILE")" "$LOG_DIR"

SOCKETS_JSON=$(python3 -c "import json; print(json.dumps([
  '${SOCKET_DIR}/claude-code.sock',
  '${SOCKET_DIR}/codex-code.sock',
  '${SOCKET_DIR}/default-code.sock'
]))")

python3 -c "
import json, os, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

sockets = json.loads('${SOCKETS_JSON}')
registry_path = '${REGISTRY}'
snapshot_path = '${SNAPSHOT_FILE}'
backup_dir = str(Path(snapshot_path).with_name('session-snapshot-backups'))

# Load registry for enrichment
registry = {}
if Path(registry_path).exists():
    try:
        registry = json.load(open(registry_path)).get('sessions', {})
    except Exception:
        pass

sessions = []

for socket in sockets:
    # Get actual tmux sessions on this socket
    try:
        result = subprocess.run(
            ['tmux', '-S', socket, 'list-sessions', '-F', '#{session_name}'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            continue
        names = [n.strip() for n in result.stdout.strip().split('\n') if n.strip()]
    except Exception:
        continue

    for name in names:
        # Enrich from registry
        reg = registry.get(name, {})
        sessions.append({
            'name': name,
            'socket': socket,
            'cc_session_id': reg.get('cc_session_id', ''),
            'cwd': reg.get('cwd', '/root'),
            'command': reg.get('command', 'bash'),
            'source': reg.get('source', 'claw-remote'),
            'task_id': reg.get('task_id', ''),
            'workdir': reg.get('cwd', '/root'),
            'parent_tmux_session': reg.get('parent_tmux_session', ''),
        })

tz_sh = __import__('zoneinfo').ZoneInfo('Asia/Shanghai')
snapshot = {
    'snapshot_at': datetime.now(tz_sh).isoformat(),
    'session_count': len(sessions),
    'sessions': sessions,
}

snapshot_file = Path(snapshot_path)
backup_path = Path(backup_dir)
previous = {}
previous_count = 0
if snapshot_file.exists():
    try:
        previous = json.loads(snapshot_file.read_text(encoding='utf-8'))
        previous_count = int(previous.get('session_count') or 0)
    except Exception:
        previous = {}
        previous_count = 0

    backup_path.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz_sh).strftime('%Y%m%d-%H%M%S')
    shutil.copy2(snapshot_file, backup_path / f'session-snapshot-{stamp}.json')

if len(sessions) == 0 and previous_count > 0 and os.environ.get('ALLOW_EMPTY_SNAPSHOT') != '1':
    empty_scan_path = snapshot_file.with_name('session-snapshot-empty-scan.json')
    empty_scan_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
    print(
        f'Refusing to overwrite non-empty snapshot ({previous_count} sessions) '
        f'with empty scan. Empty scan saved to {empty_scan_path}',
        file=sys.stderr,
    )
    sys.exit(1)

tmp_path = snapshot_file.with_name(f'.{snapshot_file.name}.{os.getpid()}.tmp')
tmp_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
tmp_path.replace(snapshot_file)
print(f'Snapshot: {len(sessions)} alive sessions saved to {snapshot_path}')
" 2>&1
