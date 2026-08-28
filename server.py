"""
autoreel as a local web service.

    python server.py                  # http://127.0.0.1:8092
    python server.py --host 0.0.0.0 --token secret

Standard library only -- no Flask, no build step -- so it starts on a machine
where the render dependencies are not installed yet and still does everything
that does not need them.

Mutating routes (upload a file, render, publish) require the token whenever the
server is bound to anything other than loopback. Reads are open. This mirrors
the rule the dspm bridge follows, and for the same reason: this process can
publish to YouTube, so it must not be an open endpoint on a venue LAN.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import secrets
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import autoreel
import metadata
import reel

HERE = Path(__file__).resolve().parent
MEDIA = HERE / 'media'
AUDIO = HERE / 'audio'
OUT = HERE / 'out'
UI = HERE / 'ui.html'

MAX_BODY = 512 * 1024 * 1024          # 512 MB per request
AUDIO_EXT = {'.wav', '.mp3', '.flac', '.m4a', '.aac', '.ogg', '.opus'}

CONFIG = {'token': None, 'loopback_only': True}
JOBS: dict = {}
JOBS_LOCK = threading.Lock()


# --- jobs -----------------------------------------------------------------

class Job:
    """A render or publish running in its own thread, with a readable log."""

    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.lines: list = []
        self.state = 'running'        # running | done | error
        self.result: dict = {}
        self.started = time.time()
        self._lock = threading.Lock()

    def log(self, *parts) -> None:
        line = ' '.join(str(p) for p in parts)
        with self._lock:
            self.lines.append(line)
        print('[%s] %s' % (self.id, line), flush=True)

    def snapshot(self) -> dict:
        with self._lock:
            return {'id': self.id, 'kind': self.kind, 'state': self.state,
                    'lines': list(self.lines), 'result': self.result,
                    'elapsed': round(time.time() - self.started, 1)}


def start_job(kind: str, fn) -> Job:
    job = Job(kind)
    with JOBS_LOCK:
        JOBS[job.id] = job

    def run():
        try:
            job.result = fn(job) or {}
            job.state = 'done'
        except Exception as e:
            job.state = 'error'
            job.log('error: %s' % e)
            job.log(traceback.format_exc().strip().splitlines()[-1])
        finally:
            job.log('--- %s ---' % job.state)

    threading.Thread(target=run, daemon=True).start()
    return job


# --- work -----------------------------------------------------------------

def do_render(job: Job, body: dict) -> dict:
    settings = reel.ReelSettings(
        cut=int(body.get('cut', 2)),
        rate=float(body.get('rate', 1) or 1),
        segment=int(body.get('segment', 0) or 0),
        order=body.get('order', 'sequential'),
        fit=body.get('fit', 'cover'),
        zoom=bool(body.get('zoom', True)),
        bpm=float(body['bpm']) if body.get('bpm') else None,
        seed=int(body['seed']) if body.get('seed') else None,
        length=float(body.get('length', reel.SEGMENT_LENGTH) or reel.SEGMENT_LENGTH),
        fx=body.get('fx') or [])

    track = AUDIO / Path(body['audio']).name
    if not track.exists():
        raise RuntimeError('no such track: %s' % track.name)

    out = OUT / ('reel_%s.mp4' % time.strftime('%Y%m%d_%H%M%S'))
    facts = reel.build(MEDIA, track, out, settings, log=job.log)
    facts['audio_track'] = facts.pop('audio', None)
    facts['file'] = out.name
    return facts


def do_publish(job: Job, body: dict) -> dict:
    path = OUT / Path(body['file']).name
    if not path.exists():
        raise RuntimeError('no such file: %s' % path.name)

    facts = dict(body.get('facts') or {})
    facts['path'] = str(path)
    if body.get('analyze'):
        import analyzer
        job.log('analyzing (this loads YOLO the first time)...')
        facts.update({k: v for k, v in analyzer.probe(path).items() if v is not None})
    if not facts.get('duration'):
        try:
            facts['duration'] = reel.audio_duration(path)
        except RuntimeError:
            facts['duration'] = 0

    meta = metadata.build(facts, metadata.load_profile(HERE / 'profile.json'))
    if body.get('title'):
        meta['title'] = body['title'][:metadata.TITLE_MAX]
    if body.get('privacy'):
        meta['privacyStatus'] = body['privacy']

    job.log('title:   %s' % meta['title'])
    job.log('privacy: %s' % meta['privacyStatus'])
    job.log('tags:    %s' % ', '.join(meta['tags']))

    if body.get('dry_run', True):
        job.log('dry run -- nothing uploaded')
        return {'meta': meta, 'dry_run': True}

    import youtube
    job.log('authenticating...')
    service = youtube.authenticate()
    response = youtube.upload(service, path, meta, log=job.log)
    vid = response['id']
    autoreel._remember(path, vid, meta['title'])
    return {'meta': meta, 'id': vid, 'url': 'https://youtu.be/%s' % vid}


def read_state() -> dict:
    for d in (MEDIA, AUDIO, OUT):
        d.mkdir(parents=True, exist_ok=True)

    media = [p.name for p in sorted(MEDIA.iterdir())
             if p.suffix.lower() in reel.IMAGE_EXT | reel.VIDEO_EXT]
    tracks = [p.name for p in sorted(AUDIO.iterdir()) if p.suffix.lower() in AUDIO_EXT]
    outputs = [{'name': p.name, 'size': p.stat().st_size, 'mtime': p.stat().st_mtime}
               for p in sorted(OUT.glob('*.mp4'), key=lambda p: -p.stat().st_mtime)]

    quota = {'left': None, 'uploads': None}
    try:
        import youtube
        quota = {'left': youtube.quota_left(),
                 'uploads': youtube.quota_left() // youtube.UPLOAD_COST}
    except Exception:
        pass

    uploaded = [v for v in autoreel._state().values() if v.get('id')][-10:]
    return {'media': media, 'tracks': tracks, 'outputs': outputs,
            'quota': quota, 'uploaded': uploaded,
            'connected': (HERE / 'token.json').exists(),
            'has_client_secret': (HERE / 'client_secret.json').exists(),
            'cut_divisions': reel.CUT_DIVISIONS}


# --- multipart ------------------------------------------------------------

def parse_multipart(body: bytes, boundary: str) -> list:
    """Minimal multipart/form-data reader: [(field, filename, bytes)]."""
    sep = b'--' + boundary.encode()
    parts = []
    for chunk in body.split(sep):
        if not chunk or chunk in (b'--\r\n', b'--', b'\r\n'):
            continue
        head, _, data = chunk.partition(b'\r\n\r\n')
        if not _:
            continue
        head_text = head.decode('utf8', 'replace')
        name = re.search(r'name="([^"]*)"', head_text)
        filename = re.search(r'filename="([^"]*)"', head_text)
        parts.append((name.group(1) if name else '',
                      filename.group(1) if filename else None,
                      data[:-2] if data.endswith(b'\r\n') else data))
    return parts


SAFE_NAME = re.compile(r'[^A-Za-z0-9._ -]')


def safe_name(name: str) -> str:
    """A filename that cannot escape its folder."""
    name = SAFE_NAME.sub('_', Path(unquote(name)).name).strip('. ')
    return name or 'file'


# --- http -----------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = 'autoreel'

    def log_message(self, fmt, *args):
        pass                                   # the jobs do the talking

    # -- helpers
    def _send(self, code: int, payload, ctype='application/json'):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        elif isinstance(payload, str):
            payload = payload.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def _authorised(self) -> bool:
        if not CONFIG['token']:
            return True
        given = self.headers.get('X-Token') or parse_qs(
            urlparse(self.path).query).get('token', [''])[0]
        return secrets.compare_digest(given or '', CONFIG['token'])

    def _body(self) -> bytes:
        length = int(self.headers.get('Content-Length') or 0)
        if length > MAX_BODY:
            raise ValueError('body too large')
        return self.rfile.read(length) if length else b''

    # -- routes
    def do_GET(self):
        route = urlparse(self.path).path
        try:
            if route in ('/', '/index.html'):
                return self._send(200, UI.read_text(encoding='utf8'),
                                  'text/html; charset=utf-8')
            if route == '/api/state':
                return self._send(200, read_state())
            if route.startswith('/api/job/'):
                job = JOBS.get(route.rsplit('/', 1)[1])
                return self._send(200, job.snapshot()) if job else \
                    self._send(404, {'error': 'no such job'})
            if route.startswith('/out/'):
                return self._file(OUT / safe_name(route[5:]))
            if route.startswith('/media/'):
                return self._file(MEDIA / safe_name(route[7:]))
            return self._send(404, {'error': 'not found'})
        except Exception as e:
            self._send(500, {'error': str(e)})

    def _file(self, path: Path):
        if not path.exists() or not path.is_file():
            return self._send(404, {'error': 'not found'})
        ctype = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        size = path.stat().st_size
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(size))
        self.send_header('Accept-Ranges', 'none')
        self.end_headers()
        with open(path, 'rb') as fh:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_POST(self):
        route = urlparse(self.path).path
        if not self._authorised():
            return self._send(401, {'error': 'token required'})
        try:
            if route == '/api/files':
                return self._upload_files()

            try:
                body = json.loads(self._body() or b'{}')
            except json.JSONDecodeError as e:
                return self._send(400, {'error': 'bad JSON: %s' % e})
            if route == '/api/render':
                job = start_job('render', lambda j: do_render(j, body))
                return self._send(202, {'job': job.id})
            if route == '/api/publish':
                job = start_job('publish', lambda j: do_publish(j, body))
                return self._send(202, {'job': job.id})
            if route == '/api/delete':
                target = OUT / safe_name(body.get('file', ''))
                if target.exists():
                    target.unlink()
                return self._send(200, {'ok': True})
            return self._send(404, {'error': 'not found'})
        except Exception as e:
            self._send(500, {'error': str(e)})

    def _upload_files(self):
        ctype = self.headers.get('Content-Type', '')
        if 'boundary=' not in ctype:
            return self._send(400, {'error': 'expected multipart/form-data'})
        boundary = ctype.split('boundary=')[1].strip('"')
        saved = []
        for field, filename, data in parse_multipart(self._body(), boundary):
            if not filename or not data:
                continue
            name = safe_name(filename)
            ext = Path(name).suffix.lower()
            if ext in AUDIO_EXT:
                folder = AUDIO
            elif ext in reel.IMAGE_EXT | reel.VIDEO_EXT:
                folder = MEDIA
            else:
                continue                       # ignore anything unrenderable
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(data)
            saved.append({'name': name, 'into': folder.name, 'bytes': len(data)})
        return self._send(200, {'saved': saved})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='autoreel local web service')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8092)
    ap.add_argument('--token', default=os.environ.get('AUTOREEL_TOKEN'),
                    help='required for mutating routes; mandatory off loopback')
    ap.add_argument('--insecure', action='store_true',
                    help='allow a tokenless bind to a routable address')
    args = ap.parse_args(argv)

    loopback = args.host in ('127.0.0.1', 'localhost', '::1')
    if not loopback and not args.token and not args.insecure:
        print('refusing to bind %s without a token: this process can publish to\n'
              'YouTube. Pass --token (or AUTOREEL_TOKEN), or --insecure if the\n'
              'network is genuinely trusted.' % args.host)
        return 2

    CONFIG['token'] = args.token
    CONFIG['loopback_only'] = loopback
    for d in (MEDIA, AUDIO, OUT):
        d.mkdir(parents=True, exist_ok=True)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print('autoreel on http://%s:%d' % (args.host, args.port))
    print('media: %s\naudio: %s\nout:   %s' % (MEDIA, AUDIO, OUT))
    if args.token:
        print('token required for render/publish')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
