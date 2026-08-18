#!/usr/bin/env python3
"""
Claw - CLaude AnyWhere
Remote control for Claude Code sessions from your phone.
https://github.com/raullenchai/claw
"""

import http.server
import subprocess
import socketserver
import os
import json
import html
import re
import argparse
import platform
import stat
import signal
import sys
import threading
import fcntl
import time
import secrets
import base64
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs, unquote_to_bytes
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError


TMUX_SOCKET = None
SESSION_SOCKET_MAP = {}  # session_name → socket_path, populated by get_tmux_sessions()
SHANGHAI_TZ = ZoneInfo('Asia/Shanghai')
SKILL_DIR = Path(__file__).resolve().parent.parent
SESSION_LABELS_PATH = SKILL_DIR / 'tmp' / 'session-labels.json'
ANSI_RE = re.compile(r'\x1b\[[0-9;:]*[A-Za-z]')
SESSION_HOOK_STATUS_PATH = SKILL_DIR / 'tmp' / 'session-hook-status.json'
CUSTOM_PROMPTS_PATH = SKILL_DIR / 'data' / 'custom-prompts.json'
SENT_PROMPTS_PATH = SKILL_DIR / 'data' / 'sent-prompts.json'
SESSION_PROMPTS_PATH = SKILL_DIR / 'data' / 'session-prompts.json'
PROMPT_OPTIMIZE_TEMPLATES_PATH = SKILL_DIR / 'data' / 'prompt-optimize-templates.json'
SESSION_PROMPT_PLAN_TEMPLATE_PATH = SKILL_DIR / 'data' / 'session-prompt-plan-template.json'
SESSION_REGISTRY_PATH = SKILL_DIR / 'data' / 'session-registry.json'
REMOTE_IMAGE_UPLOAD_DIR = SKILL_DIR / 'tmp' / 'remote-upload-images'
PROMPT_FILES_DIR = SKILL_DIR / 'tmp' / 'prompt-files'
UPLOAD_FAILURE_LOG_PATH = SKILL_DIR / 'tmp' / 'remote-image-upload-errors.log'
MAX_UPLOAD_IMAGE_COUNT = 10
DEFAULT_SESSION_PROMPT_PLAN_TEMPLATE = """1、修改文件：

功能描述：

验收效果：

2、

3、

4、

5、"""
DEFAULT_PROMPT_OPTIMIZE_TEMPLATE = """请结合当前会话上下文，帮我优化下面这段后续 prompt 想法：
- 补全必要背景
- 拆成清晰步骤
- 指出可能遗漏的约束和风险
- 最后给出一版可以直接发送执行的 prompt

草稿：
{draft}"""
FRONTEND_DIR = SKILL_DIR / 'frontend' / 'claw-remote'
STATIC_DIR = FRONTEND_DIR / 'static'
INDEX_TEMPLATE_PATH = FRONTEND_DIR / 'index.html'
IMAGE_MIME_EXTENSIONS = {
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/gif': '.gif',
    'image/webp': '.webp',
}


def now_shanghai():
    return datetime.now(SHANGHAI_TZ)


def write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f'.{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp')
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp_path, path)


def with_file_lock(path, callback):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f'{path.name}.lock')
    with lock_path.open('a+', encoding='utf-8') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        return callback()


def safe_json_load(path, default):
    path = Path(path)

    def _default_value():
        if callable(default):
            return default()
        return json.loads(json.dumps(default))

    def _load():
        if not path.exists():
            return _default_value()
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return _default_value()
        if data is None:
            return _default_value()
        return data

    return with_file_lock(path, _load)


def _json_update(path, default, updater):
    path = Path(path)

    def _update():
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding='utf-8'))
            except Exception:
                current = default() if callable(default) else json.loads(json.dumps(default))
        else:
            current = default() if callable(default) else json.loads(json.dumps(default))
        next_data = updater(current)
        write_json_atomic(path, next_data)
        return next_data

    return with_file_lock(path, _update)


def _session_store_key(mode, session_name):
    return f'{str(mode or "claude").strip() or "claude"}:{str(session_name or "").strip()}'


def _sanitize_prompt_text(value):
    return str(value or '').replace('\x00', '').strip()


def _sanitize_image_name(name):
    value = Path(str(name or 'image')).name
    value = re.sub(r'[^0-9A-Za-z._-]+', '_', value).strip('._')
    return value or 'image'


def log_image_upload_failure(reason, detail=''):
    UPLOAD_FAILURE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    message = f'[{now_shanghai().isoformat()}] {reason}'
    if detail:
        message += f' | {detail}'
    with UPLOAD_FAILURE_LOG_PATH.open('a', encoding='utf-8') as fh:
        fh.write(message + '\n')


def _detect_image_mime(data):
    if not data or len(data) < 4:
        return ''
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return ''


def decode_data_url_image(data_url):
    match = re.match(r'^data:([^;,]+)?((?:;[^;,=]+=[^;,=]+)*)(;base64)?,(.*)$', str(data_url or ''), re.DOTALL)
    if not match:
        raise ValueError('Invalid data URL')
    mime = (match.group(1) or '').strip().lower()
    is_base64 = bool(match.group(3))
    payload = match.group(4) or ''
    if is_base64:
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception as exc:
            raise ValueError('Invalid base64 image payload') from exc
    else:
        raw = unquote_to_bytes(payload)
    detected_mime = _detect_image_mime(raw)
    if detected_mime:
        mime = detected_mime
    if mime not in IMAGE_MIME_EXTENSIONS:
        raise ValueError(f'Unsupported image MIME type: {mime or "unknown"}')
    return mime, raw


def cleanup_old_remote_images(max_age_seconds=7 * 24 * 60 * 60):
    REMOTE_IMAGE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - max_age_seconds
    for path in REMOTE_IMAGE_UPLOAD_DIR.iterdir():
        try:
            if not path.is_file():
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except Exception:
            continue


def _find_remote_image_path(image_id):
    if not re.fullmatch(r'[0-9A-Za-z_-]{8,128}', str(image_id or '')):
        return None
    if not REMOTE_IMAGE_UPLOAD_DIR.exists():
        return None
    for path in REMOTE_IMAGE_UPLOAD_DIR.glob(f'{image_id}.*'):
        if path.is_file():
            return path
    return None


def save_remote_images(images):
    images = list(images or [])
    if len(images) > MAX_UPLOAD_IMAGE_COUNT:
        raise ValueError(f'Too many images (max {MAX_UPLOAD_IMAGE_COUNT})')
    cleanup_old_remote_images()
    REMOTE_IMAGE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for item in images:
        try:
            mime, raw = decode_data_url_image(item.get('data', ''))
            ext = IMAGE_MIME_EXTENSIONS[mime]
            image_id = secrets.token_urlsafe(12).replace('-', 'a').replace('_', 'b')
            file_path = REMOTE_IMAGE_UPLOAD_DIR / f'{image_id}{ext}'
            file_path.write_bytes(raw)
            saved.append({
                'id': image_id,
                'path': str(file_path),
                'name': _sanitize_image_name(item.get('name') or f'image{ext}'),
                'mime': mime,
                'size': len(raw),
            })
        except Exception as exc:
            log_image_upload_failure('save_remote_images', str(exc))
            raise
    return saved


def normalize_sent_prompt_images(images):
    normalized = []
    for item in list(images or [])[:MAX_UPLOAD_IMAGE_COUNT]:
        if not isinstance(item, dict):
            continue
        image_id = str(item.get('id') or '').strip()
        path = str(item.get('path') or '').strip()
        if image_id and not path:
            found = _find_remote_image_path(image_id)
            path = str(found) if found else ''
        exists = bool(path and Path(path).exists())
        normalized.append({
            'id': image_id,
            'path': path,
            'name': _sanitize_image_name(item.get('name') or Path(path).name or 'image'),
            'mime': str(item.get('mime') or item.get('type') or '').strip(),
            'size': int(item.get('size') or 0),
            'exists': exists,
        })
    return normalized


def build_image_path_block(images):
    ready_images = [image for image in normalize_sent_prompt_images(images) if image.get('exists') and image.get('path')]
    if not ready_images:
        return ''
    lines = ['[Attached Images]', 'Use these local image files as part of the request:']
    for image in ready_images:
        lines.append(f"- {image['path']} ({image['name']})")
    return '\n'.join(lines)


def build_message_with_images(text, images):
    body = str(text or '').replace('\x00', '').strip()
    image_block = build_image_path_block(images)
    if body and image_block:
        return f'{body}\n\n{image_block}'
    if image_block:
        return image_block
    return body


def _load_hook_status_store():
    data = safe_json_load(SESSION_HOOK_STATUS_PATH, {'sessions': {}})
    if not isinstance(data, dict):
        return {'sessions': {}}
    sessions = data.get('sessions')
    if not isinstance(sessions, dict):
        data['sessions'] = {}
    return data


def mark_session_hook_status(session_name, state, source='claw-remote'):
    session_name = str(session_name or '').strip()
    if not session_name:
        return

    def _update(data):
        sessions = data.get('sessions')
        if not isinstance(sessions, dict):
            sessions = {}
            data['sessions'] = sessions
        sessions[session_name] = {
            'state': str(state or 'idle'),
            'source': source,
            'updated_at': now_shanghai().isoformat(),
            'updated_at_ts': int(time.time()),
        }
        return data

    _json_update(SESSION_HOOK_STATUS_PATH, {'sessions': {}}, _update)


def _build_history_prompt_file(text):
    PROMPT_FILES_DIR.mkdir(parents=True, exist_ok=True)
    prompt_id = f'{int(time.time())}-{secrets.token_hex(4)}'
    prompt_path = PROMPT_FILES_DIR / f'prompt-{prompt_id}.txt'
    prompt_path.write_text(str(text or ''), encoding='utf-8')
    return {
        'path': str(prompt_path),
        'size': prompt_path.stat().st_size,
        'name': prompt_path.name,
    }


def load_custom_prompts():
    data = safe_json_load(CUSTOM_PROMPTS_PATH, {'prompts': []})
    prompts = data.get('prompts') if isinstance(data, dict) else []
    if not isinstance(prompts, list):
        return []
    normalized = []
    for item in prompts:
        if not isinstance(item, dict):
            continue
        prompt_id = str(item.get('id') or '').strip()
        title = str(item.get('title') or '').strip()
        text = str(item.get('text') or '')
        if not prompt_id or not title:
            continue
        normalized.append({
            'id': prompt_id,
            'title': title,
            'text': text,
            'updated_at': item.get('updated_at') or '',
        })
    normalized.sort(key=lambda item: item.get('updated_at') or '', reverse=True)
    return normalized


def save_custom_prompt(prompt_id, title, text):
    title = str(title or '').strip()
    text = str(text or '').strip()
    if not title:
        return {'success': False, 'error': 'Title is required'}
    if not text:
        return {'success': False, 'error': 'Text is required'}
    if len(title) > 80:
        return {'success': False, 'error': 'Title too long (max 80 chars)'}
    if prompt_id and not re.fullmatch(r'[0-9A-Za-z_-]{6,128}', str(prompt_id)):
        return {'success': False, 'error': 'Invalid prompt id'}

    def _update(data):
        prompts = data.get('prompts')
        if not isinstance(prompts, list):
            prompts = []
        now_iso = now_shanghai().isoformat()
        current_id = str(prompt_id or secrets.token_urlsafe(9)).replace('-', 'a').replace('_', 'b')
        record = {
            'id': current_id,
            'title': title,
            'text': text,
            'updated_at': now_iso,
        }
        next_prompts = [item for item in prompts if isinstance(item, dict) and item.get('id') != current_id]
        next_prompts.insert(0, record)
        data['prompts'] = next_prompts[:100]
        return data

    _json_update(CUSTOM_PROMPTS_PATH, {'prompts': []}, _update)
    return {'success': True}


def delete_custom_prompt(prompt_id):
    prompt_id = str(prompt_id or '').strip()
    if not prompt_id:
        return {'success': False, 'error': 'Prompt id is required'}

    def _update(data):
        prompts = data.get('prompts')
        if not isinstance(prompts, list):
            prompts = []
        data['prompts'] = [item for item in prompts if isinstance(item, dict) and item.get('id') != prompt_id]
        return data

    _json_update(CUSTOM_PROMPTS_PATH, {'prompts': []}, _update)
    return {'success': True}


def get_session_prompt_note(mode, session_name):
    data = safe_json_load(SESSION_PROMPTS_PATH, {'notes': {}})
    notes = data.get('notes') if isinstance(data, dict) else {}
    if not isinstance(notes, dict):
        notes = {}
    note = notes.get(_session_store_key(mode, session_name), {})
    if not isinstance(note, dict):
        note = {}
    return {
        'text': str(note.get('text') or ''),
        'images': normalize_sent_prompt_images(note.get('images') or []),
        'updated_at': note.get('updated_at') or '',
        'updated_at_ts': int(note.get('updated_at_ts') or 0),
    }


def save_session_prompt_note(mode, session_name, text, images):
    key = _session_store_key(mode, session_name)
    text = str(text or '')
    images = normalize_sent_prompt_images(images)

    def _update(data):
        notes = data.get('notes')
        if not isinstance(notes, dict):
            notes = {}
            data['notes'] = notes
        if not text.strip() and not images:
            notes.pop(key, None)
            return data
        notes[key] = {
            'text': text,
            'images': images,
            'updated_at': now_shanghai().isoformat(),
            'updated_at_ts': int(time.time()),
        }
        return data

    _json_update(SESSION_PROMPTS_PATH, {'notes': {}}, _update)
    return {'success': True, 'note': get_session_prompt_note(mode, session_name)}


def load_sent_prompts(mode, session_name, limit=20):
    data = safe_json_load(SENT_PROMPTS_PATH, {'sessions': {}})
    sessions = data.get('sessions') if isinstance(data, dict) else {}
    if not isinstance(sessions, dict):
        sessions = {}
    items = sessions.get(_session_store_key(mode, session_name), [])
    if not isinstance(items, list):
        return []
    normalized = []
    for item in items[:max(1, int(limit or 20))]:
        if not isinstance(item, dict):
            continue
        normalized.append({
            'text': str(item.get('text') or ''),
            'sent_at': item.get('sent_at') or '',
            'images': normalize_sent_prompt_images(item.get('images') or []),
            'prompt_file': item.get('prompt_file') or '',
            'text_truncated': bool(item.get('text_truncated')),
        })
    return normalized


def append_sent_prompt(mode, session_name, text, images, prompt_file=None):
    key = _session_store_key(mode, session_name)
    text = str(text or '')
    normalized_images = normalize_sent_prompt_images(images)
    prompt_file = prompt_file or ''

    def _update(data):
        sessions = data.get('sessions')
        if not isinstance(sessions, dict):
            sessions = {}
            data['sessions'] = sessions
        existing = sessions.get(key, [])
        if not isinstance(existing, list):
            existing = []
        item = {
            'text': text,
            'sent_at': now_shanghai().isoformat(),
            'images': normalized_images,
            'prompt_file': prompt_file,
            'text_truncated': bool(prompt_file),
        }
        sessions[key] = [item] + existing[:49]
        return data

    _json_update(SENT_PROMPTS_PATH, {'sessions': {}}, _update)

    # Update first_prompt in session-registry
    _update_registry_first_prompt(session_name, text)


def _register_session(tmux_name, cc_session_id, command, cwd):
    """Record tmux session -> CC session UUID mapping."""
    now = now_shanghai()

    def _update(data):
        sessions = data.get('sessions')
        if not isinstance(sessions, dict):
            sessions = {}
        sessions[tmux_name] = {
            'tmux_name': tmux_name,
            'cc_session_id': cc_session_id,
            'created_at': now.isoformat(),
            'created_at_ts': int(now.timestamp()),
            'command': command,
            'cwd': str(cwd),
            'first_prompt': '',
            'first_prompt_at': '',
            'backend': 'claude-code' if TMUX_SOCKET else 'default',
            'tmux_socket': TMUX_SOCKET or '',
            'state': 'running',
        }
        data['sessions'] = sessions
        return data

    _json_update(SESSION_REGISTRY_PATH, {'version': 1, 'sessions': {}}, _update)


def _update_registry_first_prompt(session_name, text):
    """Update first_prompt in session-registry if not yet set."""
    def _update(data):
        sessions = data.get('sessions', {})
        if session_name in sessions and not sessions[session_name].get('first_prompt'):
            sessions[session_name]['first_prompt'] = str(text)[:500]
            sessions[session_name]['first_prompt_at'] = now_shanghai().isoformat()
        data['sessions'] = sessions
        return data
    _json_update(SESSION_REGISTRY_PATH, {'version': 1, 'sessions': {}}, _update)


def _update_registry_state(session_name, state):
    """Update session state in registry."""
    def _update(data):
        sessions = data.get('sessions', {})
        if session_name in sessions:
            sessions[session_name]['state'] = state
        data['sessions'] = sessions
        return data
    _json_update(SESSION_REGISTRY_PATH, {'version': 1, 'sessions': {}}, _update)


def get_prompt_optimize_template(mode, session_name):
    data = safe_json_load(PROMPT_OPTIMIZE_TEMPLATES_PATH, {'global_template': DEFAULT_PROMPT_OPTIMIZE_TEMPLATE, 'sessions': {}})
    if not isinstance(data, dict):
        data = {'global_template': DEFAULT_PROMPT_OPTIMIZE_TEMPLATE, 'sessions': {}}
    sessions = data.get('sessions')
    if not isinstance(sessions, dict):
        sessions = {}
    return {
        'global_template': str(data.get('global_template') or DEFAULT_PROMPT_OPTIMIZE_TEMPLATE),
        'session_template': str(sessions.get(_session_store_key(mode, session_name)) or ''),
    }


def save_prompt_optimize_template(mode, session_name, scope, template):
    scope = str(scope or 'global').strip()
    template = str(template or '').strip()
    key = _session_store_key(mode, session_name)

    def _update(data):
        if not isinstance(data, dict):
            data = {}
        data['global_template'] = str(data.get('global_template') or DEFAULT_PROMPT_OPTIMIZE_TEMPLATE)
        sessions = data.get('sessions')
        if not isinstance(sessions, dict):
            sessions = {}
            data['sessions'] = sessions
        if scope == 'session':
            sessions[key] = template
        else:
            data['global_template'] = template or DEFAULT_PROMPT_OPTIMIZE_TEMPLATE
        data['updated_at'] = now_shanghai().isoformat()
        return data

    _json_update(PROMPT_OPTIMIZE_TEMPLATES_PATH, {'global_template': DEFAULT_PROMPT_OPTIMIZE_TEMPLATE, 'sessions': {}}, _update)
    result = get_prompt_optimize_template(mode, session_name)
    result['success'] = True
    return result


