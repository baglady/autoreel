"""
YouTube Data API v3 upload, with a resumable transfer and an OAuth flow that
stores its token next to the script.

Quota reality, because it bites early: an upload costs 1600 units against a
default 10,000/day project quota, so a fresh Google Cloud project gets about
six uploads per day no matter how many channels you point it at. Raising it
requires a compliance audit. The uploader therefore counts what it has spent
and refuses to start a transfer it cannot finish.
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import date
from pathlib import Path

SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
UPLOAD_COST = 1600
DEFAULT_DAILY_QUOTA = 10000

HERE = Path(__file__).resolve().parent
CLIENT_SECRET = HERE / 'client_secret.json'
TOKEN = HERE / 'token.json'
QUOTA_FILE = HERE / '.quota.json'

RETRIABLE_STATUS = {500, 502, 503, 504}


def authenticate(client_secret=None, token=None, headless: bool = False):
    """
    Returns an authorised YouTube service. First run opens a browser for
    consent; after that the refresh token in token.json is enough.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build as build_service

    client_secret = Path(client_secret or CLIENT_SECRET)
    token = Path(token or TOKEN)

    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not client_secret.exists():
            raise RuntimeError(
                'missing %s -- create an OAuth client (type: Desktop app) in a '
                'Google Cloud project with the YouTube Data API v3 enabled, '
                'download the JSON, and save it there' % client_secret)
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
        creds = flow.run_console() if headless else flow.run_local_server(port=0)
        token.write_text(creds.to_json(), encoding='utf8')
        os.chmod(token, 0o600)

    return build_service('youtube', 'v3', credentials=creds, cache_discovery=False)


# --- quota ----------------------------------------------------------------

def _quota_state() -> dict:
    if QUOTA_FILE.exists():
        try:
            state = json.loads(QUOTA_FILE.read_text(encoding='utf8'))
            if state.get('date') == date.today().isoformat():
                return state
        except json.JSONDecodeError:
            pass
    return {'date': date.today().isoformat(), 'spent': 0}


def quota_left(daily=DEFAULT_DAILY_QUOTA) -> int:
    return max(0, int(daily) - _quota_state()['spent'])


def _spend(units: int) -> None:
    state = _quota_state()
    state['spent'] += units
    QUOTA_FILE.write_text(json.dumps(state), encoding='utf8')


# --- upload ---------------------------------------------------------------

def upload(service, path, meta: dict, log=print, chunk_mb: int = 8,
           daily_quota=DEFAULT_DAILY_QUOTA) -> dict:
    """
    Resumable upload with exponential backoff. Returns the created video
    resource; raises on a non-retriable failure.
    """
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    path = Path(path)
    if not path.exists():
        raise RuntimeError('no such file: %s' % path)
    if quota_left(daily_quota) < UPLOAD_COST:
        raise RuntimeError(
            'daily quota exhausted (an upload costs %d units; %d left). '
            'It resets at midnight Pacific.' % (UPLOAD_COST, quota_left(daily_quota)))

    title = meta['title']
    if meta.get('short') and '#shorts' not in title.lower():
        # The hashtag is what actually classifies a Short, not the aspect ratio.
        title = (title[:TITLE_ROOM] if len(title) > TITLE_ROOM else title) + ' #Shorts'

    body = {
        'snippet': {
            'title': title,
            'description': meta.get('description', ''),
            'tags': meta.get('tags', []),
            'categoryId': meta.get('categoryId', '10'),
        },
        'status': {
            'privacyStatus': meta.get('privacyStatus', 'private'),
            'selfDeclaredMadeForKids': meta.get('madeForKids', False),
        },
    }

    media = MediaFileUpload(str(path), chunksize=chunk_mb * 1024 * 1024,
                            resumable=True, mimetype='video/*')
    request = service.videos().insert(part='snippet,status', body=body, media_body=media)

    response, attempt = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                log('  uploading %d%%' % int(status.progress() * 100))
        except HttpError as err:
            if err.resp.status in RETRIABLE_STATUS and attempt < 6:
                attempt += 1
                nap = min(60, 2 ** attempt) + random.random()
                log('  %s -- retrying in %.1fs' % (err.resp.status, nap))
                time.sleep(nap)
                continue
            raise
        except (OSError, IOError):
            if attempt >= 6:
                raise
            attempt += 1
            time.sleep(min(60, 2 ** attempt))

    _spend(UPLOAD_COST)
    vid = response['id']
    log('https://youtu.be/%s  (%s)' % (vid, body['status']['privacyStatus']))
    return response


TITLE_ROOM = 100 - len(' #Shorts')
