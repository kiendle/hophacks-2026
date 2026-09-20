"""Explicit, deduplicated delivery of an existing brief to the paired Telegram chat."""
import asyncio
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import aiohttp
from aiohttp import web

import telegram_bot as telegram

RECEIPTS = Path(__file__).resolve().parent / 'state/telegram/brief-deliveries.sqlite3'
MAX_AUDIO = 20 * 1024 * 1024


def destination():
    token, allowed = telegram.settings()
    if not token or not allowed:
        raise ValueError('Connect Telegram first: configure TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS, then send /start to your bot.')
    selected = os.environ.get('TELEGRAM_DEFAULT_CHAT_ID', '').strip()
    if selected:
        if not re.fullmatch(r'-?\d+', selected) or int(selected) not in allowed:
            raise ValueError('The default Telegram chat must be one of the allowed chat IDs.')
        return token, int(selected)
    if len(allowed) != 1:
        raise ValueError('Multiple Telegram chats are connected. Set TELEGRAM_DEFAULT_CHAT_ID to choose where web briefs go.')
    return token, next(iter(allowed))


def receipt(brief_id, chat_id, action, message_id=None):
    RECEIPTS.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(RECEIPTS, timeout=5)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS deliveries (brief TEXT, chat INTEGER, state TEXT, message INTEGER, PRIMARY KEY (brief, chat))')
        if action == 'claim':
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state, message FROM deliveries WHERE brief=? AND chat=?', (brief_id, chat_id)).fetchone()
            if row:
                return row
            db.execute("INSERT INTO deliveries VALUES (?, ?, 'pending', NULL)", (brief_id, chat_id))
        elif action == 'sent':
            db.execute("UPDATE deliveries SET state='sent', message=? WHERE brief=? AND chat=?", (message_id, brief_id, chat_id))
        elif action == 'release':
            db.execute('DELETE FROM deliveries WHERE brief=? AND chat=?', (brief_id, chat_id))
    return None


async def deliver(brief_id, brief_base=None):
    if not isinstance(brief_id, str) or not re.fullmatch(r'\d{8}-\d{6}', brief_id):
        return {'error': 'Choose a valid brief to send.'}
    try:
        token, chat_id = destination()
    except ValueError as error:
        return {'error': str(error)}
    base = (brief_base or os.environ.get('SIGNAL_BASE_URL') or 'http://127.0.0.1:5194').rstrip('/')
    claimed = False
    uploading = False
    try:
        previous = await asyncio.to_thread(receipt, brief_id, chat_id, 'claim')
        if previous:
            if previous[0] == 'sent':
                return {'sent': True, 'already_sent': True, 'message_id': previous[1]}
            return {'error': 'Delivery is in progress or could not be confirmed. Check your Telegram chat before sending again.'}
        claimed = True
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as http:
            async with http.get(f'{base}/api/briefs/{brief_id}') as response:
                if response.status != 200:
                    raise ValueError('The brief could not be loaded. Make sure Morning Brief is running.')
                brief = await response.json()
            filename = (brief.get('audio') or {}).get('full')
            if brief.get('status') != 'ready' or not isinstance(filename, str) or not re.fullmatch(r'[a-z0-9-]+\.mp3', filename):
                raise ValueError('This brief does not have a ready audio recording yet.')
            async with http.get(f'{base}/api/briefs/{brief_id}/audio/{filename}') as response:
                if response.status != 200:
                    raise ValueError('The brief recording could not be downloaded.')
                audio = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    audio.extend(chunk)
                    if len(audio) > MAX_AUDIO:
                        raise ValueError('This recording is too large to send.')
                if not audio:
                    raise ValueError('The brief recording is empty.')
            uploading = True
            result = await telegram.Api(token, telegram.API_BASE, http).call(
                'sendAudio', attempts=1, chat_id=chat_id,
                title=str(brief.get('title') or 'Morning Brief')[:120], performer='Morning Brief',
                caption='Your brief, saved here for listening whenever you like.',
                upload=('audio', ('morning-brief.mp3', bytes(audio), 'audio/mpeg')))
            if not isinstance(result, dict) or not result.get('message_id'):
                raise ValueError('Telegram did not confirm delivery. Check your chat before trying again.')
            await asyncio.to_thread(receipt, brief_id, chat_id, 'sent', result['message_id'])
            return {'sent': True, 'message_id': result['message_id']}
    except telegram.ApiError as error:
        if error.status >= 500:
            return {'error': 'Delivery could not be confirmed. Check Telegram before retrying.'}
        uploading = False  # Telegram explicitly refused the upload, so retrying is safe.
        return {'error': 'Telegram could not accept the recording. Open your bot, send /start, and check the configured chat.'}
    except ValueError as error:
        return {'error': str(error)}
    except aiohttp.ClientConnectorError:
        uploading = False  # No connection was established, so no upload reached Telegram.
        return {'error': 'Could not connect to the brief service or Telegram. Nothing was sent; please try again when the connection is available.'}
    except (aiohttp.ClientError, TimeoutError, OSError, sqlite3.Error):
        return {'error': 'Delivery could not be confirmed. Check Telegram before retrying.' if uploading
                else 'The brief or Telegram service is unavailable. Please try again.'}
    finally:
        if claimed and not uploading:
            await asyncio.to_thread(receipt, brief_id, chat_id, 'release')


async def send_route(request):
    result = await deliver(request.match_info['id'])
    return web.json_response(result, status=200 if result.get('sent') else 409, headers={'Cache-Control': 'no-store'})


def setup(app):
    app.add_routes([web.post('/api/briefs/{id}/telegram', send_route)])