def get_session_prompt_plan_template():
    data = safe_json_load(SESSION_PROMPT_PLAN_TEMPLATE_PATH, {'template': DEFAULT_SESSION_PROMPT_PLAN_TEMPLATE, 'updated_at': ''})
    if not isinstance(data, dict):
        data = {'template': DEFAULT_SESSION_PROMPT_PLAN_TEMPLATE, 'updated_at': ''}
    return {
        'template': str(data.get('template') or DEFAULT_SESSION_PROMPT_PLAN_TEMPLATE),
        'updated_at': data.get('updated_at') or '',
    }


def save_session_prompt_plan_template(template):
    template = str(template or '').strip() or DEFAULT_SESSION_PROMPT_PLAN_TEMPLATE

    def _update(_data):
        return {
            'template': template,
            'updated_at': now_shanghai().isoformat(),
        }

    _json_update(SESSION_PROMPT_PLAN_TEMPLATE_PATH, {'template': DEFAULT_SESSION_PROMPT_PLAN_TEMPLATE, 'updated_at': ''}, _update)
    result = get_session_prompt_plan_template()
    result['success'] = True
    return result


def _infer_mode_from_path(path):
    raw_path = urlparse(str(path or '/')).path
    if raw_path.startswith('/codex/'):
        return 'codex'
    if raw_path.startswith('/default/'):
        return 'default'
    return 'claude'


def _format_hook_time(iso_str):
    """Format an ISO timestamp into a short Shanghai-time display like '上午10:37'."""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(iso_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=__import__('datetime').timezone.utc)
        dt_sh = dt.astimezone(SHANGHAI_TZ)
        return format_shanghai_time(dt_sh)
    except Exception:
        return str(iso_str)


def _strip_mode_prefix(path):
    raw_path = urlparse(str(path or '/')).path
    for prefix in ('/claude', '/codex', '/default'):
        if raw_path == prefix:
            return '/'
        if raw_path.startswith(prefix + '/'):
            return raw_path[len(prefix):] or '/'
    return raw_path or '/'


def _augment_sessions_for_frontend(mode, sessions):
    hook_status_map = _load_hook_status_store().get('sessions', {})
    enriched = []
    for session in list(sessions or []):
        if not isinstance(session, dict):
            continue
        item = dict(session)
        note = get_session_prompt_note(mode, item.get('name', ''))
        has_prompt_plan = bool(note.get('text', '').strip() or note.get('images'))
        hook_status = hook_status_map.get(item.get('name')) or {'state': 'idle'}
        item['hook_status'] = hook_status
        item['has_prompt_plan'] = has_prompt_plan
        item['prompt_plan_pending'] = bool(has_prompt_plan and hook_status.get('state') == 'completed')
        # Use the latest timestamp across tmux activity, hook status, and prompt note
        hook_ts = int(hook_status.get('updated_at_ts') or 0)
        item['display_activity_ts'] = max(int(item.get('last_activity_ts') or 0), int(note.get('updated_at_ts') or 0), hook_ts)
        # Prefer hook completion time as display text for non-idle hook states
        if hook_status.get('state') in ('running', 'completed', 'uncertain') and hook_status.get('updated_at'):
            item['display_activity'] = _format_hook_time(hook_status['updated_at'])
        else:
            item['display_activity'] = item.get('last_activity') or ''
        enriched.append(item)
    return enriched


def _send_text_via_tmux_buffer(session_name, text, press_enter=True, window=None, pane=None):
    if not CommandSender._validate_session_name(session_name):
        return {'success': False, 'error': 'Invalid session name'}
    if window is not None and not CommandSender._validate_session_name(window):
        return {'success': False, 'error': 'Invalid window index'}
    if pane is not None and not CommandSender._validate_session_name(pane):
        return {'success': False, 'error': 'Invalid pane index'}

    target = CommandSender._build_target(session_name, window, pane)
    target_command = CommandSender._get_target_command(target)
    sanitized_text = str(text or '').replace('\x00', '')
    if CommandSender._is_ai_quick_command(sanitized_text) and not CommandSender._target_allows_ai_quick_command(session_name, target_command):
        return {'success': False, 'error': f'Quick commands are only allowed for claude/codex panes, current pane is {target_command or "unknown"}'}

    prompt_file = _build_history_prompt_file(sanitized_text)
    buffer_name = f'claw-{secrets.token_hex(6)}'
    load_result = subprocess.run(
        tmux_cmd_for_session(session_name, 'load-buffer', '-b', buffer_name, prompt_file['path']),
        capture_output=True,
        text=True,
        timeout=5,
    )
    if load_result.returncode != 0:
        return {'success': False, 'error': load_result.stderr.strip() or 'Failed to load tmux buffer'}

    paste_result = subprocess.run(
        tmux_cmd_for_session(session_name, 'paste-buffer', '-b', buffer_name, '-d', '-t', target),
        capture_output=True,
        text=True,
        timeout=5,
    )
    if paste_result.returncode != 0:
        return {'success': False, 'error': paste_result.stderr.strip() or 'Failed to paste tmux buffer'}
    if press_enter:
        subprocess.run(tmux_cmd_for_session(session_name, 'send-keys', '-t', target, 'Enter'), capture_output=True, text=True, timeout=5)
    return {'success': True, 'mode': 'file', 'prompt_file': prompt_file}


def strip_ansi(text):
    return ANSI_RE.sub('', text or '')


def load_session_labels():
    if not SESSION_LABELS_PATH.exists():
        return {}
    try:
        data = json.loads(SESSION_LABELS_PATH.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(k) and str(v).strip()}


def save_session_labels(labels):
    SESSION_LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_LABELS_PATH.write_text(json.dumps(labels, ensure_ascii=False, indent=2) + '\n')


def set_session_label(session_name, label):
    if not re.match(r'^[\w\-.]+$', str(session_name or '')):
        return {'success': False, 'error': 'Invalid session name'}
    label = strip_ansi(str(label or '')).strip()
    if len(label) > 80:
        return {'success': False, 'error': 'Display name too long (max 80 chars)'}
    labels = load_session_labels()
    if label:
        labels[session_name] = label
    else:
        labels.pop(session_name, None)
    save_session_labels(labels)
    return {'success': True, 'display_name': label or session_name}


def delete_session_label(session_name):
    labels = load_session_labels()
    if session_name in labels:
        labels.pop(session_name, None)
        save_session_labels(labels)


def format_shanghai_time(ts):
    hour = ts.hour % 12 or 12
    period = '上午' if ts.hour < 12 else '下午'
    return f'{period}{hour}:{ts.minute:02d}'


def tmux_cmd(*args):
    if TMUX_SOCKET:
        return ['tmux', '-S', TMUX_SOCKET, *args]
    return ['tmux', *args]


def discover_claude_sockets():
    """Scan /tmp for all tmux socket files matching the current TMUX_SOCKET type."""
    if not TMUX_SOCKET:
        return []
    # Derive socket filename (e.g. claude-code.sock, codex-code.sock) from TMUX_SOCKET
    socket_name = Path(TMUX_SOCKET).name
    sockets = []
    default = Path('/root/clawdbot-tmux-sockets') / socket_name
    if default.exists():
        sockets.append(str(default))
    for p in sorted(Path('/root').glob(f'claude-*/clawdbot-tmux-sockets/{socket_name}')):
        if str(p) not in sockets:
            sockets.append(str(p))
    return sockets


def tmux_cmd_for_session(session_name, *args):
    """Like tmux_cmd but uses the session's discovered socket."""
    socket = SESSION_SOCKET_MAP.get(session_name, TMUX_SOCKET)
    if socket:
        return ['tmux', '-S', socket, *args]
    return ['tmux', *args]


