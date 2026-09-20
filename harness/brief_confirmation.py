"""Human confirmation for delivering one existing recording to one Telegram chat.

Tools can create pending requests. Only the web and Telegram button handlers call
decide_confirmation; they send directly, without asking the model to act on approval.
"""
import json
import os
import re
import secrets
import time
from pathlib import Path

import aiohttp

import brief_delivery

TTL_MS = 10 * 60 * 1000
CONFIRMATION = re.compile(r'[A-Za-z0-9_-]{16}')
KIND = 'brief_telegram'


def _read(path):
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        return record if isinstance(record, dict) else None
    except (OSError, ValueError):
        return None


def _write(path, record):
    temporary = path.with_name(f'{path.name}.{secrets.token_hex(8)}.tmp')
    try:
        temporary.write_text(json.dumps(record, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _result(record):
    return {key: record[key] for key in ('kind', 'confirmation_id', 'brief_id', 'summary', 'expires_ms')} | {
        'next': 'Wait for the user to press Confirm. The button sends this recording directly; do not call another send tool.'}


async def request_confirmation(brief_id, *, directory=None, brief_base=None):
    """Snapshot ready audio and its destination without sending any Telegram message."""
    if not isinstance(brief_id, str) or not brief_delivery.BRIEF_ID.fullmatch(brief_id):
        return {'error': 'Choose a valid brief to send.'}
    directory = directory or os.environ.get('HARNESS_SESSION_DIR')
    if not directory:
        return {'error': 'Open a chat session before requesting delivery confirmation.'}
    base = (brief_base or os.environ.get('SIGNAL_BASE_URL') or 'http://127.0.0.1:5194').rstrip('/')
    try:
        _, chat_id = brief_delivery.destination()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
            brief, _, identity = await brief_delivery.ready_audio(http, base, brief_id)
        identity['chat_id'] = chat_id
        now = int(time.time() * 1000)
        folder = Path(directory) / 'confirmations'
        folder.mkdir(parents=True, exist_ok=True)
        for path in folder.glob('*.json'):
            previous = _read(path)
            if (previous and previous.get('kind') == KIND and previous.get('delivery') == identity
                    and previous.get('decision') is None and not previous.get('consumed')
                    and previous.get('expires_ms', 0) > now and previous.get('brief_base') == base):
                return _result(previous)
        title = str(brief.get('title') or 'Your audio brief')[:120]
        record = {'kind': KIND, 'confirmation_id': secrets.token_urlsafe(12), 'brief_id': brief_id,
                  'brief_base': base, 'delivery': identity, 'created_ms': now, 'expires_ms': now + TTL_MS,
                  'decision': None, 'decided_ms': None, 'consumed': False,
                  'summary': f'Send "{title}" (brief {brief_id}) to Telegram chat {chat_id}? '
                             'The existing recording is ready. It will be sent only when you confirm.'}
        _write(folder / f"{record['confirmation_id']}.json", record)
        return _result(record)
    except ValueError as error:
        return {'error': str(error)}
    except (aiohttp.ClientError, TimeoutError, OSError):
        return {'error': 'The recording could not be checked. Nothing was sent; try again when Morning Brief is available.'}


def _decide(directory, confirmation_id, approved, expected_chat_id):
    if not isinstance(confirmation_id, str) or not CONFIRMATION.fullmatch(confirmation_id) or not isinstance(approved, bool):
        raise ValueError('A valid confirmation ID and boolean decision are required.')
    path = Path(directory) / 'confirmations' / f'{confirmation_id}.json'
    lock = path.with_suffix('.decision-lock')
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError('That confirmation is already being decided.') from None
    except FileNotFoundError:
        raise ValueError('That confirmation is no longer valid; ask for a new one.') from None
    os.close(descriptor)
    try:
        record, now = _read(path), int(time.time() * 1000)
        if not record or record.get('kind') != KIND or record.get('confirmation_id') != confirmation_id:
            raise ValueError('That confirmation is no longer valid; ask for a new one.')
        if record.get('decision') is not None or record.get('consumed'):
            raise ValueError('That confirmation was already decided.')
        if record.get('expires_ms', 0) <= now:
            raise ValueError('That confirmation expired; ask for a new one.')
        identity = record.get('delivery')
        if not isinstance(identity, dict) or identity.get('brief_id') != record.get('brief_id'):
            raise ValueError('The saved brief does not match this confirmation. Ask for a new one.')
        if expected_chat_id is not None and identity.get('chat_id') != expected_chat_id:
            raise ValueError('This confirmation belongs to a different Telegram chat.')
        record.update(decision='approved' if approved else 'declined', decided_ms=now, consumed=True)
        _write(path, record)
        return path, record
    finally:
        lock.unlink(missing_ok=True)


async def decide_confirmation(directory, confirmation_id, approved, *, brief_base=None, expected_chat_id=None):
    """Record one immutable human decision, then send only the approved recording."""
    try:
        path, record = _decide(directory, confirmation_id, approved, expected_chat_id)
    except (ValueError, OSError) as error:
        return {'error': str(error)}
    if not approved:
        return {'declined': True, 'brief_id': record['brief_id'], 'message': 'Cancelled. Your recording was not sent to Telegram.'}
    result = await brief_delivery.deliver(record['brief_id'], brief_base=brief_base or record['brief_base'], expected=record['delivery'])
    record['delivery_result'] = result
    _write(path, record)
    return result


def delivery_message(result):
    if result.get('sent'):
        return 'This brief was already sent to Telegram.' if result.get('already_sent') else 'Your brief was sent to Telegram.'
    return result.get('message') or result.get('error') or 'The recording could not be sent to Telegram.'
