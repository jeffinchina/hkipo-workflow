"""Task-local CLI discovery and strict JSONL completion checks.

No authentication/config edits, subprocess launches, transport fallback or retries.
A discovered binary is available, not proven connected.
"""
from pathlib import Path
import json
import shutil

DESKTOP_CLI = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex')
MODEL = 'gpt-6.1-sol'


def cli_candidates(explicit=None):
    """Return deduplicated existing paths; caller must explicitly select/probe one."""
    paths = [explicit] if explicit else [shutil.which('codex'), DESKTOP_CLI]
    result = []
    seen = set()
    for candidate in paths:
        if not candidate:
            continue
        p = Path(candidate).expanduser()
        if p.is_file() and str(p.resolve()) not in seen:
            result.append(str(p.absolute()))
            seen.add(str(p.resolve()))
    return result


def completion_summary(jsonl, returncode=None, timed_out=False):
    """Require successful process, turn.completed, and a nonempty agent message.

    Thread/turn started is not success. Error messages are deliberately not echoed
    here: diagnostic logs may contain caller data and remain local.
    """
    types = []
    texts = []
    malformed = 0
    errors = 0
    for line in jsonl.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        kind = event.get('type', '')
        types.append(kind)
        if kind in ('error', 'turn.failed'):
            errors += 1
        item = event.get('item', {})
        if kind == 'item.completed' and isinstance(item, dict) and item.get('type') == 'agent_message':
            text = item.get('text', '')
            if isinstance(text, str) and text.strip():
                texts.append(text)
    completed = bool(types) and types[-1] == 'turn.completed'
    success = returncode == 0 and not timed_out and completed and bool(texts) and malformed == 0
    return {'success': success, 'turn_completed': completed,
            'agent_message_present': bool(texts), 'error_events': errors,
            'malformed_lines': malformed, 'timed_out': bool(timed_out),
            'returncode': returncode}