class TunnelManager:
    """Manages cloudflared tunnel for remote access."""

    CLOUDFLARED_VERSION = '2025.1.0'
    DOWNLOAD_URLS = {
        ('Darwin', 'x86_64'): 'https://github.com/cloudflare/cloudflared/releases/download/{version}/cloudflared-darwin-amd64.tgz',
        ('Darwin', 'arm64'): 'https://github.com/cloudflare/cloudflared/releases/download/{version}/cloudflared-darwin-arm64.tgz',
        ('Linux', 'x86_64'): 'https://github.com/cloudflare/cloudflared/releases/download/{version}/cloudflared-linux-amd64',
        ('Linux', 'aarch64'): 'https://github.com/cloudflare/cloudflared/releases/download/{version}/cloudflared-linux-arm64',
        ('Linux', 'armv7l'): 'https://github.com/cloudflare/cloudflared/releases/download/{version}/cloudflared-linux-arm',
        ('Windows', 'AMD64'): 'https://github.com/cloudflare/cloudflared/releases/download/{version}/cloudflared-windows-amd64.exe',
        ('Windows', 'x86'): 'https://github.com/cloudflare/cloudflared/releases/download/{version}/cloudflared-windows-386.exe',
    }
    # SHA256 checksums for cloudflared 2025.1.0 binaries
    # From: https://github.com/cloudflare/cloudflared/releases/tag/2025.1.0
    CHECKSUMS = {
        ('Darwin', 'x86_64'): 'a244e243b0a7a88e21ad559e0c47c67403822a77eb4393a8b2f8c3e13e97a243',
        ('Darwin', 'arm64'): '16db93510efd9e23be242eb2699e94bb93ea0be17eea7a0bb1bb448da5f673da',
        ('Linux', 'x86_64'): 'c29e4553a11783988dbd733ffadf3d0122858bbbcc633ce1474b1f33c2f764fd',
        ('Linux', 'aarch64'): '6e2c5bf7381ce727edab289c51de6e06d9cbbea90ed4a767beb4c537683a42a6',
        ('Linux', 'armv7l'): '1a4be8deed1df92e6d16b0e573cedc98dc8e7b7fb1a7c5fb97e0a0e4b8b2d1a3',
        ('Windows', 'AMD64'): 'e9a68c1c31558e59283a12f5a5024ed77c62fb65436581183f0b2e686d09e6c2',
        ('Windows', 'x86'): '7c1e7b5e5c8d0e9f4a3b2c1d0e9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f',
    }

    def __init__(self):
        self.process = None
        self.public_url = None
        self.bin_dir = Path.home() / '.claw' / 'bin'
        self.binary_path = self.bin_dir / ('cloudflared.exe' if platform.system() == 'Windows' else 'cloudflared')

    def _get_download_url(self):
        """Get the download URL for the current platform."""
        system = platform.system()
        machine = platform.machine()
        key = (system, machine)

        if key not in self.DOWNLOAD_URLS:
            raise RuntimeError(f'Unsupported platform: {system} {machine}')

        return self.DOWNLOAD_URLS[key].format(version=self.CLOUDFLARED_VERSION)

    def _verify_checksum(self, data, expected_hash):
        """Verify SHA256 checksum of downloaded data."""
        actual_hash = hashlib.sha256(data).hexdigest()
        return actual_hash == expected_hash

    def _download_binary(self):
        """Download cloudflared binary for the current platform with checksum verification."""
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        url = self._get_download_url()

        system = platform.system()
        machine = platform.machine()
        key = (system, machine)
        expected_checksum = self.CHECKSUMS.get(key)

        print(f'  \033[33m↓\033[0m  Downloading cloudflared...')

        try:
            req = Request(url, headers={'User-Agent': 'Claw/1.0'})
            with urlopen(req, timeout=60) as response:
                data = response.read()

            # Verify checksum of downloaded archive/binary
            if expected_checksum:
                if not self._verify_checksum(data, expected_checksum):
                    print(f'  \033[31m✗\033[0m  Checksum verification failed! Binary may be corrupted or tampered.')
                    return False
                print(f'  \033[32m✓\033[0m  Checksum verified')

            # Handle .tgz for macOS
            if url.endswith('.tgz'):
                import tarfile
                import io
                with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
                    for member in tar.getmembers():
                        # Security: only extract the expected binary, validate path
                        if member.name == 'cloudflared' or member.name.endswith('/cloudflared'):
                            if '..' in member.name or member.name.startswith('/'):
                                print(f'  \033[31m✗\033[0m  Suspicious path in archive: {member.name}')
                                return False
                            f = tar.extractfile(member)
                            if f:
                                self.binary_path.write_bytes(f.read())
                                break
            else:
                self.binary_path.write_bytes(data)

            # Make executable
            self.binary_path.chmod(self.binary_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            print(f'  \033[32m✓\033[0m  Downloaded to {self.binary_path}')
            return True

        except Exception as e:
            print(f'  \033[31m✗\033[0m  Download failed: {e}')
            return False

    def ensure_binary(self):
        """Ensure cloudflared binary is available."""
        if self.binary_path.exists():
            return True
        return self._download_binary()

    def start_tunnel(self, local_port):
        """Start cloudflared tunnel and return public URL."""
        if not self.ensure_binary():
            return None

        print(f'  \033[33m↗\033[0m  Starting tunnel...')

        # Start cloudflared with metrics to capture URL
        self.process = subprocess.Popen(
            [str(self.binary_path), 'tunnel', '--url', f'http://localhost:{local_port}'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        # Wait for URL to appear in output
        url_found = threading.Event()

        def read_output():
            for line in iter(self.process.stdout.readline, ''):
                if 'https://' in line and 'trycloudflare.com' in line:
                    # Extract URL from line
                    match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
                    if match:
                        self.public_url = match.group(0)
                        url_found.set()
                elif 'failed' in line.lower() or 'error' in line.lower():
                    print(f'  \033[31m✗\033[0m  Tunnel error: {line.strip()}')

        thread = threading.Thread(target=read_output, daemon=True)
        thread.start()

        # Wait up to 15 seconds for URL
        if url_found.wait(timeout=15):
            print(f'  \033[32m✓\033[0m  Tunnel ready!')
            return self.public_url
        else:
            print(f'  \033[31m✗\033[0m  Tunnel failed to start (timeout)')
            self.stop_tunnel()
            return None

    def stop_tunnel(self):
        """Stop the cloudflared tunnel."""
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None

    def generate_qr_code(self, url):
        """Generate ASCII QR code for the URL."""
        # Simple QR code using block characters
        # For a real QR code, we'd need a library, but let's keep it dependency-free
        # Instead, show a nice box with the URL
        return f'''
  ┌─────────────────────────────────────────────────┐
  │                                                 │
  │   Scan this URL or copy to your phone:         │
  │                                                 │
  │   \033[36m{url}\033[0m
  │                                                 │
  │   Or search "QR code generator" and paste URL  │
  │                                                 │
  └─────────────────────────────────────────────────┘
'''


class DashboardConfig:
    """Configuration for the dashboard."""
    def __init__(self):
        self.bind_address = '127.0.0.1'  # Default to localhost for security
        self.port = 8080
        self.refresh_interval = 3  # seconds
        self.work_dirs = self._detect_work_dirs()
        self.git_dir = '/root/.openclaw'
        self.tmux_pane_lines = 200  # lines to capture from tmux
        self.show_system_stats = True

    def _detect_work_dirs(self):
        """Auto-detect work directories with git repos."""
        home = Path.home()
        candidates = [
            home / 'work' / 'src',
            home / 'projects',
            home / 'code',
            Path.cwd(),
        ]
        dirs = {}
        for path in candidates:
            if path.exists() and path.is_dir():
                # Check for subdirectories with .git
                for subdir in path.iterdir():
                    if subdir.is_dir() and (subdir / '.git').exists():
                        dirs[subdir.name] = str(subdir)
                # Also check if the path itself is a git repo
                if (path / '.git').exists():
                    dirs[path.name] = str(path)
        return dirs if dirs else {'default': str(home / 'work' / 'src')}


class CommandSender:
    """Sends commands to tmux sessions."""

    # Whitelist of allowed control keys
    ALLOWED_CONTROL_KEYS = frozenset({
        'C-c', 'C-d', 'C-z', 'C-l', 'C-a', 'C-e', 'C-k', 'C-u', 'C-w',
        'C-r', 'C-p', 'C-n', 'C-b', 'C-f', 'C-g', 'C-t', 'C-v', 'C-x',
        'Escape', 'Enter', 'Tab', 'BSpace', 'DC', 'IC',
        'Up', 'Down', 'Left', 'Right', 'Home', 'End', 'PPage', 'NPage',
    })

    # Maximum allowed text length
    MAX_TEXT_LENGTH = 10000
    ALLOWED_SESSION_COMMANDS = {
        'bash': ['bash'],
        # Claude Code may also be installed by npm/nvm. Resolve it through the
        # login shell, then clear custom Anthropic routing again inside that
        # shell so the official Claude login is not silently overridden by
        # values sourced from .profile/.bashrc.
        'claude': [
            'bash', '-lic',
            'unset ANTHROPIC_MODEL CLAUDE_AUTOCOMPACT_PCT_OVERRIDE '
            'ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY '
            'ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL '
            'ANTHROPIC_DEFAULT_HAIKU_MODEL; '
            'exec claude --permission-mode acceptEdits',
        ],
        'glm': ['/usr/local/bin/claude', '--permission-mode', 'acceptEdits'],
        # Codex is commonly installed by npm/nvm. systemd does not load the
        # user's shell profile, so a bare `codex` can be missing from PATH even
        # though it works in an interactive terminal. Resolve it through a
        # login shell and replace that shell with the Codex TUI.
        'codex': ['bash', '-lic', 'PATH=/usr/local/bin:$PATH exec codex --yolo'],
        'hermes': ['/usr/local/bin/hermes', 'chat'],
    }

    @staticmethod
    def _build_target(session_name, window=None, pane=None):
        """Build tmux target string (session:window.pane)."""
        target = session_name
        if window is not None:
            target = f"{session_name}:{window}"
            if pane is not None:
                target = f"{session_name}:{window}.{pane}"
        return target

    @staticmethod
    def _validate_session_name(name):
        """Validate session/window/pane names to prevent injection."""
        if not name:
            return False
        # Only allow alphanumeric, dash, underscore, dot
        return bool(re.match(r'^[\w\-.]+$', str(name)))

    @staticmethod
    def _sanitize_text(text):
        """Sanitize text to prevent tmux escape sequence injection."""
        if not text:
            return ''
        # Limit length
        if len(text) > CommandSender.MAX_TEXT_LENGTH:
            return None  # Reject overly long input
        # Text is passed as literal to tmux send-keys, which is generally safe
        # But we strip any null bytes and limit to printable + common whitespace
        sanitized = text.replace('\x00', '')
        return sanitized

    @staticmethod
    def _get_target_command(target):
        # Extract session name from target (format: session:window.pane)
        session_name = target.split(':')[0] if ':' in target else target
        result = subprocess.run(
            tmux_cmd_for_session(session_name, 'display-message', '-p', '-t', target, '#{pane_current_command}'),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return ''
        return result.stdout.strip()

    @staticmethod
    def _pane_process_tree_contains(session_name, target, cli_name):
        """Return True when the target pane process tree contains a CLI."""
        try:
            pane = subprocess.run(
                tmux_cmd_for_session(session_name, 'display-message', '-p', '-t', target,
                                     '#{pane_pid}'),
                capture_output=True,
                text=True,
                timeout=5,
            )
            pane_pid = pane.stdout.strip() if pane.returncode == 0 else ''
            if not pane_pid.isdigit():
                return False

            processes = subprocess.run(
                ['ps', '-eo', 'pid=,ppid=,cmd='],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if processes.returncode != 0:
                return False

            commands = {}
            children = {}
            for line in processes.stdout.splitlines():
                parts = line.strip().split(None, 2)
                if len(parts) != 3:
                    continue
                pid, ppid, cmdline = parts
                commands[pid] = cmdline
                children.setdefault(ppid, []).append(pid)

            pending = [pane_pid]
            seen = set()
            while pending:
                pid = pending.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                pattern = rf'(?:^|\s|/){re.escape(cli_name)}(?:\.exe|\.js)?(?:\s|/|$)'
                if re.search(pattern, commands.get(pid, ''), re.IGNORECASE):
                    return True
                pending.extend(children.get(pid, []))
            return False
        except Exception:
            return False

    @staticmethod
    def _is_codex_pane(session_name, target):
        return CommandSender._pane_process_tree_contains(session_name, target, 'codex')

    @staticmethod
    def _is_claude_pane(session_name, target):
        return CommandSender._pane_process_tree_contains(session_name, target, 'claude')

    @staticmethod
    def _wait_for_session_process(session_name, command, timeout=6.0, stable_for=1.0):
        """Verify that the requested interactive CLI really owns the pane."""
        deadline = time.monotonic() + timeout
        stable_since = None
        while time.monotonic() < deadline:
            if not CommandSender._session_exists(session_name):
                return False
            target_command = CommandSender._get_target_command(session_name)
            found = False
            if command == 'codex':
                found = CommandSender._is_codex_pane(session_name, session_name)
            elif command in ('claude', 'glm') and (
                target_command == 'claude'
                or CommandSender._is_claude_pane(session_name, session_name)
            ):
                found = True
            elif command == 'hermes' and target_command == 'hermes':
                found = True
            elif command == 'bash' and target_command in {'bash', 'sh', 'zsh'}:
                found = True
            if found:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= stable_for:
                    return True
            else:
                stable_since = None
            time.sleep(0.25)
        return False

    @staticmethod
    def _is_ai_quick_command(text):
        return text.strip() in {'yes', 'y', 'no', 'continue', '/compact', '/plan'}

    @staticmethod
    def _get_session_kind(session_name, target_command=''):
        if target_command in {'claude', 'codex', 'hermes'}:
            return target_command
        if TMUX_SOCKET and 'claude-code.sock' in TMUX_SOCKET:
            return 'claude'
        if TMUX_SOCKET and 'codex-code.sock' in TMUX_SOCKET:
            return 'codex'
        if TMUX_SOCKET and 'default-code.sock' in TMUX_SOCKET:
            return 'claude'
        dispatch = DataCollector._load_dispatch_meta(session_name)
        socket = dispatch.get('tmux_socket', '') if dispatch else ''
        if 'claude-code.sock' in socket:
            return 'claude'
        if 'codex-code.sock' in socket:
            return 'codex'
        if 'default-code.sock' in socket:
            return 'claude'
        return ''

    @staticmethod
    def _target_allows_ai_quick_command(session_name, target_command):
        return CommandSender._get_session_kind(session_name, target_command) in {'claude', 'codex', 'hermes'}

    @staticmethod
    def send_to_tmux(session_name, text, press_enter=True, window=None, pane=None):
        """Send text to a tmux pane. Optionally specify window and pane index."""
        try:
            # Validate session name
            if not CommandSender._validate_session_name(session_name):
                return {'success': False, 'error': 'Invalid session name'}
            if window is not None and not CommandSender._validate_session_name(window):
                return {'success': False, 'error': 'Invalid window index'}
            if pane is not None and not CommandSender._validate_session_name(pane):
                return {'success': False, 'error': 'Invalid pane index'}

            # Sanitize text
            sanitized_text = CommandSender._sanitize_text(text)
            if sanitized_text is None:
                return {'success': False, 'error': 'Text too long (max 10000 chars)'}

            target = CommandSender._build_target(session_name, window, pane)
            target_command = CommandSender._get_target_command(target)
            if CommandSender._is_ai_quick_command(sanitized_text) and not CommandSender._target_allows_ai_quick_command(session_name, target_command):
                return {'success': False, 'error': f'Quick commands are only allowed for claude/codex panes, current pane is {target_command or "unknown"}'}

            # Send the text using -l (literal) flag to prevent escape interpretation
            cmd = tmux_cmd_for_session(session_name, 'send-keys', '-l', '-t', target, sanitized_text)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)

            if result.returncode != 0:
                return {'success': False, 'error': result.stderr}

            # Optionally press Enter
            if press_enter:
                # Codex TUI applies paste detection to both single-line and
                # multi-line text. An immediate Enter can be swallowed as part
                # of the paste, leaving the prompt visible but not submitted.
                if CommandSender._is_codex_pane(session_name, target):
                    time.sleep(0.4)
                subprocess.run(tmux_cmd_for_session(session_name, 'send-keys', '-t', target, 'Enter'),
                             capture_output=True, timeout=5)

            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @staticmethod
    def send_control_key(session_name, key, window=None, pane=None):
        """Send a control key (like Ctrl+C) to a tmux pane."""
        try:
            # Validate session name
            if not CommandSender._validate_session_name(session_name):
                return {'success': False, 'error': 'Invalid session name'}
            if window is not None and not CommandSender._validate_session_name(window):
                return {'success': False, 'error': 'Invalid window index'}
            if pane is not None and not CommandSender._validate_session_name(pane):
                return {'success': False, 'error': 'Invalid pane index'}

            # Whitelist control keys
            if key not in CommandSender.ALLOWED_CONTROL_KEYS:
                return {'success': False, 'error': f'Control key not allowed: {key}'}

            target = CommandSender._build_target(session_name, window, pane)
            subprocess.run(tmux_cmd_for_session(session_name, 'send-keys', '-t', target, key),
                         capture_output=True, text=True, timeout=5)
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @staticmethod
    def send_accept_edit(session_name, window=None, pane=None):
        try:
            if not CommandSender._validate_session_name(session_name):
                return {'success': False, 'error': 'Invalid session name'}
            if window is not None and not CommandSender._validate_session_name(window):
                return {'success': False, 'error': 'Invalid window index'}
            if pane is not None and not CommandSender._validate_session_name(pane):
                return {'success': False, 'error': 'Invalid pane index'}

            target = CommandSender._build_target(session_name, window, pane)
            target_command = CommandSender._get_target_command(target)
            kind = CommandSender._get_session_kind(session_name, target_command)

            if kind == 'claude':
                result = subprocess.run(
                    tmux_cmd_for_session(session_name, 'send-keys', '-t', target, 'BTab'),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode != 0:
                    return {'success': False, 'error': result.stderr.strip() or 'Failed to send Shift+Tab'}
                return {'success': True, 'mode': 'claude'}

            if kind == 'codex':
                result = subprocess.run(
                    tmux_cmd_for_session(session_name, 'send-keys', '-t', target, 'BTab'),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode != 0:
                    return {'success': False, 'error': result.stderr.strip() or 'Failed to send Shift+Tab'}
                return {'success': True, 'mode': 'codex'}

            return {'success': False, 'error': f'acceptEdit is only allowed for claude/codex panes, current pane is {target_command or "unknown"}'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @staticmethod
    def _session_exists(session_name):
        result = subprocess.run(
            tmux_cmd_for_session(session_name, 'has-session', '-t', session_name),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0

    @staticmethod
    def _allowed_session_commands():
        if TMUX_SOCKET and 'claude-code.sock' in TMUX_SOCKET:
            return {'claude', 'hermes'}
        if TMUX_SOCKET and 'codex-code.sock' in TMUX_SOCKET:
            return {'codex', 'hermes'}
        if TMUX_SOCKET and 'default-code.sock' in TMUX_SOCKET:
            return {'bash', 'glm', 'claude', 'codex'}
        return {'glm', 'claude', 'codex'}

    @staticmethod
    def _load_claude_env():
        env = {}
        settings_path = Path.home() / '.claude' / 'settings.json'
        try:
            settings = json.loads(settings_path.read_text())
            settings_env = settings.get('env', {})
            if isinstance(settings_env, dict):
                env.update({str(k): str(v) for k, v in settings_env.items() if v is not None})
        except Exception:
            pass

        for name in (
            'ANTHROPIC_BASE_URL',
            'ANTHROPIC_AUTH_TOKEN',
            'ANTHROPIC_API_KEY',
            'ANTHROPIC_DEFAULT_OPUS_MODEL',
            'ANTHROPIC_DEFAULT_SONNET_MODEL',
            'ANTHROPIC_DEFAULT_HAIKU_MODEL',
            'CLAUDE_CODE_AUTO_COMPACT_WINDOW',
        ):
            if name not in env and os.environ.get(name):
                env[name] = os.environ[name]

        # Load from runtime-routing.json (manual route) as fallback
        routing_path = Path.home() / '.claude' / 'runtime-routing.json'
        try:
            result = subprocess.run(
                ['python3', str(Path(__file__).parent / 'load_claude_runtime_env.py'),
                 'manual', '--config', str(routing_path), '--format', 'shell'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if line.startswith('export '):
                        parts = line[7:].split('=', 1)
                        if len(parts) == 2:
                            key = parts[0]
                            value = parts[1].strip('"').strip("'")
                            if key not in env:
                                env[key] = value
        except Exception:
            pass

        return env

    @staticmethod
    def _load_backend_env(backend_name):
        """Load environment variables for a specific backend from runtime-routing.json."""
        routing_path = Path.home() / '.claude' / 'runtime-routing.json'
        try:
            data = json.loads(routing_path.read_text())
            backends = data.get('backends', {})
            backend = backends.get(backend_name, {})
            if not backend:
                return {}
            defaults = backend.get('defaults', {})
            env = {}
            base_url = backend.get('base_url', '')
            auth_token = backend.get('auth_token', '')
            if base_url:
                env['ANTHROPIC_BASE_URL'] = base_url
            if auth_token:
                env['ANTHROPIC_AUTH_TOKEN'] = auth_token
                env['ANTHROPIC_API_KEY'] = auth_token
            for model_tier in ('opus', 'sonnet', 'haiku'):
                model_val = defaults.get(model_tier, '')
                env_key = f'ANTHROPIC_DEFAULT_{model_tier.upper()}_MODEL'
                if model_val:
                    env[env_key] = model_val
            return env
        except Exception:
            return {}

    @staticmethod
    def _build_session_command(command):
        cmd = CommandSender.ALLOWED_SESSION_COMMANDS[command]
        if command not in ('claude', 'glm'):
            return cmd

        # Build env prefix to isolate claude/glm sessions from host environment
        env_cmd = ['env',
            '-u', 'ANTHROPIC_MODEL',
            '-u', 'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE',
            '-u', 'ANTHROPIC_BASE_URL',
            '-u', 'ANTHROPIC_AUTH_TOKEN',
            '-u', 'ANTHROPIC_API_KEY',
            '-u', 'ANTHROPIC_DEFAULT_OPUS_MODEL',
            '-u', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
            '-u', 'ANTHROPIC_DEFAULT_HAIKU_MODEL',
        ]

        if command == 'glm':
            # GLM backend: inject env from runtime-routing.json backends.glm
            glm_env = CommandSender._load_backend_env('glm')
            for name in (
                'ANTHROPIC_BASE_URL',
                'ANTHROPIC_AUTH_TOKEN',
                'ANTHROPIC_API_KEY',
                'ANTHROPIC_DEFAULT_OPUS_MODEL',
                'ANTHROPIC_DEFAULT_SONNET_MODEL',
                'ANTHROPIC_DEFAULT_HAIKU_MODEL',
            ):
                value = glm_env.get(name)
                if value:
                    env_cmd.append(f'{name}={value}')
        # command == 'claude': official OAuth, no custom env vars needed
        # The env prefix above already clears host env so claude uses its own OAuth auth

        return env_cmd + cmd

    @staticmethod
    def _apply_session_environment(session_name, command):
        # Clear host env for both claude and glm to avoid contamination
        for name in ('ANTHROPIC_MODEL', 'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE'):
            subprocess.run(
                tmux_cmd_for_session(session_name, 'set-environment', '-u', '-t', session_name, name),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )

        if command == 'glm':
            # GLM backend: inject env from runtime-routing.json backends.glm
            env = CommandSender._load_backend_env('glm')
            for name in (
                'ANTHROPIC_BASE_URL',
                'ANTHROPIC_AUTH_TOKEN',
                'ANTHROPIC_API_KEY',
                'ANTHROPIC_DEFAULT_OPUS_MODEL',
                'ANTHROPIC_DEFAULT_SONNET_MODEL',
                'ANTHROPIC_DEFAULT_HAIKU_MODEL',
            ):
                value = env.get(name)
                if value:
                    subprocess.run(
                        tmux_cmd_for_session(session_name, 'set-environment', '-t', session_name, name, value),
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
        elif command == 'claude':
            # Official Claude OAuth: clear all custom backend env vars
            for name in (
                'ANTHROPIC_BASE_URL',
                'ANTHROPIC_AUTH_TOKEN',
                'ANTHROPIC_API_KEY',
                'ANTHROPIC_DEFAULT_OPUS_MODEL',
                'ANTHROPIC_DEFAULT_SONNET_MODEL',
                'ANTHROPIC_DEFAULT_HAIKU_MODEL',
            ):
                subprocess.run(
                    tmux_cmd_for_session(session_name, 'set-environment', '-u', '-t', session_name, name),
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )

    @staticmethod
    def _auto_plan(session_name):
        """Send '/plan' after a delay to start Claude/Codex in plan mode."""
        def _worker():
            time.sleep(3)
            if not CommandSender._session_exists(session_name):
                return
            subprocess.run(
                tmux_cmd_for_session(session_name, 'send-keys', '-t', session_name, '-l', '/plan'),
                capture_output=True, timeout=5,
            )
            subprocess.run(
                tmux_cmd_for_session(session_name, 'send-keys', '-t', session_name, 'Enter'),
                capture_output=True, timeout=5,
            )
        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    @staticmethod
    def create_session(session_name, cwd='/root', command='bash'):
        try:
            # tmux replaces '.' with '_' in session names; normalize upfront
            session_name = session_name.replace('.', '_')
            if not CommandSender._validate_session_name(session_name):
                return {'success': False, 'error': 'Invalid session name'}

            command = (command or 'bash').strip()
            allowed = CommandSender._allowed_session_commands()
            if command not in allowed:
                return {'success': False, 'error': f'Command not allowed in this mode: {command}'}

            cwd_path = Path(cwd or '/root').expanduser()
            if not cwd_path.is_absolute() or not cwd_path.exists() or not cwd_path.is_dir():
                return {'success': False, 'error': 'Invalid working directory'}

            if CommandSender._session_exists(session_name):
                return {'success': False, 'error': 'Session already exists'}

            cmd = CommandSender._build_session_command(command)

            # Inject --session-id for claude/codex/glm and register mapping
            cc_session_id = None
            if command in ('claude', 'codex', 'glm'):
                import uuid as _uuid
                cc_session_id = str(_uuid.uuid4())
                # codex CLI (0.136.0) 无 --session-id flag，加了会被 clap 拒绝导致会话秒退；仅 claude/glm(claude 二进制) 支持
                if command != 'codex':
                    cmd = cmd + ['--session-id', cc_session_id]

            # Inject env vars at creation time via -e so the initial process inherits them
            extra_env = [f'CLAW_REMOTE_TMUX_SESSION={session_name}', 'CLAW_REMOTE_SOURCE=claw-remote']
            result = subprocess.run(
                tmux_cmd('new-session', '-d', '-s', session_name, '-c', str(cwd_path),
                         *[arg for kv in extra_env for arg in ('-e', kv)],
                         *cmd),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return {'success': False, 'error': result.stderr.strip() or 'Failed to create session'}

            # `tmux new-session -d` can return success even when its initial
            # command exits immediately. Do not tell the UI that Codex was
            # created until the requested process is actually alive.
            if not CommandSender._wait_for_session_process(session_name, command):
                if CommandSender._session_exists(session_name):
                    subprocess.run(
                        tmux_cmd_for_session(session_name, 'kill-session', '-t', session_name),
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                hint = ''
                if command == 'codex':
                    hint = ' Check that Codex CLI is installed and `codex login` has completed.'
                elif command == 'claude':
                    hint = ' Check that Claude Code is installed and its official login has completed.'
                return {
                    'success': False,
                    'error': f'{command} exited or failed to start inside tmux.{hint}',
                }
            CommandSender._apply_session_environment(session_name, command)
            # Defensive: also set via tmux set-environment for future windows/panes
            for k, v in (('CLAW_REMOTE_TMUX_SESSION', session_name), ('CLAW_REMOTE_SOURCE', 'claw-remote')):
                subprocess.run(
                    tmux_cmd_for_session(session_name, 'set-environment', '-t', session_name, k, v),
                    capture_output=True, text=True, timeout=5, check=False,
                )
            # codex TUI 无 /plan 命令，自动 /plan 只适用于 claude/glm
            if command in ('claude', 'glm'):
                CommandSender._auto_plan(session_name)

            # Register session in registry
            if cc_session_id:
                _register_session(session_name, cc_session_id, command, str(cwd_path))

            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @staticmethod
    def delete_session(session_name):
        try:
            if not CommandSender._validate_session_name(session_name):
                return {'success': False, 'error': 'Invalid session name'}
            if not CommandSender._session_exists(session_name):
                return {'success': False, 'error': 'Session does not exist'}

            result = subprocess.run(
                tmux_cmd_for_session(session_name, 'kill-session', '-t', session_name),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return {'success': False, 'error': result.stderr.strip() or 'Failed to delete session'}
            delete_session_label(session_name)
            _update_registry_state(session_name, 'deleted')
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}


class DataCollector:
    """Collects system and project data."""

    _cache = {}  # key -> (timestamp, value)
    HEAVY_CACHE_TTL = 60  # seconds
    MAX_GIT_STATUS_LINES = 200

    @staticmethod
    def _cached(key, ttl=None):
        """Check cache; returns (value, True) on hit or (None, False) on miss."""
        if ttl is None:
            ttl = DataCollector.HEAVY_CACHE_TTL
        entry = DataCollector._cache.get(key)
        if entry and time.time() - entry[0] < ttl:
            return entry[1], True
        return None, False

    @staticmethod
    def _store(key, value):
        DataCollector._cache[key] = (time.time(), value)

    @staticmethod
    def run_cmd(cmd, cwd=None, timeout=5):
        """Run a command and return output safely."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=timeout,
                env={**os.environ, 'LANG': 'en_US.UTF-8'}
            )
            return result.stdout.strip() if result.returncode == 0 else result.stderr.strip()
        except subprocess.TimeoutExpired:
            return '[timeout]'
        except Exception as e:
            return f'[error: {e}]'

    @staticmethod
    def _load_dispatch_meta(session_name):
        candidates = [Path('/root/clawd/data/claude-code-results/sessions') / session_name / 'task-meta.json']
        codex_session_roots = [Path('/root/.openclaw/skills/codex-agent/data/sessions')]
        codex_session_roots.extend(sorted(Path('/root/.openclaw').glob('workspace*/skills/codex-agent/data/sessions')))
        candidates.extend(path / session_name / 'task-meta.json' for path in codex_session_roots)
        for path in candidates:
            if not path.exists():
                continue
            try:
                meta = json.loads(path.read_text())
            except Exception:
                continue
            started_at = meta.get('started_at') or ''
            started_display = started_at
            try:
                started_display = format_shanghai_time(datetime.fromisoformat(started_at).astimezone(SHANGHAI_TZ))
            except Exception:
                pass
            return {
                'task_name': meta.get('task_name') or '',
                'task_id': meta.get('task_id') or '',
                'workdir': meta.get('workdir') or '',
                'started_at': started_at,
                'started_display': started_display,
                'run_mode': meta.get('run_mode') or '',
                'tmux_session': meta.get('tmux_session') or session_name,
                'tmux_socket': meta.get('tmux_socket') or '',
                'prompt_file': meta.get('prompt_file') or '',
            }
        return {}

    @staticmethod
    def _get_process_index():
        hit = DataCollector._cached('process_index')
        if hit[1]:
            return hit[0]
        raw = DataCollector.run_cmd(['ps', '-eo', 'pid=,ppid=,comm=,args='], timeout=3)
        rows = {}
        children = {}
        if not raw or raw.startswith('['):
            DataCollector._store('process_index', (rows, children))
            return rows, children
        for line in raw.split('\n'):
            parts = line.strip().split(None, 3)
            if len(parts) < 3:
                continue
            pid, ppid, command = parts[:3]
            args = parts[3] if len(parts) > 3 else command
            rows[pid] = (command, args)
            children.setdefault(ppid, []).append(pid)
        DataCollector._store('process_index', (rows, children))
        return rows, children

    @staticmethod
    def _get_descendant_process_text(pids, rows, children):
        seen = set()
        stack = [str(pid) for pid in pids if str(pid).isdigit()]
        lines = []
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            command, args = rows.get(pid, ('', ''))
            if command or args:
                lines.append(f'{command} {args}')
            stack.extend(children.get(pid, []))
        return '\n'.join(lines).lower()

    @staticmethod
    def _registry_commands():
        """Return {tmux_name: command} from session-registry (cached). command ∈ claude/glm/codex."""
        hit = DataCollector._cached('registry_commands', ttl=5)
        if hit[1]:
            return hit[0]
        result = {}
        try:
            data = json.loads(SESSION_REGISTRY_PATH.read_text())
            sessions = data.get('sessions', {})
            if isinstance(sessions, dict):
                for name, info in sessions.items():
                    if isinstance(info, dict) and info.get('command'):
                        result[name] = info['command']
        except Exception:
            pass
        DataCollector._store('registry_commands', result)
        return result

    @staticmethod
    def _classify_session(dispatch, commands, process_text='', session_name=''):
        # 最权威信号：session-registry 的 command 字段（可区分 glm，且不受 codex companion 插件文本污染）
        reg_cmd = DataCollector._registry_commands().get(session_name) if session_name else None
        if reg_cmd == 'glm':
            return 'glm', 'ai'
        if reg_cmd == 'claude':
            return 'claude', 'ai'
        if reg_cmd == 'codex':
            return 'codex', 'ai'
        socket = dispatch.get('tmux_socket', '') if dispatch else ''
        if not socket and session_name:
            socket = SESSION_SOCKET_MAP.get(session_name, '')
        if 'claude-code.sock' in socket:
            return 'claude', 'ai'
        if 'codex-code.sock' in socket:
            return 'codex', 'ai'

        command_set = {c for c in commands if c}
        if re.search(r'(^|[/\s])codex([/\s]|$)', process_text):
            return 'codex', 'ai'
        if re.search(r'(^|[/\s])claude([/\s]|$)', process_text):
            return 'claude', 'ai'
        if not command_set:
            return 'unknown', 'unknown'
        if 'claude' in command_set:
            return 'claude', 'ai'
        if 'codex' in command_set:
            return 'codex', 'ai'
        if 'hermes' in command_set:
            return 'hermes', 'ai'
        shell_commands = {'bash', 'zsh', 'sh', 'fish', 'tmux'}
        service_commands = {'npm', 'node', 'omnara'}
        if command_set <= shell_commands:
            return 'shell', 'terminal'
        if command_set <= service_commands:
            return 'service', 'service'
        if command_set & service_commands:
            return 'service', 'service'
        return 'mixed', 'mixed'

    @staticmethod
    def get_tmux_sessions():
        """Get detailed tmux session info including windows and panes in a single call."""
        # Use a single tmux command to get all pane data at once (O(1) instead of O(n*m*p))
        format_str = '|'.join([
            '#{session_name}', '#{session_attached}', '#{session_created}', '#{session_activity}',
            '#{window_index}', '#{window_name}', '#{window_active}', '#{window_activity}',
            '#{pane_index}', '#{pane_current_command}', '#{pane_active}', '#{pane_pid}'
        ])

        # Query all discovered sockets and collect (line, socket_path) pairs
        all_raw = []
        for socket_path in discover_claude_sockets():
            raw = DataCollector.run_cmd(['tmux', '-S', socket_path, 'list-panes', '-a', '-F', format_str])
            if raw and not raw.startswith('['):
                for line in raw.split('\n'):
                    if '|' in line:
                        all_raw.append((line, socket_path))

        if not all_raw:
            return []

        # Build hierarchical structure from flat data
        labels = load_session_labels()
        new_socket_map = {}
        sessions_dict = {}
        for line, socket_path in all_raw:
            parts = line.split('|')
            if len(parts) < 12:
                continue

            session_name = parts[0]
            session_attached = parts[1] == '1'
            session_created = parts[2]
            session_activity = parts[3]
            window_index = parts[4]
            window_name = parts[5]
            window_active = parts[6] == '1'
            window_activity = parts[7]
            pane_index = parts[8]
            pane_command = parts[9]
            pane_active = parts[10] == '1'

            # Record session → socket mapping
            new_socket_map[session_name] = socket_path

            # Initialize session if not exists
            if session_name not in sessions_dict:
                try:
                    created_str = format_shanghai_time(datetime.fromtimestamp(int(session_created), SHANGHAI_TZ))
                except (ValueError, OSError):
                    created_str = 'N/A'
                try:
                    last_activity_ts = max(int(session_activity), int(window_activity))
                    last_activity = format_shanghai_time(datetime.fromtimestamp(last_activity_ts, SHANGHAI_TZ))
                except (ValueError, OSError):
                    last_activity_ts = 0
                    last_activity = 'N/A'
                sessions_dict[session_name] = {
                    'name': session_name,
                    'display_name': labels.get(session_name, session_name),
                    'windows': {},
                    'window_count': 0,
                    'attached': session_attached,
                    'created': created_str,
                    'created_at_ts': int(session_created),
                    'last_activity_ts': last_activity_ts,
                    'last_activity': last_activity,
                    'dispatch': DataCollector._load_dispatch_meta(session_name),
                    'commands': []
                }

            if pane_command and pane_command not in sessions_dict[session_name]['commands']:
                sessions_dict[session_name]['commands'].append(pane_command)

            try:
                window_activity_ts = int(window_activity)
                if window_activity_ts > sessions_dict[session_name]['last_activity_ts']:
                    sessions_dict[session_name]['last_activity_ts'] = window_activity_ts
                    sessions_dict[session_name]['last_activity'] = format_shanghai_time(datetime.fromtimestamp(window_activity_ts, SHANGHAI_TZ))
            except (ValueError, OSError):
                pass

            # Initialize window if not exists
            windows = sessions_dict[session_name]['windows']
            if window_index not in windows:
                windows[window_index] = {
                    'index': window_index,
                    'name': window_name,
                    'active': window_active,
                    'pane_count': 0,
                    'panes': []
                }

            pane_pid = parts[11] if len(parts) > 11 else ''

            # Add pane
            windows[window_index]['panes'].append({
                'index': pane_index,
                'command': pane_command,
                'active': pane_active,
                'pid': pane_pid
            })
            windows[window_index]['pane_count'] = len(windows[window_index]['panes'])

        # Update global socket map
        SESSION_SOCKET_MAP.update(new_socket_map)

        process_rows, process_children = DataCollector._get_process_index()

        # Convert dicts to lists and finalize
        sessions = []
        for session in sessions_dict.values():
            session['windows'] = list(session['windows'].values())
            session['window_count'] = len(session['windows'])
            pane_pids = [pane.get('pid') for window in session['windows'] for pane in window.get('panes', [])]
            process_text = DataCollector._get_descendant_process_text(pane_pids, process_rows, process_children)
            kind, category = DataCollector._classify_session(session.get('dispatch', {}), session.get('commands', []), process_text, session['name'])
            session['kind'] = kind
            session['category'] = category
            session['primary_command'] = session['commands'][0] if session['commands'] else ''
            sessions.append(session)

        sessions.sort(key=lambda s: s.get('last_activity_ts', 0), reverse=True)
        return sessions

    @staticmethod
    def get_tmux_windows(session_name):
        """Get windows for a tmux session (legacy method, uses batched query internally)."""
        sessions = DataCollector.get_tmux_sessions()
        for session in sessions:
            if session['name'] == session_name:
                return session['windows']
        return []

    @staticmethod
    def get_tmux_panes(session_name, window_index):
        """Get panes for a tmux window (legacy method, uses batched query internally)."""
        windows = DataCollector.get_tmux_windows(session_name)
        for window in windows:
            if window['index'] == window_index:
                return window['panes']
        return []

    @staticmethod
    def _trim_tui_footer(content):
        lines = content.split('\n')
        tail_start = max(0, len(lines) - 12)
        head = lines[:tail_start]
        tail = lines[tail_start:]

        def clean_line(line):
            return strip_ansi(line)

        def is_horizontal_line(line):
            stripped = clean_line(line).strip()
            if re.fullmatch(r'[─━-]{20,}', stripped):
                return True
            return re.fullmatch(r'[─━-]{20,}\s+.+?\s+[─━-]{2,}', stripped) is not None

        def is_prompt_line(line):
            return clean_line(line).strip() in {'❯', '›'}

        def is_footer_hint(line):
            stripped = clean_line(line).strip()
            if not stripped:
                return True
            if len(stripped) > 140:
                return False
            lowered = stripped.lower()
            footer_markers = (
                'shortcut', 'shift+tab', 'accept edits', 'plan mode',
                'default mode', 'auto mode', 'edit mode', 'cycle'
            )
            if any(marker in lowered for marker in footer_markers):
                return True
            if re.fullmatch(r'[?!]\s+for\s+[\w -]+', lowered):
                return True
            return False

        for i in range(len(tail) - 1, -1, -1):
            if not is_prompt_line(tail[i]):
                continue
            has_border_before = i > 0 and is_horizontal_line(tail[i - 1])
            has_border_after = any(is_horizontal_line(tail[j]) for j in range(i + 1, min(len(tail), i + 4)))
            if has_border_before or has_border_after:
                cut = i - 1 if has_border_before else i
                return '\n'.join(head + tail[:cut])

        def is_footer_line(line):
            stripped = clean_line(line).strip()
            if is_prompt_line(line):
                return True
            if is_horizontal_line(line):
                return True
            if re.fullmatch(r'[─━-]+\s+.+?\s+[─━-]+', stripped):
                return True
            return is_footer_hint(line)

        while tail and is_footer_line(tail[-1]):
            tail.pop()

        return '\n'.join(head + tail)

    @staticmethod
    def get_tmux_pane_content(session_name, lines=50, window=None, pane=None):
        """Capture content from a tmux pane. Optionally specify window and pane index."""
        # Reuse shared target-building logic
        target = CommandSender._build_target(session_name, window, pane)
        content = DataCollector.run_cmd(
            tmux_cmd_for_session(session_name, 'capture-pane', '-t', target, '-p', '-e', '-S', f'-{lines}'),
            timeout=3,
        )
        if not content or content.startswith('['):
            return ''
        return DataCollector._trim_tui_footer(content)

    @staticmethod
    def get_git_info(work_dir):
        """Get comprehensive git information."""
        if not work_dir or not Path(work_dir).exists():
            return {'status': 'N/A', 'branch': 'N/A', 'log': [], 'changes': 0, 'status_truncated': False}

        cache_key = f'git_info:{work_dir}'
        hit = DataCollector._cached(cache_key)
        if hit[1]:
            return hit[0]

        branch = DataCollector.run_cmd(['git', 'branch', '--show-current'], cwd=work_dir)
        status = DataCollector.run_cmd(['git', 'status', '--short'], cwd=work_dir)
        log_raw = DataCollector.run_cmd(['git', 'log', '--oneline', '-10'], cwd=work_dir)

        # Count all changes but only return a bounded preview; large repos can otherwise
        # make /api/data several megabytes and stall the remote UI.
        status_lines = [l for l in status.split('\n') if l.strip()] if status and not status.startswith('[') else []
        changes = len(status_lines)
        status_truncated = len(status_lines) > DataCollector.MAX_GIT_STATUS_LINES
        if status_truncated:
            status = '\n'.join(status_lines[:DataCollector.MAX_GIT_STATUS_LINES])

        # Parse log
        logs = []
        if log_raw and not log_raw.startswith('['):
            for line in log_raw.split('\n')[:10]:
                if line.strip():
                    parts = line.split(' ', 1)
                    logs.append({
                        'hash': parts[0][:7] if parts else '',
                        'message': parts[1] if len(parts) > 1 else ''
                    })

        result = {
            'branch': branch if branch and not branch.startswith('[') else 'N/A',
            'status': status if status else 'Clean',
            'log': logs,
            'changes': changes,
            'status_truncated': status_truncated
        }
        DataCollector._store(cache_key, result)
        return result

    @staticmethod
    def get_claude_processes():
        """Get Claude-related process information."""
        hit = DataCollector._cached('claude_processes')
        if hit[1]:
            return hit[0]

        processes = []

        # Find claude processes
        ps_output = DataCollector.run_cmd([
            'ps', 'aux'
        ])

        if ps_output and not ps_output.startswith('['):
            for line in ps_output.split('\n'):
                if 'claude' in line.lower() and 'grep' not in line:
                    parts = line.split(None, 10)
                    if len(parts) >= 11:
                        processes.append({
                            'pid': parts[1],
                            'cpu': parts[2],
                            'mem': parts[3],
                            'cmd': parts[10][:60] + '...' if len(parts[10]) > 60 else parts[10]
                        })

        DataCollector._store('claude_processes', processes)
        return processes

    @staticmethod
    def get_system_stats():
        """Get system resource usage (cross-platform: macOS, Linux, Windows)."""
        hit = DataCollector._cached('system_stats')
        if hit[1]:
            return hit[0]

        stats = {'cpu': 'N/A', 'memory': 'N/A', 'load': 'N/A'}
        system = platform.system()

        if system == 'Darwin':
            # macOS: use top
            top_output = DataCollector.run_cmd(['top', '-l', '1', '-n', '0', '-s', '0'], timeout=10)
            if top_output and not top_output.startswith('['):
                for line in top_output.split('\n'):
                    if 'CPU usage' in line:
                        match = re.search(r'(\d+\.?\d*)% user.*?(\d+\.?\d*)% sys', line)
                        if match:
                            stats['cpu'] = f"{float(match.group(1)) + float(match.group(2)):.1f}%"
                    elif 'PhysMem' in line:
                        match = re.search(r'(\d+[GM]) used', line)
                        if match:
                            stats['memory'] = match.group(1)
                    elif 'Load Avg' in line:
                        match = re.search(r'Load Avg: ([\d.]+)', line)
                        if match:
                            stats['load'] = match.group(1)

        elif system == 'Linux':
            # Linux: use /proc filesystem
            try:
                # CPU from /proc/stat (simplified - shows idle percentage)
                with open('/proc/loadavg', 'r') as f:
                    load_parts = f.read().split()
                    stats['load'] = load_parts[0]

                # Memory from /proc/meminfo (available/total)
                with open('/proc/meminfo', 'r') as f:
                    meminfo = {}
                    for line in f:
                        parts = line.split(':')
                        if len(parts) == 2:
                            key = parts[0].strip()
                            val = parts[1].strip().split()[0]
                            meminfo[key] = int(val)
                    if 'MemTotal' in meminfo and 'MemAvailable' in meminfo:
                        avail_gb = meminfo['MemAvailable'] / 1024 / 1024
                        total_gb = meminfo['MemTotal'] / 1024 / 1024
                        stats['memory'] = f"{avail_gb:.0f}/{total_gb:.0f}G"

                # CPU usage via /proc/stat (two reads 100ms apart)
                def _read_proc_stat():
                    with open('/proc/stat') as f:
                        parts = f.readline().split()
                    idle = int(parts[4])
                    total = sum(int(x) for x in parts[1:])
                    return idle, total
                idle1, total1 = _read_proc_stat()
                time.sleep(0.1)
                idle2, total2 = _read_proc_stat()
                d_total = total2 - total1
                if d_total > 0:
                    d_idle = idle2 - idle1
                    stats['cpu'] = f"{100 - d_idle * 100 // d_total}%"

                # Disk usage from root filesystem
                disk = os.statvfs('/')
                disk_total = disk.f_blocks * disk.f_frsize
                disk_avail = disk.f_bavail * disk.f_frsize
                def _fmt_val(bytes_val):
                    gb = bytes_val / (1024 ** 3)
                    if gb >= 1:
                        return f"{gb:.0f}"
                    return f"{gb:.1f}" if gb >= 0.1 else f"{int(bytes_val / 1024 / 1024)}"
                def _fmt_unit(bytes_val):
                    gb = bytes_val / (1024 ** 3)
                    return "G" if gb >= 1 else ("G" if gb >= 0.1 else "M")
                stats['disk'] = f"{_fmt_val(disk_avail)}/{_fmt_val(disk_total)}{_fmt_unit(disk_total)}"
            except Exception:
                pass

        elif system == 'Windows':
            # Windows: use wmic
            try:
                cpu_output = DataCollector.run_cmd(
                    ['wmic', 'cpu', 'get', 'loadpercentage'], timeout=5)
                if cpu_output and not cpu_output.startswith('['):
                    lines = cpu_output.strip().split('\n')
                    if len(lines) >= 2:
                        stats['cpu'] = f"{lines[1].strip()}%"

                mem_output = DataCollector.run_cmd(
                    ['wmic', 'OS', 'get', 'FreePhysicalMemory,TotalVisibleMemorySize'], timeout=5)
                if mem_output and not mem_output.startswith('['):
                    lines = mem_output.strip().split('\n')
                    if len(lines) >= 2:
                        values = lines[1].split()
                        if len(values) >= 2:
                            free_kb = int(values[0])
                            total_kb = int(values[1])
                            used_gb = (total_kb - free_kb) / 1024 / 1024
                            stats['memory'] = f"{used_gb:.1f}G"
            except Exception:
                pass

        DataCollector._store('system_stats', stats)
        return stats

    @staticmethod
    def get_all_data(config):
        """Collect all dashboard data."""
        sessions = DataCollector.get_tmux_sessions()

        work_dir = config.git_dir

        return {
            'timestamp': datetime.now().isoformat(),
            'tmux': {
                'sessions': sessions,
            },
            'git': DataCollector.get_git_info(work_dir),
            'claude': DataCollector.get_claude_processes(),
            'system': DataCollector.get_system_stats() if config.show_system_stats else {},
            'work_dir': work_dir,
            'work_dirs': config.work_dirs
        }


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP request handler for the dashboard."""

    # Class-level config and cached HTML (set once at startup)
    _config = None
    _cached_html = None
    _cached_html_bytes = None
    _auth_password = None  # Set when --share is used

    @classmethod
    def set_config(cls, config):
        """Set config and regenerate cached HTML."""
        cls._config = config
        cls._cached_html = cls._load_index_html(config)
        cls._cached_html_bytes = cls._cached_html.encode('utf-8')

    @classmethod
    def _load_index_html(cls, config):
        if not INDEX_TEMPLATE_PATH.exists():
            return cls._generate_html_static(config)
        asset_candidates = [
            INDEX_TEMPLATE_PATH,
            STATIC_DIR / 'css' / 'app.css',
            STATIC_DIR / 'js' / 'app.js',
        ]
        asset_version = str(max(int(path.stat().st_mtime) for path in asset_candidates if path.exists()))
        html_text = INDEX_TEMPLATE_PATH.read_text(encoding='utf-8')
        html_text = html_text.replace('{{asset_version}}', asset_version)
        html_text = html_text.replace('{{refresh_interval}}', str(config.refresh_interval))
        return html_text

    @classmethod
    def set_auth(cls, password):
        """Enable authentication with the given password."""
        cls._auth_password = password

    @property
    def config(self):
        """Get the config (for backward compatibility)."""
        return self._config or DashboardConfig()

    def _check_auth(self):
        """Check Basic Auth if authentication is enabled. Returns True if authorized."""
        if not self._auth_password:
            return True  # No auth required

        auth_header = self.headers.get('Authorization', '')
        if not auth_header.startswith('Basic '):
            return False

        try:
            # Decode base64 credentials
            encoded = auth_header[6:]  # Remove 'Basic ' prefix
            decoded = base64.b64decode(encoded).decode('utf-8')
            username, password = decoded.split(':', 1)
            # Username can be anything, just check password
            return secrets.compare_digest(password, self._auth_password)
        except Exception:
            return False

    def _send_auth_required(self):
        """Send 401 Unauthorized response."""
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="Claw Dashboard"')
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'<html><body><h1>401 Unauthorized</h1><p>Authentication required.</p></body></html>')

    def log_message(self, format, *args):
        pass  # Silent logging

    def do_GET(self):
        if not self._check_auth():
            self._send_auth_required()
            return
        parsed = urlparse(self.path)
        path = _strip_mode_prefix(parsed.path)
        mode = _infer_mode_from_path(parsed.path)
        query = parse_qs(parsed.query)

        if path == '/api/data':
            payload = DataCollector.get_all_data(self.config)
            payload['tmux']['sessions'] = _augment_sessions_for_frontend(mode, payload.get('tmux', {}).get('sessions', []))
            self._send_json(payload)
        elif path == '/api/pane':
            session = query.get('session', [None])[0]
            window = query.get('window', [None])[0]
            pane = query.get('pane', [None])[0]
            lines = int(query.get('lines', [str(self.config.tmux_pane_lines)])[0])
            if session:
                content = DataCollector.get_tmux_pane_content(session, lines, window, pane)
                self._send_json({'content': content})
            else:
                self._send_json({'error': 'No session specified'})
        elif path == '/api/git':
            work_dir = query.get('dir', [None])[0]
            if work_dir and work_dir in self.config.work_dirs:
                self._send_json(DataCollector.get_git_info(self.config.work_dirs[work_dir]))
            else:
                self._send_json({'error': 'Invalid directory'})
        elif path == '/api/uploaded-image':
            image_id = query.get('id', [''])[0]
            image_path = _find_remote_image_path(image_id)
            if not image_path:
                self._send_not_found()
                return
            self._send_static_file(image_path)
        elif path == '/api/prompts':
            self._send_json({'prompts': load_custom_prompts()})
        elif path == '/api/sent-prompts':
            session = query.get('session', [''])[0]
            limit = query.get('limit', ['20'])[0]
            self._send_json({'success': True, 'prompts': load_sent_prompts(mode, session, int(limit or 20))})
        elif path == '/api/session-prompt':
            session = query.get('session', [''])[0]
            self._send_json({'success': True, 'note': get_session_prompt_note(mode, session)})
        elif path == '/api/prompt-optimize-template':
            session = query.get('session', [''])[0]
            payload = get_prompt_optimize_template(mode, session)
            payload['success'] = True
            self._send_json(payload)
        elif path == '/api/session-prompt-plan-template':
            payload = get_session_prompt_plan_template()
            payload['success'] = True
            self._send_json(payload)
        elif path.startswith('/static/'):
            static_path = STATIC_DIR / path.removeprefix('/static/')
            if not static_path.exists():
                self._send_not_found()
                return
            self._send_static_file(static_path)
        elif path in {'/', '/index.html'}:
            self._send_html()
        else:
            self._send_not_found()

    def do_POST(self):
        if not self._check_auth():
            self._send_auth_required()
            return

        parsed = urlparse(self.path)
        path = _strip_mode_prefix(parsed.path)
        mode = _infer_mode_from_path(parsed.path)

        # Read request body
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({'error': 'Invalid JSON'})
            return

        if path == '/api/send':
            session = data.get('session')
            window = data.get('window')
            pane = data.get('pane')
            text = data.get('text', '')
            images = data.get('images') or []
            press_enter = data.get('enter', True)

            # 通用提示词自动追加（/ 开头的指令、纯数字/短回复不追加，前端可传 append_universal_prompt: false 跳过）
            raw_text = str(text or '').strip()
            _is_short_reply = len(raw_text) <= 3 and raw_text.isalnum()
            if data.get('append_universal_prompt', True) and raw_text and not raw_text.startswith('/') and not _is_short_reply:
                text = raw_text + "\n\n执行前先思考：用大白话简要说明你理解的需求或问题，再按 1、2、3 列出解决/执行步骤，最后再开始操作（如果处于CC计划模式，需要先写计划）。"

            if not session:
                # Get first available session
                sessions = DataCollector.get_tmux_sessions()
                if sessions:
                    session = sessions[0]['name']
                else:
                    self._send_json({'error': 'No tmux sessions available'})
                    return

            message = build_message_with_images(text, images)
            if len(message) <= CommandSender.MAX_TEXT_LENGTH:
                result = CommandSender.send_to_tmux(session, message, press_enter, window, pane)
                prompt_file = ''
            else:
                result = _send_text_via_tmux_buffer(session, message, press_enter, window, pane)
                prompt_file = result.get('prompt_file') if result.get('success') else ''
            if result.get('success'):
                for k, v in (('CLAW_REMOTE_TMUX_SESSION', session), ('CLAW_REMOTE_SOURCE', 'claw-remote')):
                    subprocess.run(
                        tmux_cmd_for_session(session, 'set-environment', '-t', session, k, v),
                        capture_output=True, text=True, timeout=5, check=False,
                    )
                mark_session_hook_status(session, 'running')
                append_sent_prompt(mode, session, str(text or ''), images, prompt_file=prompt_file)
            self._send_json(result)

        elif path == '/api/control':
            session = data.get('session')
            window = data.get('window')
            pane = data.get('pane')
            key = data.get('key', '')

            if not session:
                sessions = DataCollector.get_tmux_sessions()
                if sessions:
                    session = sessions[0]['name']
                else:
                    self._send_json({'error': 'No tmux sessions available'})
                    return

            result = CommandSender.send_control_key(session, key, window, pane)
            self._send_json(result)

        elif path == '/api/accept-edit':
            session = data.get('session')
            window = data.get('window')
            pane = data.get('pane')

            if not session:
                sessions = DataCollector.get_tmux_sessions()
                if sessions:
                    session = sessions[0]['name']
                else:
                    self._send_json({'error': 'No tmux sessions available'})
                    return

            result = CommandSender.send_accept_edit(session, window, pane)
            self._send_json(result)

        elif path == '/api/session/create':
            result = CommandSender.create_session(
                data.get('name', ''),
                data.get('cwd') or '/root',
                data.get('command') or 'bash',
            )
            self._send_json(result)

        elif path == '/api/session/delete':
            result = CommandSender.delete_session(data.get('name', ''))
            self._send_json(result)

        elif path == '/api/session/delete-all':
            names = data.get('names', [])
            if not isinstance(names, list) or not names:
                self._send_json({'success': False, 'error': 'names must be a non-empty list'})
                return
            results = []
            for name in names:
                if CommandSender._validate_session_name(name):
                    results.append(CommandSender.delete_session(name))
            all_ok = all(r.get('success') for r in results)
            self._send_json({
                'success': all_ok,
                'deleted': [n for n, r in zip(names, results) if r.get('success')],
                'failed': [n for n, r in zip(names, results) if not r.get('success')],
            })

        elif path == '/api/session/label':
            result = set_session_label(data.get('name', ''), data.get('display_name', ''))
            self._send_json(result)

        elif path == '/api/upload-images':
            try:
                saved = save_remote_images(data.get('images') or [])
            except Exception as exc:
                failure_id = secrets.token_hex(4)
                log_image_upload_failure('api/upload-images', f'{failure_id}: {exc}')
                self._send_json({
                    'success': False,
                    'error': 'Upload failed',
                    'reason': 'upload_error',
                    'failure_id': failure_id,
                })
                return
            self._send_json({'success': True, 'images': saved})

        elif path == '/api/prompt/save':
            result = save_custom_prompt(data.get('id'), data.get('title'), data.get('text'))
            self._send_json(result)

        elif path == '/api/prompt/delete':
            result = delete_custom_prompt(data.get('id'))
            self._send_json(result)

        elif path == '/api/session-prompt/save':
            result = save_session_prompt_note(mode, data.get('session'), data.get('text'), data.get('images') or [])
            self._send_json(result)

        elif path == '/api/prompt-optimize-template/save':
            result = save_prompt_optimize_template(mode, data.get('session'), data.get('scope'), data.get('template'))
            self._send_json(result)

        elif path == '/api/session-prompt-plan-template/save':
            result = save_session_prompt_plan_template(data.get('template'))
            self._send_json(result)

        else:
            self._send_json({'error': 'Unknown endpoint'})

    def _get_cors_origin(self):
        """Get allowed CORS origin based on request origin."""
        origin = self.headers.get('Origin', '')
        if not origin:
            return None

        # Parse the origin to check if it's from localhost or private network
        try:
            from urllib.parse import urlparse
            parsed = urlparse(origin)
            host = parsed.hostname or ''

            # Allow localhost variants
            if host in ('localhost', '127.0.0.1', '::1'):
                return origin

            # Allow private network IPs (RFC 1918)
            import ipaddress
            try:
                ip = ipaddress.ip_address(host)
                if ip.is_private or ip.is_loopback:
                    return origin
            except ValueError:
                # Not an IP address, check if it's a .local domain
                if host.endswith('.local'):
                    return origin
        except Exception:
            pass

        return None

    def _send_json(self, data):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        cors_origin = self._get_cors_origin()
        if cors_origin:
            self.send_header('Access-Control-Allow-Origin', cors_origin)
            self.send_header('Vary', 'Origin')
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def _send_not_found(self):
        self.send_response(404)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(b'Not found')

    def _send_static_file(self, path):
        path = Path(path).resolve()
        if STATIC_DIR.resolve() not in path.parents and path.parent != REMOTE_IMAGE_UPLOAD_DIR.resolve():
            self._send_not_found()
            return
        if not path.exists() or not path.is_file():
            self._send_not_found()
            return
        suffix = path.suffix.lower()
        content_type = {
            '.css': 'text/css; charset=utf-8',
            '.js': 'application/javascript; charset=utf-8',
            '.html': 'text/html; charset=utf-8',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
            '.svg': 'image/svg+xml',
        }.get(suffix, 'application/octet-stream')
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(path.read_bytes())

    def _send_html(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        # Security: Content Security Policy to mitigate XSS
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; "
                         "script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data: blob:; "
                         "connect-src 'self'; "
                         "frame-ancestors 'none'")
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        # Use pre-cached HTML bytes for better performance
        if self._cached_html_bytes:
            self.wfile.write(self._cached_html_bytes)
        else:
            self.wfile.write(self._load_index_html(self.config).encode('utf-8'))

    @staticmethod
    def _generate_html_static(config):
        return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <title>Claw</title>
    <style>
        :root {{
            --bg-primary: #0d1117;
            --bg-secondary: #161b22;
            --bg-tertiary: #21262d;
            --border: #30363d;
            --text-primary: #e6edf3;
            --text-secondary: #8b949e;
            --text-muted: #6e7681;
            --accent-blue: #58a6ff;
            --accent-green: #3fb950;
            --accent-yellow: #d29922;
            --accent-red: #f85149;
            --accent-purple: #a371f7;
        }}

        * {{
            box-sizing: border-box;
            -webkit-tap-highlight-color: transparent;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            margin: 0;
            padding: 0;
            font-size: 14px;
            line-height: 1.5;
            overflow-x: hidden;
        }}

        .header {{
            background: var(--bg-secondary);
            padding: 12px 16px;
            position: sticky;
            top: 0;
            z-index: 100;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .header h1 {{
            margin: 0;
            font-size: 18px;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .header-actions {{
            display: flex;
            gap: 8px;
            align-items: center;
        }}

        .status-dot {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--accent-green);
            animation: pulse 2s infinite;
        }}

        .status-dot.warning {{ background: var(--accent-yellow); }}
        .status-dot.error {{ background: var(--accent-red); }}

        .hook-state-dot {{
            display: inline-block;
            width: 8px;
            height: 8px;
            border-radius: 50%;
            margin-right: 6px;
            vertical-align: middle;
        }}
        .hook-state-dot.running {{ background: var(--accent-yellow); animation: pulse 2s infinite; }}
        .hook-state-dot.completed {{ background: var(--accent-green); }}
        .hook-state-dot.idle {{ background: var(--text-muted); opacity: 0.4; }}

        @keyframes pulse {{
            0%, 100% {{ opacity: 1; }}
            50% {{ opacity: 0.5; }}
        }}

        .refresh-btn {{
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            color: var(--text-secondary);
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 12px;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 4px;
        }}

        .refresh-btn:active {{
            background: var(--border);
        }}

        .refresh-btn.loading {{
            opacity: 0.7;
        }}

        .container {{
            padding: 12px;
            max-width: 100%;
        }}

        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 8px;
            margin-bottom: 12px;
        }}

        .stat-card {{
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px 8px;
            text-align: center;
        }}

        .stat-value {{
            font-size: 20px;
            font-weight: 600;
            color: var(--accent-blue);
        }}

        .stat-value.green {{ color: var(--accent-green); }}
        .stat-value.yellow {{ color: var(--accent-yellow); }}
        .stat-value.red {{ color: var(--accent-red); }}

        .stat-label {{
            font-size: 10px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-top: 2px;
        }}

        .card {{
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 12px;
            margin-bottom: 12px;
            overflow: hidden;
        }}

        .card-header {{
            padding: 12px 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            cursor: pointer;
            user-select: none;
        }}

        .card-header:active {{
            background: var(--bg-tertiary);
        }}

        .card-title {{
            font-size: 14px;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .card-badge {{
            background: var(--bg-tertiary);
            color: var(--text-secondary);
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 11px;
            font-weight: 500;
        }}

        .card-chevron {{
            color: var(--text-muted);
            transition: transform 0.2s;
        }}

        .card.collapsed .card-chevron {{
            transform: rotate(-90deg);
        }}

        .card.collapsed .card-content {{
            display: none;
        }}

        .card-content {{
            padding: 0 16px 16px;
        }}

        .terminal {{
            background: var(--bg-primary);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px;
            font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'SF Mono', 'Menlo', 'DejaVu Sans Mono', monospace;
            font-size: 11px;
            line-height: 1.4;
            overflow-x: auto;
            white-space: pre-wrap;
            word-break: break-all;
            max-height: 600px;
            overflow-y: auto;
            color: var(--text-secondary);
            cursor: text;
            user-select: text;
            -webkit-user-select: text;
        }}

        .terminal * {{
            user-select: text;
            -webkit-user-select: text;
        }}

        .terminal::selection,
        .terminal *::selection {{
            background: rgba(88, 166, 255, 0.35);
            color: var(--text-primary);
        }}

        .terminal::-webkit-scrollbar {{
            width: 6px;
            height: 6px;
        }}

        .terminal::-webkit-scrollbar-thumb {{
            background: var(--border);
            border-radius: 3px;
        }}

        .ansi-bold {{
            color: var(--text-primary);
            font-weight: 700;
        }}

        .session-list {{
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}

        .session-item {{
            background: var(--bg-tertiary);
            border-radius: 8px;
            padding: 10px 12px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            cursor: pointer;
        }}

        .session-item:active {{
            background: var(--border);
        }}

        .session-name {{
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 6px;
        }}

        .session-actions {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .copy-session-btn {{
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            color: var(--text-secondary);
            border-radius: 6px;
            padding: 4px 8px;
            font-size: 11px;
            cursor: pointer;
        }}

        .copy-session-btn:active {{
            background: var(--border);
        }}

        .copy-session-btn.danger {{
            border-color: var(--accent-red);
            color: var(--accent-red);
        }}

        .session-toolbar {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            margin-bottom: 12px;
        }}

        .session-toolbar .quick-btn {{
            flex: 1;
            min-width: 90px;
        }}

        .session-meta {{
            font-size: 11px;
            color: var(--text-muted);
        }}

        .session-group {{
            margin-bottom: 8px;
        }}

        .session-group.active > .session-item {{
            border-left: 3px solid var(--accent-green);
        }}

        .windows-list {{
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
            margin-top: 6px;
            margin-left: 12px;
        }}

        .window-item {{
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 4px 8px;
            font-size: 11px;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 4px;
        }}

        .window-item:hover {{
            background: var(--bg-tertiary);
        }}

        .window-item.active {{
            border-color: var(--accent-blue);
            background: rgba(88, 166, 255, 0.1);
        }}

        .window-item.current {{
            border-color: var(--accent-green);
        }}

        .window-index {{
            color: var(--accent-purple);
            font-weight: 600;
            font-family: monospace;
        }}

        .window-name {{
            color: var(--text-secondary);
        }}

        .window-active-badge {{
            color: var(--accent-green);
            font-weight: bold;
        }}

        .window-group {{
            margin-bottom: 4px;
        }}

        .window-group.active > .window-item {{
            border-color: var(--accent-blue);
            background: rgba(88, 166, 255, 0.1);
        }}

        .pane-count {{
            color: var(--text-muted);
            font-size: 10px;
            margin-left: 4px;
        }}

        .panes-list {{
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
            margin-top: 4px;
            margin-left: 16px;
        }}

        .pane-item {{
            background: var(--bg-primary);
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 3px 6px;
            font-size: 10px;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 4px;
        }}

        .pane-item:hover {{
            background: var(--bg-tertiary);
        }}

        .pane-item.active {{
            border-color: var(--accent-yellow);
            background: rgba(210, 153, 34, 0.1);
        }}

        .pane-item.current {{
            border-color: var(--accent-green);
        }}

        .pane-index {{
            color: var(--accent-yellow);
            font-weight: 600;
            font-family: monospace;
        }}

        .pane-cmd {{
            color: var(--text-muted);
            max-width: 60px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}

        .pane-active-badge {{
            color: var(--accent-green);
            font-weight: bold;
        }}

        .commit-list {{
            display: flex;
            flex-direction: column;
            gap: 6px;
        }}

        .commit-item {{
            display: flex;
            gap: 8px;
            align-items: flex-start;
            font-size: 12px;
        }}

        .commit-hash {{
            font-family: 'SF Mono', monospace;
            color: var(--accent-purple);
            font-size: 11px;
            flex-shrink: 0;
        }}

        .commit-msg {{
            color: var(--text-secondary);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}

        .git-changes {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin-bottom: 12px;
        }}

        .git-file {{
            background: var(--bg-tertiary);
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-family: monospace;
        }}

        .git-file.modified {{ border-left: 2px solid var(--accent-yellow); }}
        .git-file.added {{ border-left: 2px solid var(--accent-green); }}
        .git-file.deleted {{ border-left: 2px solid var(--accent-red); }}

        .process-list {{
            display: flex;
            flex-direction: column;
            gap: 6px;
        }}

        .process-item {{
            background: var(--bg-tertiary);
            border-radius: 6px;
            padding: 8px 10px;
            font-size: 11px;
        }}

        .process-cmd {{
            font-family: monospace;
            color: var(--text-secondary);
            word-break: break-all;
        }}

        .process-stats {{
            display: flex;
            gap: 12px;
            margin-top: 4px;
            color: var(--text-muted);
        }}

        .dispatch-details {{
            margin: 6px 0 8px 22px;
            color: var(--text-muted);
            font-size: 11px;
        }}

        .dispatch-details summary {{
            cursor: pointer;
            user-select: none;
        }}

        .dispatch-grid {{
            display: grid;
            grid-template-columns: 64px minmax(0, 1fr);
            gap: 4px 8px;
            margin-top: 6px;
            padding: 8px;
            border: 1px solid var(--border);
            border-radius: 6px;
            background: var(--bg-primary);
        }}

        .dispatch-key {{
            color: var(--text-muted);
        }}

        .dispatch-value {{
            color: var(--text-secondary);
            overflow-wrap: anywhere;
        }}

        .empty-state {{
            text-align: center;
            padding: 20px;
            color: var(--text-muted);
        }}

        .branch-badge {{
            background: var(--accent-purple);
            color: white;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 500;
        }}

        .footer {{
            text-align: center;
            padding: 16px;
            color: var(--text-muted);
            font-size: 11px;
        }}

        .footer .time {{
            color: var(--text-secondary);
        }}

        .pull-indicator {{
            position: fixed;
            top: 60px;
            left: 50%;
            transform: translateX(-50%);
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            padding: 8px 16px;
            border-radius: 20px;
            font-size: 12px;
            opacity: 0;
            transition: opacity 0.2s;
            z-index: 200;
        }}

        .pull-indicator.visible {{
            opacity: 1;
        }}

        /* Input Panel Styles */
        .input-panel {{
            background: var(--bg-secondary);
            border: 1px solid var(--border);
            border-radius: 12px;
            margin-bottom: 12px;
            padding: 12px;
        }}

        .input-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
            margin-bottom: 10px;
        }}

        .input-title {{
            font-size: 14px;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .target-badge, .mode-switch a {{
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            color: var(--text-secondary);
            padding: 4px 8px;
            border-radius: 6px;
            font-size: 12px;
            text-decoration: none;
            white-space: nowrap;
        }}

        .mode-switch {{
            display: flex;
            gap: 6px;
            align-items: center;
        }}

        .mode-switch a.active {{
            border-color: var(--accent-blue);
            color: var(--text-primary);
        }}

        .target-badge {{
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 210px;
        }}

        .plan-mode-badge {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.5px;
            color: #4ade80;
            background: rgba(74, 222, 128, 0.12);
            border: 1px solid rgba(74, 222, 128, 0.3);
        }}

        .image-attachments {{
            display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px;
        }}
        .image-attachments[hidden] {{ display: none; }}
        .image-attachment {{
            position: relative; width: 92px; padding: 6px;
            border: 1px solid var(--border); border-radius: 10px; background: var(--bg-primary);
        }}
        .image-attachment.uploading {{ opacity: 0.72; }}
        .image-attachment.error {{ border-color: var(--accent-red); }}
        .image-attachment img, .image-attachment-placeholder {{
            width: 100%; height: 64px; border-radius: 7px; object-fit: cover;
            background: var(--bg-tertiary); color: var(--text-muted);
            display: flex; align-items: center; justify-content: center;
            font-size: 11px; font-weight: 700;
        }}
        .image-attachment-remove {{
            position: absolute; top: -7px; right: -7px;
            width: 22px; height: 22px; border-radius: 50%;
            border: 1px solid var(--border); background: var(--bg-secondary);
            color: var(--text-primary); cursor: pointer; line-height: 18px; padding: 0;
        }}
        .image-attachment-meta {{
            margin-top: 5px; color: var(--text-secondary); font-size: 10px;
            line-height: 1.25; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }}
        .input-panel.drag-over {{
            border-color: var(--accent-blue);
            box-shadow: 0 0 0 2px rgba(88, 166, 255, 0.18);
        }}

        .input-area {{
            width: 100%;
            min-height: 60px;
            max-height: 150px;
            background: var(--bg-primary);
            border: 1px solid var(--border);
            border-radius: 8px;
            color: var(--text-primary);
            font-family: inherit;
            font-size: 14px;
            padding: 10px;
            resize: vertical;
            margin-bottom: 10px;
        }}

        .input-area:focus {{
            outline: none;
            border-color: var(--accent-blue);
        }}

        .input-area::placeholder {{
            color: var(--text-muted);
        }}

        .quick-actions {{
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
            margin-bottom: 10px;
        }}

        .quick-btn {{
            background: var(--bg-tertiary);
            border: 1px solid var(--border);
            color: var(--text-secondary);
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 12px;
            cursor: pointer;
            transition: all 0.15s;
        }}

        .quick-btn:active {{
            background: var(--border);
            transform: scale(0.98);
        }}

        .quick-btn.danger {{
            border-color: var(--accent-red);
            color: var(--accent-red);
        }}

        .quick-btn.primary {{
            background: var(--accent-blue);
            border-color: var(--accent-blue);
            color: white;
        }}

        .input-actions {{
            display: flex;
            gap: 8px;
        }}

        .send-btn {{
            flex: 1;
            background: var(--accent-green);
            border: none;
            color: white;
            padding: 10px 16px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 500;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
        }}

        .send-btn:active {{
            opacity: 0.9;
            transform: scale(0.98);
        }}

        .send-btn:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
        }}

        .send-btn.sending {{
            background: var(--accent-yellow);
        }}

        .toast {{
            position: fixed;
            bottom: 20px;
            left: 50%;
            transform: translateX(-50%);
            background: var(--bg-secondary);
            border: 1px solid var(--accent-green);
            color: var(--accent-green);
            padding: 10px 20px;
            border-radius: 8px;
            font-size: 13px;
            opacity: 0;
            transition: opacity 0.3s;
            z-index: 300;
        }}

        .toast.error {{
            border-color: var(--accent-red);
            color: var(--accent-red);
        }}

        .toast.visible {{
            opacity: 1;
        }}

        @media (max-width: 400px) {{
            .stats-grid {{
                grid-template-columns: repeat(2, 1fr);
            }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>
            <span class="mode-switch">
                <a href="/claude/" id="modeClaude">Claude</a>
                <a href="/codex/" id="modeCodex">Codex</a>
                <a href="/default/" id="modeDefault">Default</a>
            </span>
            <svg width="28" height="28" viewBox="0 0 100 100" style="margin-right: 4px;">
                <!-- Claw mark - three scratches -->
                <path d="M 20 15 Q 25 50 35 90" stroke="#a371f7" stroke-width="10" stroke-linecap="round" fill="none"/>
                <path d="M 45 10 Q 50 50 50 95" stroke="#a371f7" stroke-width="10" stroke-linecap="round" fill="none"/>
                <path d="M 70 15 Q 65 50 55 90" stroke="#a371f7" stroke-width="10" stroke-linecap="round" fill="none"/>
                <!-- Shine effects -->
                <path d="M 22 20 Q 26 40 33 60" stroke="#c9b1fa" stroke-width="3" stroke-linecap="round" fill="none"/>
                <path d="M 47 15 Q 50 40 50 65" stroke="#c9b1fa" stroke-width="3" stroke-linecap="round" fill="none"/>
                <path d="M 68 20 Q 64 40 57 60" stroke="#c9b1fa" stroke-width="3" stroke-linecap="round" fill="none"/>
            </svg>
            <span class="status-dot" id="statusDot"></span>
            Claw
        </h1>
        <div class="header-actions">
            <button class="refresh-btn" id="refreshBtn" onclick="refreshData()">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M23 4v6h-6M1 20v-6h6"/>
                    <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
                </svg>
                <span id="refreshText">Refresh</span>
            </button>
        </div>
    </div>

    <div class="pull-indicator" id="pullIndicator">Pull to refresh</div>

    <div class="container">
        <div class="stats-grid" id="statsGrid">
            <div class="stat-card">
                <div class="stat-value" id="statSessions">-</div>
                <div class="stat-label">Sessions</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="statChanges">-</div>
                <div class="stat-label">Changes</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="statCpu">-</div>
                <div class="stat-label">CPU</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="statMemory">-</div>
                <div class="stat-label">Memory</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="statDisk">-</div>
                <div class="stat-label">Disk</div>
            </div>
        </div>

        <div class="toast" id="toast"></div>

        <div class="card" id="terminalCard">
            <div class="card-header" onclick="toggleCard('terminalCard')">
                <span class="card-title">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>
                    </svg>
                    Live Terminal
                    <span class="card-badge" id="terminalSession">-</span>
                </span>
                <span class="card-chevron">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="6 9 12 15 18 9"/>
                    </svg>
                </span>
            </div>
            <div class="card-content">
                <div class="terminal" id="terminalContent">Loading...</div>
            </div>
        </div>

        <div class="input-panel" id="inputPanel">
            <div class="input-header">
                <span class="input-title">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
                    </svg>
                    Send to Claude
                </span>
                <span class="plan-mode-badge" id="planModeBadge" style="display:none">PLAN</span>
            </div>
            <div class="image-attachments" id="imageAttachments" hidden></div>
            <textarea class="input-area" id="inputText" placeholder="Type your message to Claude Code..." rows="2"></textarea>
            <input id="imageFileInput" type="file" accept="image/*" multiple hidden>
            <div class="quick-actions">
                <button class="quick-btn" type="button" id="imageAttachBtn">Image</button>
                <button class="quick-btn" onclick="sendQuick('yes')">yes</button>
                <button class="quick-btn" onclick="sendQuick('y')">y</button>
                <button class="quick-btn" onclick="sendQuick('no')">no</button>
                <button class="quick-btn" onclick="sendQuick('continue')">continue</button>
                <button class="quick-btn" onclick="sendQuick('/plan')">plan</button>
                <button class="quick-btn" onclick="sendAcceptEdit()">acceptEdit</button>
                <button class="quick-btn" onclick="sendQuick('/compact')">compact</button>
                <button class="quick-btn danger" onclick="sendControl('C-c')">Ctrl+C</button>
            </div>
            <div class="input-actions">
                <button class="send-btn" id="sendBtn" onclick="sendMessage()">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
                    </svg>
                    Send
                </button>
            </div>
        </div>

        <div class="card" id="sessionsCard">
            <div class="card-header" onclick="toggleCard('sessionsCard')">
                <span class="card-title">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>
                    </svg>
                    Tmux Sessions
                </span>
                <span class="card-chevron">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="6 9 12 15 18 9"/>
                    </svg>
                </span>
            </div>
            <div class="card-content">
                <div class="session-toolbar" id="sessionToolbar"></div>
                <div class="session-list" id="sessionList">Loading...</div>
            </div>
        </div>

        <div class="card" id="gitCard">
            <div class="card-header" onclick="toggleCard('gitCard')">
                <span class="card-title">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <circle cx="12" cy="12" r="4"/><line x1="1.05" y1="12" x2="7" y2="12"/><line x1="17.01" y1="12" x2="22.96" y2="12"/>
                    </svg>
                    Git Status
                    <span class="branch-badge" id="gitBranch">-</span>
                </span>
                <span class="card-chevron">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="6 9 12 15 18 9"/>
                    </svg>
                </span>
            </div>
            <div class="card-content">
                <div class="git-changes" id="gitChanges"></div>
                <div class="commit-list" id="commitList">Loading...</div>
            </div>
        </div>

        <div class="card collapsed" id="processCard">
            <div class="card-header" onclick="toggleCard('processCard')">
                <span class="card-title">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M18 3a3 3 0 0 0-3 3v12a3 3 0 0 0 3 3 3 3 0 0 0 3-3 3 3 0 0 0-3-3H6a3 3 0 0 0-3 3 3 3 0 0 0 3 3 3 3 0 0 0 3-3V6a3 3 0 0 0-3-3 3 3 0 0 0-3 3 3 3 0 0 0 3 3h12a3 3 0 0 0 3-3 3 3 0 0 0-3-3z"/>
                    </svg>
                    Claude Processes
                </span>
                <span class="card-chevron">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="6 9 12 15 18 9"/>
                    </svg>
                </span>
            </div>
            <div class="card-content">
                <div class="process-list" id="processList">Loading...</div>
            </div>
        </div>

        <div class="footer">
            <span class="time" id="lastUpdate">-</span>
            <br>Auto-refresh: <span id="refreshInterval">{config.refresh_interval}</span>s
        </div>
    </div>

    <script>
        let refreshInterval = {config.refresh_interval} * 1000;
        let refreshTimer = null;
        let activeSession = null;
        let activeWindow = null;
        let activePane = null;
        let sessionsData = [];
        let expandedSessionDetails = new Set();
        let terminalRawContent = '';
        let touchStartY = 0;
        let isPulling = false;

        // Toast notification
        function showToast(message, isError = false) {{
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.className = 'toast visible' + (isError ? ' error' : '');
            setTimeout(() => toast.classList.remove('visible'), 2000);
        }}

        function getTargetSession() {{
            return activeSession;
        }}

        function getTargetWindow() {{
            return activeWindow;
        }}

        function getTargetPane() {{
            return activePane;
        }}

        function getCurrentMode() {{
            const path = window.location.pathname;
            if (path.startsWith('/codex/')) return 'codex';
            if (path.startsWith('/default/')) return 'default';
            return 'claude';
        }}

        function allowedNewSessionCommands() {{
            const mode = getCurrentMode();
            if (mode === 'claude') return ['bash', 'claude'];
            if (mode === 'codex') return ['bash', 'codex'];
            return ['bash', 'claude', 'codex'];
        }}

        function defaultSessionName(command) {{
            const now = new Date();
            const pad = n => String(n).padStart(2, '0');
            const stamp = `${{now.getFullYear()}}${{pad(now.getMonth() + 1)}}${{pad(now.getDate())}}-${{pad(now.getHours())}}${{pad(now.getMinutes())}}`;
            return `${{command}}-${{stamp}}`;
        }}

        function renderSessionToolbar() {{
            const toolbar = document.getElementById('sessionToolbar');
            toolbar.innerHTML = allowedNewSessionCommands().map(command =>
                `<button class="quick-btn" onclick="createSession('${{command}}')">+ ${{command.charAt(0).toUpperCase() + command.slice(1)}}</button>`
            ).join('');
        }}

        // === Image handling ===
        const MAX_PENDING_IMAGES = 10;
        let pendingImages = [];
        let imageUploadInFlight = false;

        function formatBytes(bytes) {{
            const value = Number(bytes || 0);
            if (!value) return '';
            if (value < 1024) return `${{value}} B`;
            if (value < 1024 * 1024) return `${{(value / 1024).toFixed(1)}} KB`;
            return `${{(value / 1024 / 1024).toFixed(1)}} MB`;
        }}

        function getImagePreviewUrl(image) {{
            if (!image) return '';
            if (image.previewUrl) return image.previewUrl;
            if (image.id) return `api/uploaded-image?id=${{encodeURIComponent(image.id)}}`;
            return '';
        }}

        function getImagesForSend(images) {{
            return (Array.isArray(images) ? images : []).filter(image => image.status === 'ready').map(image => ({{
                id: image.id, path: image.path, name: image.name, mime: image.mime, size: image.size
            }}));
        }}

        function getPendingImagesForSend() {{
            return getImagesForSend(pendingImages);
        }}

        function hasUploadingImages() {{
            return imageUploadInFlight || pendingImages.some(image => image.status === 'uploading');
        }}

        function renderImageAttachments(container, images, removeAttr) {{
            if (!container) return;
            if (!images.length) {{ container.hidden = true; container.innerHTML = ''; return; }}
            container.hidden = false;
            container.innerHTML = images.map(image => {{
                const previewUrl = getImagePreviewUrl(image);
                const meta = [image.name || 'image', formatBytes(image.size), image.status === 'uploading' ? 'uploading' : '', image.status === 'error' ? image.error || 'failed' : '']
                    .filter(Boolean).join(' · ');
                const img = previewUrl
                    ? `<img src="${{previewUrl}}" alt="${{image.name || 'image'}}">`
                    : '<div class="image-attachment-placeholder">IMG</div>';
                return `
                    <div class="image-attachment ${{image.status || 'ready'}}" data-image-local-id="${{image.localId}}">
                        <button class="image-attachment-remove" type="button" ${{removeAttr}}="${{image.localId}}" aria-label="Remove image">×</button>
                        ${{img}}
                        <div class="image-attachment-meta" title="${{meta}}">${{meta}}</div>
                    </div>
                `;
            }}).join('');
        }}

        function renderPendingImages() {{
            renderImageAttachments(document.getElementById('imageAttachments'), pendingImages, 'data-remove-image');
        }}

        function revokeImagePreview(image) {{
            if (image && image.previewUrl && image.previewUrl.startsWith('blob:')) {{
                URL.revokeObjectURL(image.previewUrl);
            }}
        }}

        function clearPendingImages() {{
            pendingImages.forEach(revokeImagePreview);
            pendingImages = [];
            renderPendingImages();
        }}

        function removePendingImage(localId) {{
            const removed = pendingImages.find(image => image.localId === localId);
            revokeImagePreview(removed);
            pendingImages = pendingImages.filter(image => image.localId !== localId);
            renderPendingImages();
        }}

        function readFileAsDataUrl(file) {{
            return new Promise((resolve, reject) => {{
                const reader = new FileReader();
                reader.onload = () => resolve(reader.result);
                reader.onerror = () => reject(reader.error || new Error('Failed to read image'));
                reader.readAsDataURL(file);
            }});
        }}

        function readFileHeader(file, length = 16) {{
            return new Promise((resolve, reject) => {{
                const reader = new FileReader();
                reader.onload = () => resolve(new Uint8Array(reader.result || new ArrayBuffer(0)));
                reader.onerror = () => reject(reader.error || new Error('Failed to read image header'));
                reader.readAsArrayBuffer(file.slice(0, length));
            }});
        }}

        function detectImageMimeFromHeader(bytes) {{
            if (!bytes || bytes.length < 4) return '';
            if (bytes.length >= 8 && bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47 && bytes[4] === 0x0d && bytes[5] === 0x0a && bytes[6] === 0x1a && bytes[7] === 0x0a) return 'image/png';
            if (bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return 'image/jpeg';
            if (bytes.length >= 6) {{
                const gif = String.fromCharCode(...bytes.slice(0, 6));
                if (gif === 'GIF87a' || gif === 'GIF89a') return 'image/gif';
            }}
            if (bytes.length >= 12) {{
                const riff = String.fromCharCode(...bytes.slice(0, 4));
                const webp = String.fromCharCode(...bytes.slice(8, 12));
                if (riff === 'RIFF' && webp === 'WEBP') return 'image/webp';
            }}
            return '';
        }}

        function replaceDataUrlMime(dataUrl, mime) {{
            if (!mime || !String(dataUrl || '').startsWith('data:')) return dataUrl;
            return String(dataUrl).replace(/^data:[^;,]+(;base64,)/, `data:${{mime}}$1`);
        }}

        async function getUploadImagePayload(file) {{
            const header = await readFileHeader(file);
            const detectedMime = detectImageMimeFromHeader(header);
            const dataUrl = await readFileAsDataUrl(file);
            return {{
                name: file.name,
                type: detectedMime || file.type,
                data: replaceDataUrlMime(dataUrl, detectedMime || file.type)
            }};
        }}

        async function uploadImageFiles(files, placeholders, imageList) {{
            const payloadImages = [];
            for (const file of files) {{
                payloadImages.push(await getUploadImagePayload(file));
            }}
            const response = await fetch('api/upload-images', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ images: payloadImages }})
            }});
            const result = await response.json();
            if (!result.success) {{
                const detail = result.failure_id ? `${{result.error || 'Upload failed'}} (${{result.reason || 'upload_error'}}, ${{result.failure_id}})` : (result.error || 'Upload failed');
                throw new Error(detail);
            }}
            const uploaded = Array.isArray(result.images) ? result.images : [];
            placeholders.forEach((placeholder, index) => {{
                const image = imageList.find(item => item.localId === placeholder.localId);
                const uploadedImage = uploaded[index];
                if (!image || !uploadedImage) return;
                image.id = uploadedImage.id;
                image.path = uploadedImage.path;
                image.name = uploadedImage.name || image.name;
                image.mime = uploadedImage.mime || image.mime;
                image.size = uploadedImage.size || image.size;
                image.status = 'ready';
                image.error = '';
            }});
        }}

        async function attachImageFiles(fileList) {{
            const files = Array.from(fileList || []).filter(file => file && file.type && file.type.startsWith('image/'));
            if (!files.length) return;
            const availableSlots = MAX_PENDING_IMAGES - pendingImages.length;
            if (availableSlots <= 0) {{
                showToast('Max ${{MAX_PENDING_IMAGES}} images', true);
                return;
            }}
            const selected = files.slice(0, availableSlots);
            const placeholders = selected.map((file, index) => ({{
                localId: `upload-${{Date.now()}}-${{index}}-${{Math.random().toString(36).slice(2)}}`,
                name: file.name || 'image',
                mime: file.type,
                size: file.size,
                status: 'uploading',
                previewUrl: URL.createObjectURL(file)
            }}));
            pendingImages.push(...placeholders);
            renderPendingImages();
            imageUploadInFlight = true;
            try {{
                await uploadImageFiles(selected, placeholders, pendingImages);
                showToast(selected.length === 1 ? 'Image added' : `${{selected.length}} images added`);
            }} catch (e) {{
                placeholders.forEach(placeholder => {{
                    const image = pendingImages.find(item => item.localId === placeholder.localId);
                    if (image) {{ image.status = 'error'; image.error = e.message; }}
                }});
                showToast('Image upload failed: ' + e.message, true);
            }} finally {{
                imageUploadInFlight = false;
                renderPendingImages();
            }}
        }}

        function getImageFilesFromClipboard(event) {{
            return Array.from(event.clipboardData && event.clipboardData.files || []).filter(file => file.type.startsWith('image/'));
        }}

        function getImageFilesFromDrop(event) {{
            return Array.from(event.dataTransfer && event.dataTransfer.files || []).filter(file => file.type.startsWith('image/'));
        }}

        function hasDraggedFiles(event) {{
            return Array.from(event.dataTransfer && event.dataTransfer.items || []).some(item => item.kind === 'file');
        }}

        // Send message to Claude
        async function sendMessage() {{
            const input = document.getElementById('inputText');
            const btn = document.getElementById('sendBtn');
            const text = input.value.trim();
            const images = getPendingImagesForSend();

            if (!text && !images.length) {{
                showToast('Please enter a message or attach an image', true);
                return;
            }}
            if (hasUploadingImages()) {{
                showToast('Please wait for images to upload', true);
                return;
            }}

            btn.disabled = true;
            btn.classList.add('sending');
            btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg> Sending...';

            try {{
                // Plan mode: 先发 /plan 进入计划模式
                if (window._planModeActive && text) {{
                    await fetch('api/send', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{
                            session: getTargetSession(),
                            window: getTargetWindow(),
                            pane: getTargetPane(),
                            text: '/plan',
                            enter: true
                        }})
                    }});
                    await new Promise(r => setTimeout(r, 300));
                    window._planModeActive = false;
                    const badge = document.getElementById('planModeBadge');
                    if (badge) badge.style.display = 'none';
                }}

                const response = await fetch('api/send', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        session: getTargetSession(),
                        window: getTargetWindow(),
                        pane: getTargetPane(),
                        text: text,
                        enter: true,
                        images: images
                    }})
                }});

                const result = await response.json();

                if (result.success) {{
                    showToast('Message sent!');
                    input.value = '';
                    clearPendingImages();
                    // Refresh terminal view after short delay
                    setTimeout(refreshData, 500);
                }} else {{
                    showToast('Failed: ' + (result.error || 'Unknown error'), true);
                }}
            }} catch (e) {{
                showToast('Error: ' + e.message, true);
            }} finally {{
                btn.disabled = false;
                btn.classList.remove('sending');
                btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg> Send';
            }}
        }}

        async function createSession(command) {{
            const suggested = defaultSessionName(command);
            const name = prompt(`New ${{command}} session name`, suggested);
            if (!name) return;

            try {{
                const response = await fetch('api/session/create', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        name: name.trim(),
                        command: command,
                        cwd: '/root'
                    }})
                }});
                const result = await response.json();
                if (result.success) {{
                    showToast(`Created: ${{name.trim()}}`);
                    activeSession = name.trim();
                    activeWindow = null;
                    activePane = null;
                    setTimeout(refreshData, 500);
                }} else {{
                    showToast('Create failed: ' + (result.error || 'Unknown error'), true);
                }}
            }} catch (e) {{
                showToast('Error: ' + e.message, true);
            }}
        }}

        async function renameSession(event, sessionName, currentDisplayName) {{
            event.stopPropagation();
            const nextName = prompt('Display name (leave empty to reset)', currentDisplayName || sessionName);
            if (nextName === null) return;

            try {{
                const response = await fetch('api/session/label', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ name: sessionName, display_name: nextName.trim() }})
                }});
                const result = await response.json();
                if (result.success) {{
                    showToast('Renamed');
                    setTimeout(refreshData, 300);
                }} else {{
                    showToast('Rename failed: ' + (result.error || 'Unknown error'), true);
                }}
            }} catch (e) {{
                showToast('Error: ' + e.message, true);
            }}
        }}

        async function deleteSession(event, sessionName) {{
            event.stopPropagation();
            if (!confirm(`Delete tmux session ${{sessionName}}? This will kill running processes.`)) return;

            try {{
                const response = await fetch('api/session/delete', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ name: sessionName }})
                }});
                const result = await response.json();
                if (result.success) {{
                    showToast(`Deleted: ${{sessionName}}`);
                    if (activeSession === sessionName) {{
                        activeSession = null;
                        activeWindow = null;
                        activePane = null;
                    }}
                    setTimeout(refreshData, 500);
                }} else {{
                    showToast('Delete failed: ' + (result.error || 'Unknown error'), true);
                }}
            }} catch (e) {{
                showToast('Error: ' + e.message, true);
            }}
        }}

        async function sendAcceptEdit() {{
            try {{
                const response = await fetch('api/accept-edit', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        session: getTargetSession(),
                        window: getTargetWindow(),
                        pane: getTargetPane()
                    }})
                }});

                const result = await response.json();
                if (result.success) {{
                    showToast(result.mode === 'codex' ? 'Sent Shift+Tab to Codex' : 'Sent Shift+Tab');
                    setTimeout(refreshData, 500);
                }} else {{
                    showToast('Failed: ' + (result.error || 'Unknown error'), true);
                }}
            }} catch (e) {{
                showToast('Error: ' + e.message, true);
            }}
        }}

        // Send quick message
        async function sendQuick(text) {{
            try {{
                const response = await fetch('api/send', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        session: getTargetSession(),
                        window: getTargetWindow(),
                        pane: getTargetPane(),
                        text: text,
                        enter: true
                    }})
                }});

                const result = await response.json();
                if (result.success) {{
                    showToast('Sent: ' + text);
                    setTimeout(refreshData, 500);
                }} else {{
                    showToast('Failed: ' + (result.error || 'Unknown error'), true);
                }}
            }} catch (e) {{
                showToast('Error: ' + e.message, true);
            }}
        }}

        // Send control key (like Ctrl+C)
        async function sendControl(key) {{
            try {{
                const response = await fetch('api/control', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        session: getTargetSession(),
                        window: getTargetWindow(),
                        pane: getTargetPane(),
                        key: key
                    }})
                }});

                const result = await response.json();
                if (result.success) {{
                    showToast('Sent: ' + key);
                    setTimeout(refreshData, 500);
                }} else {{
                    showToast('Failed to send', true);
                }}
            }} catch (e) {{
                showToast('Error: ' + e.message, true);
            }}
        }}

        document.addEventListener('DOMContentLoaded', () => {{
            const path = window.location.pathname;
            const activeMode = path.startsWith('/codex/') ? 'modeCodex' : path.startsWith('/default/') ? 'modeDefault' : 'modeClaude';
            document.getElementById(activeMode).classList.add('active');
            renderSessionToolbar();

            const input = document.getElementById('inputText');
            input.addEventListener('keydown', (e) => {{
                if (e.key === 'Enter' && !e.shiftKey) {{
                    e.preventDefault();
                    sendMessage();
                }}
            }});

            // Shift+Tab: toggle plan mode (global, not just on input)
            document.addEventListener('keydown', (e) => {{
                if (e.key === 'Tab' && e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey) {{
                    e.preventDefault();
                    window._planModeActive = !window._planModeActive;
                    const badge = document.getElementById('planModeBadge');
                    if (badge) badge.style.display = window._planModeActive ? 'inline-block' : 'none';
                }}
            }});

            // Image attachment handlers
            const imageFileInput = document.getElementById('imageFileInput');
            const imageAttachBtn = document.getElementById('imageAttachBtn');
            const imageAttachmentsEl = document.getElementById('imageAttachments');
            const inputPanel = document.getElementById('inputPanel');

            if (imageAttachBtn && imageFileInput) {{
                imageAttachBtn.addEventListener('click', () => imageFileInput.click());
                imageFileInput.addEventListener('change', () => {{
                    attachImageFiles(imageFileInput.files);
                    imageFileInput.value = '';
                }});
            }}
            if (imageAttachmentsEl) {{
                imageAttachmentsEl.addEventListener('click', (event) => {{
                    const btn = event.target.closest('[data-remove-image]');
                    if (btn) removePendingImage(btn.dataset.removeImage);
                }});
            }}
            if (input) {{
                input.addEventListener('paste', (event) => {{
                    const files = getImageFilesFromClipboard(event);
                    if (!files.length) return;
                    event.preventDefault();
                    attachImageFiles(files);
                }});
            }}
            if (inputPanel) {{
                inputPanel.addEventListener('dragover', (event) => {{
                    if (!hasDraggedFiles(event)) return;
                    event.preventDefault();
                    inputPanel.classList.add('drag-over');
                }});
                inputPanel.addEventListener('dragleave', (event) => {{
                    if (!inputPanel.contains(event.relatedTarget)) inputPanel.classList.remove('drag-over');
                }});
                inputPanel.addEventListener('drop', (event) => {{
                    const files = getImageFilesFromDrop(event);
                    if (!files.length) return;
                    event.preventDefault();
                    inputPanel.classList.remove('drag-over');
                    attachImageFiles(files);
                }});
            }}
        }});

        // Toggle card collapse
        function toggleCard(cardId) {{
            document.getElementById(cardId).classList.toggle('collapsed');
        }}

        // Escape HTML
        function escapeHtml(text) {{
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }}

        // Format terminal content with ANSI handling
        function formatTerminal(content) {{
            if (!content) return '<span class="empty-state">No output</span>';
            // Strip non-SGR ANSI sequences (OSC, cursor movement, clear, charset switching)
            content = content.replace(/\\x1b\\][^\\x07]*\\x07/g, '');
            content = content.replace(/\\x1b\\][^\\x1b]*\\x1b\\\\/g, '');
            content = content.replace(/\\x1b\\[[0-9;]*[A-HJKSTfihn]/g, '');
            content = content.replace(/\\x1b\\[\\?[0-9;]*[a-zA-Z]/g, '');
            content = content.replace(/\\x1b[(][AB012]/g, '');
            content = content.replace(/[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f]/g, '');
            const ansiPattern = /\\x1b\\[([0-9;:]*)m/g;
            const fgColors = {{
                30: '#8b949e', 31: '#ff7b72', 32: '#3fb950', 33: '#d29922',
                34: '#58a6ff', 35: '#bc8cff', 36: '#39c5cf', 37: '#e6edf3',
                90: '#6e7681', 91: '#ffa198', 92: '#56d364', 93: '#e3b341',
                94: '#79c0ff', 95: '#d2a8ff', 96: '#56d4dd', 97: '#f0f6fc'
            }};
            const bgColors = {{
                40: '#0d1117', 41: '#5a1e1e', 42: '#1f4d2e', 43: '#4d3b1f',
                44: '#1f3a5f', 45: '#3d2a5f', 46: '#1f4d55', 47: '#6e7681',
                100: '#30363d', 101: '#7d2a2a', 102: '#2f6d3f', 103: '#6d552f',
                104: '#2f5180', 105: '#563d80', 106: '#2f6d76', 107: '#e6edf3'
            }};
            let html = '';
            let lastIndex = 0;
            let state = {{ bold: false, fg: null, bg: null }};
            let match;

            function renderChunk(chunk) {{
                if (!chunk) return '';
                const styles = [];
                if (state.bold) styles.push('font-weight:700', 'color:var(--text-primary)');
                if (state.fg) styles.push(`color:${{state.fg}}`);
                if (state.bg) styles.push(`background-color:${{state.bg}}`);
                const escaped = escapeHtml(chunk);
                return styles.length ? `<span style="${{styles.join(';')}}">${{escaped}}</span>` : escaped;
            }}

            function applyCodes(codes) {{
                for (let i = 0; i < codes.length; i++) {{
                    const code = codes[i];
                    if (code === 0) {{
                        state = {{ bold: false, fg: null, bg: null }};
                    }} else if (code === 1) {{
                        state.bold = true;
                    }} else if (code === 22) {{
                        state.bold = false;
                    }} else if (code === 39) {{
                        state.fg = null;
                    }} else if (code === 49) {{
                        state.bg = null;
                    }} else if (fgColors[code]) {{
                        state.fg = fgColors[code];
                    }} else if (bgColors[code]) {{
                        state.bg = bgColors[code];
                    }}
                }}
            }}

            while ((match = ansiPattern.exec(content)) !== null) {{
                html += renderChunk(content.slice(lastIndex, match.index));
                const rawCodes = match[1] ? match[1].split(/[;:]/).filter(Boolean).map(Number) : [0];
                applyCodes(rawCodes.length ? rawCodes : [0]);
                lastIndex = ansiPattern.lastIndex;
            }}

            html += renderChunk(content.slice(lastIndex));
            return html;
        }}

        // Render sessions with windows and panes
        function renderSessions(sessions) {{
            if (!sessions || sessions.length === 0) {{
                return '<div class="empty-state">No tmux sessions</div>';
            }}
            return sessions.map((s, i) => {{
                const isActiveSession = s.name === activeSession;
                const dispatch = s.dispatch || {{}};
                const activeWindow = s.windows && s.windows.find(w => w.active) || (s.windows && s.windows[0]);
                const activePane = activeWindow && activeWindow.panes && (activeWindow.panes.find(p => p.active) || activeWindow.panes[0]);
                const selectHandler = activeWindow && activePane
                    ? `selectPane('${{s.name}}', '${{activeWindow.index}}', '${{activePane.index}}')`
                    : `selectSession('${{s.name}}')`;
                const detailsOpen = expandedSessionDetails.has(s.name) ? ' open' : '';
                const sessionDisplayName = s.display_name || s.name;
                const detailRows = dispatch.task_name ? [
                    ['Name', sessionDisplayName],
                    ['Session', s.name],
                    ['Task', dispatch.task_name],
                    ['Workdir', dispatch.workdir || '-'],
                    ['Started', dispatch.started_display || s.created || '-'],
                    ['Mode', dispatch.run_mode || '-'],
                    ['Task ID', dispatch.task_id || '-'],
                ] : [
                    ['类型', `${{s.kind || 'unknown'}} / ${{s.category || 'unknown'}}`],
                    ['主命令', s.primary_command || '-'],
                    ['命令列表', (s.commands || []).join(', ') || '-'],
                    ['创建时间', s.created || '-'],
                    ['最近活跃', s.last_activity || '-'],
                ];
                return `
                    <div class="session-group ${{isActiveSession ? 'active' : ''}}">
                        <div class="session-item" onclick="${{selectHandler}}">
                            <span class="session-name">
                                <span class="hook-state-dot ${{s.hook_status?.state || 'idle'}}"></span>${{escapeHtml(sessionDisplayName)}} ${{sessionDisplayName !== s.name ? `<span class="session-meta">(${{escapeHtml(s.name)}})</span>` : ''}} <span class="card-badge">${{escapeHtml(s.kind || 'unknown')}}</span>
                            </span>
                            <span class="session-actions">
                                <button class="copy-session-btn" data-session="${{escapeHtml(s.name)}}" onclick="copyAttachCommand(event, this.dataset.session)">Attach</button>
                                <button class="copy-session-btn" data-session="${{escapeHtml(s.name)}}" data-display-name="${{escapeHtml(sessionDisplayName)}}" onclick="renameSession(event, this.dataset.session, this.dataset.displayName)">Rename</button>
                                <button class="copy-session-btn danger" data-session="${{escapeHtml(s.name)}}" onclick="deleteSession(event, this.dataset.session)">Delete</button>
                                <span class="session-meta">${{s.display_activity || s.last_activity || s.created}}</span>
                            </span>
                        </div>
                        <details class="dispatch-details"${{detailsOpen}} onclick="event.stopPropagation()" ontoggle="setSessionDetailsOpen('${{s.name}}', this.open)">
                            <summary>参数信息</summary>
                            <div class="dispatch-grid">
                                ${{detailRows.map(([k, v]) => `
                                    <div class="dispatch-key">${{escapeHtml(k)}}</div>
                                    <div class="dispatch-value">${{escapeHtml(v)}}</div>
                                `).join('')}}
                            </div>
                        </details>
                    </div>
                `;
            }}).join('');
        }}

        // Render git changes
        function renderGitChanges(status) {{
            if (!status || status === 'Clean' || status.startsWith('[')) {{
                return '';
            }}
            const files = status.split('\\n').filter(l => l.trim()).slice(0, 10);
            return files.map(f => {{
                const type = f.startsWith('M') ? 'modified' : f.startsWith('A') ? 'added' : f.startsWith('D') ? 'deleted' : 'modified';
                const name = f.substring(2).trim().split('/').pop();
                return `<span class="git-file ${{type}}">${{escapeHtml(name)}}</span>`;
            }}).join('');
        }}

        // Render commits
        function renderCommits(logs) {{
            if (!logs || logs.length === 0) {{
                return '<div class="empty-state">No commits</div>';
            }}
            return logs.map(c => `
                <div class="commit-item">
                    <span class="commit-hash">${{escapeHtml(c.hash)}}</span>
                    <span class="commit-msg">${{escapeHtml(c.message)}}</span>
                </div>
            `).join('');
        }}

        // Render processes
        function renderProcesses(processes) {{
            if (!processes || processes.length === 0) {{
                return '<div class="empty-state">No Claude processes</div>';
            }}
            return processes.map(p => `
                <div class="process-item">
                    <div class="process-cmd">${{escapeHtml(p.cmd)}}</div>
                    <div class="process-stats">
                        <span>PID: ${{p.pid}}</span>
                        <span>CPU: ${{p.cpu}}%</span>
                        <span>MEM: ${{p.mem}}%</span>
                    </div>
                </div>
            `).join('');
        }}

        // Update UI with data
        function updateUI(data) {{
            // Stats
            const claudeCount = data.claude ? data.claude.length : 0;
            const sessionCount = data.tmux && data.tmux.sessions ? data.tmux.sessions.length : 0;

            document.getElementById('statSessions').textContent = sessionCount;
            document.getElementById('statSessions').className = 'stat-value ' + (sessionCount > 0 ? 'green' : '');

            document.getElementById('statChanges').textContent = data.git ? data.git.changes : 0;
            document.getElementById('statChanges').className = 'stat-value ' + (data.git && data.git.changes > 0 ? 'yellow' : '');

            if (data.system && data.system.cpu) {{
                document.getElementById('statCpu').textContent = data.system.cpu;
            }}

            if (data.system && data.system.memory) {{
                document.getElementById('statMemory').textContent = data.system.memory;
            }}

            if (data.system && data.system.disk) {{
                document.getElementById('statDisk').textContent = data.system.disk;
            }}

            // Status dot
            const statusDot = document.getElementById('statusDot');
            statusDot.className = 'status-dot' + (claudeCount > 0 ? '' : sessionCount > 0 ? ' warning' : ' error');

            // Store sessions data for later use
            sessionsData = data.tmux ? data.tmux.sessions : [];

            if (!activeSession && sessionsData.length > 0) {{
                activeSession = sessionsData[0].name;
            }}

            const activeStillExists = activeSession && sessionsData.some(s => s.name === activeSession);
            if (!activeStillExists) {{
                activeSession = sessionsData.length > 0 ? sessionsData[0].name : null;
                activeWindow = null;
                activePane = null;
            }}

            updateTerminalBadge();

            // Sessions list
            document.getElementById('sessionList').innerHTML = renderSessions(sessionsData);

            // Git
            if (data.git) {{
                document.getElementById('gitBranch').textContent = data.git.branch || '-';
                document.getElementById('gitChanges').innerHTML = renderGitChanges(data.git.status);
                document.getElementById('commitList').innerHTML = renderCommits(data.git.log);
            }}

            // Processes
            document.getElementById('processList').innerHTML = renderProcesses(data.claude);

            // Update time
            document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
        }}

        // Select a different session (uses active window/pane of that session)
        async function selectSession(name) {{
            activeSession = name;
            activeWindow = null;
            activePane = null;
            updateTerminalBadge();
            terminalRawContent = '';

            try {{
                const response = await fetch(`api/pane?session=${{encodeURIComponent(name)}}`);
                const data = await response.json();
                renderTerminalContent(data.content);
            }} catch (e) {{
                console.error('Failed to fetch pane:', e);
            }}

            document.getElementById('sessionList').innerHTML = renderSessions(sessionsData);
        }}

        // Select a specific window within a session
        async function selectWindow(sessionName, windowIndex) {{
            activeSession = sessionName;
            activeWindow = windowIndex;
            activePane = null;
            updateTerminalBadge();
            terminalRawContent = '';

            try {{
                const response = await fetch(`api/pane?session=${{encodeURIComponent(sessionName)}}&window=${{encodeURIComponent(windowIndex)}}`);
                const data = await response.json();
                renderTerminalContent(data.content);
            }} catch (e) {{
                console.error('Failed to fetch pane:', e);
            }}

            document.getElementById('sessionList').innerHTML = renderSessions(sessionsData);
        }}

        // Select a specific pane within a window
        async function selectPane(sessionName, windowIndex, paneIndex) {{
            activeSession = sessionName;
            activeWindow = windowIndex;
            activePane = paneIndex;
            updateTerminalBadge();
            terminalRawContent = '';

            try {{
                const response = await fetch(`api/pane?session=${{encodeURIComponent(sessionName)}}&window=${{encodeURIComponent(windowIndex)}}&pane=${{encodeURIComponent(paneIndex)}}`);
                const data = await response.json();
                renderTerminalContent(data.content);
            }} catch (e) {{
                console.error('Failed to fetch pane:', e);
            }}

            document.getElementById('sessionList').innerHTML = renderSessions(sessionsData);
        }}

        function getSessionDisplayName(sessionName) {{
            const session = sessionsData.find(item => item.name === sessionName);
            return (session && session.display_name) || sessionName;
        }}

        function getTargetLabel() {{
            const displayName = getSessionDisplayName(activeSession);
            if (activePane !== null) {{
                return `${{displayName}}:${{activeWindow}}.${{activePane}}`;
            }}
            if (activeWindow !== null) {{
                return `${{displayName}}:${{activeWindow}}`;
            }}
            return displayName || '-';
        }}

        function setSessionDetailsOpen(sessionName, isOpen) {{
            if (isOpen) {{
                expandedSessionDetails.add(sessionName);
            }} else {{
                expandedSessionDetails.delete(sessionName);
            }}
        }}

        function shellQuote(value) {{
            return "'" + String(value).replace(/'/g, "'\\\\''") + "'";
        }}

        function getAttachSocket(s) {{
            const socket = s && s.dispatch && s.dispatch.tmux_socket ? s.dispatch.tmux_socket : '';
            if (socket) return socket;
            const mode = getCurrentMode();
            if (mode === 'claude') return '/root/clawdbot-tmux-sockets/claude-code.sock';
            if (mode === 'codex') return '/root/clawdbot-tmux-sockets/codex-code.sock';
            if (mode === 'default') return '/root/clawdbot-tmux-sockets/default-code.sock';
            return '';
        }}

        function buildAttachCommand(sessionName) {{
            const s = sessionsData.find(item => item.name === sessionName) || {{ name: sessionName, dispatch: {{}} }};
            const socket = getAttachSocket(s);
            const socketArg = socket ? `-S ${{shellQuote(socket)}} ` : '';
            return `tmux ${{socketArg}}attach -t ${{shellQuote(s.name || sessionName)}}`;
        }}

        async function copyAttachCommand(event, sessionName) {{
            event.stopPropagation();
            const text = buildAttachCommand(sessionName);
            try {{
                await navigator.clipboard.writeText(text);
                showToast('Attach command copied');
            }} catch (e) {{
                const textarea = document.createElement('textarea');
                textarea.value = text;
                document.body.appendChild(textarea);
                textarea.select();
                document.execCommand('copy');
                textarea.remove();
                showToast('Attach command copied');
            }}
        }}

        // Update terminal badge to show session:window.pane
        function updateTerminalBadge() {{
            const label = getTargetLabel();
            document.getElementById('terminalSession').textContent = label;
        }}

        function terminalHasSelection(terminal) {{
            const selection = window.getSelection();
            if (!selection || selection.isCollapsed || selection.rangeCount === 0) return false;
            return terminal.contains(selection.anchorNode) || terminal.contains(selection.focusNode);
        }}

        function renderTerminalContent(content, followIfAtBottom = false) {{
            const terminal = document.getElementById('terminalContent');
            if (terminalHasSelection(terminal)) {{
                return;
            }}
            const distanceFromBottom = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight;
            const wasNearBottom = distanceFromBottom < 40;

            if (followIfAtBottom && terminalRawContent && content.startsWith(terminalRawContent)) {{
                const appended = content.slice(terminalRawContent.length);
                if (appended) {{
                    terminal.insertAdjacentHTML('beforeend', formatTerminal(appended));
                }}
            }} else {{
                terminal.innerHTML = formatTerminal(content);
            }}
            terminalRawContent = content;

            if (followIfAtBottom && wasNearBottom) {{
                terminal.scrollTop = terminal.scrollHeight;
            }} else if (!followIfAtBottom) {{
                terminal.scrollTop = terminal.scrollHeight;
            }} else {{
                terminal.scrollTop = Math.max(0, terminal.scrollHeight - terminal.clientHeight - distanceFromBottom);
            }}
        }}

        async function refreshActivePane() {{
            if (!activeSession) {{
                return;
            }}

            const params = new URLSearchParams({{ session: activeSession }});
            if (activeWindow !== null) {{
                params.set('window', activeWindow);
            }}
            if (activePane !== null) {{
                params.set('pane', activePane);
            }}

            const response = await fetch(`api/pane?${{params.toString()}}`);
            const data = await response.json();
            if (data.content !== undefined) {{
                renderTerminalContent(data.content, true);
            }}
        }}

        // Refresh data
        async function refreshData() {{
            const btn = document.getElementById('refreshBtn');
            const text = document.getElementById('refreshText');

            btn.classList.add('loading');
            text.textContent = '...';

            try {{
                const response = await fetch('api/data');
                const data = await response.json();
                updateUI(data);
                await refreshActivePane();
            }} catch (e) {{
                console.error('Failed to refresh:', e);
                document.getElementById('statusDot').className = 'status-dot error';
            }} finally {{
                btn.classList.remove('loading');
                text.textContent = 'Refresh';
            }}
        }}

        // Pull to refresh
        document.addEventListener('touchstart', (e) => {{
            if (window.scrollY === 0) {{
                touchStartY = e.touches[0].clientY;
            }}
        }});

        document.addEventListener('touchmove', (e) => {{
            if (touchStartY > 0 && window.scrollY === 0) {{
                const diff = e.touches[0].clientY - touchStartY;
                if (diff > 50) {{
                    isPulling = true;
                    document.getElementById('pullIndicator').classList.add('visible');
                }}
            }}
        }});

        document.addEventListener('touchend', () => {{
            if (isPulling) {{
                refreshData();
            }}
            touchStartY = 0;
            isPulling = false;
            document.getElementById('pullIndicator').classList.remove('visible');
        }});

        // Initial load and auto-refresh
        refreshData();
        refreshTimer = setInterval(refreshData, refreshInterval);

        // Visibility change handling
        document.addEventListener('visibilitychange', () => {{
            if (document.hidden) {{
                clearInterval(refreshTimer);
            }} else {{
                refreshData();
                refreshTimer = setInterval(refreshData, refreshInterval);
            }}
        }});
    </script>
</body>
</html>'''


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Multi-threaded TCP server for concurrent request handling."""
    allow_reuse_address = True
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description='Claw - Remote control for Claude Code sessions')
    parser.add_argument('-p', '--port', type=int, default=8080, help='Port to run on')
    parser.add_argument('-b', '--bind', '--host', dest='bind', type=str, default='127.0.0.1',
                        help='Address to bind to (default: 127.0.0.1, use 0.0.0.0 for network access)')
    parser.add_argument('--tmux-socket', type=str, default='',
                        help='Absolute path to a tmux socket. Uses default tmux server when omitted.')
    parser.add_argument('-r', '--refresh', type=int, default=5, help='Refresh interval in seconds')
    parser.add_argument('-d', '--dir', type=str, help='Add a work directory to monitor')
    parser.add_argument('-s', '--share', action='store_true',
                        help='Share via public URL (uses Cloudflare Tunnel, auto-downloads if needed)')
    parser.add_argument('--no-auth', action='store_true',
                        help='Disable Basic Auth for --share. Use only for temporary trusted testing.')
    args = parser.parse_args()

    global TMUX_SOCKET
    if args.tmux_socket:
        socket_path = Path(args.tmux_socket).expanduser()
        if not socket_path.is_absolute():
            parser.error('--tmux-socket must be an absolute path')
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        TMUX_SOCKET = str(socket_path)

    config = DashboardConfig()
    config.bind_address = args.bind
    config.port = args.port
    config.refresh_interval = args.refresh

    if args.dir:
        name = Path(args.dir).name
        config.work_dirs[name] = args.dir

    # Set config and generate cached HTML
    DashboardHandler.set_config(config)

    # Determine display URL
    display_addr = 'localhost' if config.bind_address == '127.0.0.1' else config.bind_address

    print(f'''
\033[38;5;141m    ╱╱╱
   ╱╱╱   \033[0m\033[1mClaw\033[0m - CLaude AnyWhere
\033[38;5;141m  ╱╱╱    \033[0m\033[2mRemote control for Claude Code\033[0m
    ''')

    # Start tunnel if --share is used
    tunnel = None
    public_url = None
    auth_password = None
    if args.share:
        if not args.no_auth:
            # Generate random password for authentication
            auth_password = secrets.token_urlsafe(12)
            DashboardHandler.set_auth(auth_password)

        tunnel = TunnelManager()
        public_url = tunnel.start_tunnel(config.port)
        if public_url:
            auth_lines = '' if args.no_auth else f'''

  \033[33m🔐 Authentication Required\033[0m
  \033[38;5;244m→\033[0m  Username: \033[1many\033[0m (or leave blank)
  \033[38;5;244m→\033[0m  Password: \033[1;32m{auth_password}\033[0m'''
            print(f'''
  \033[38;5;244m→\033[0m  Local:  http://{display_addr}:{config.port}
  \033[38;5;244m→\033[0m  \033[32mPublic: {public_url}\033[0m  ← \033[1mUse this on your phone!\033[0m
  \033[38;5;244m→\033[0m  Refresh: {config.refresh_interval}s | Projects: {len(config.work_dirs)} | Tmux: {TMUX_SOCKET or 'default'}{auth_lines}
{tunnel.generate_qr_code(public_url)}''')
        else:
            print(f'''
  \033[38;5;244m→\033[0m  Local:  http://{display_addr}:{config.port}
  \033[31m→\033[0m  Public: Failed to start tunnel
  \033[38;5;244m→\033[0m  Refresh: {config.refresh_interval}s | Projects: {len(config.work_dirs)} | Tmux: {TMUX_SOCKET or 'default'}
''')
    else:
        print(f'''
  \033[38;5;244m→\033[0m  http://{display_addr}:{config.port}
  \033[38;5;244m→\033[0m  Refresh: {config.refresh_interval}s | Projects: {len(config.work_dirs)} | Tmux: {TMUX_SOCKET or 'default'}
''')
        if config.bind_address == '127.0.0.1':
            print("  \033[33mTip:\033[0m Use --share to access from anywhere (phone, etc.)")
            print()

    print("  Projects:")
    for name, path in config.work_dirs.items():
        print(f"    • {name}: {path}")
    print()
    print("  \033[2mPress Ctrl+C to stop\033[0m")
    print()

    try:
        with ThreadedTCPServer((config.bind_address, config.port), DashboardHandler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  \033[2mStopping...\033[0m")
    except OSError as e:
        if 'Address already in use' in str(e):
            print(f"\033[31mError:\033[0m Port {config.port} already in use. Try: claw -p {config.port + 1}")
        else:
            raise
    finally:
        # Clean up tunnel
        if tunnel:
            tunnel.stop_tunnel()
            print("  \033[2mTunnel closed.\033[0m")
        print("  \033[2mClaw stopped.\033[0m")


if __name__ == '__main__':
    main()
