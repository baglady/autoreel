"""
autoreel -- make beat-cut vertical videos and put them on YouTube by itself.

    python autoreel.py make   media/ track.wav -o out/reel.mp4
    python autoreel.py upload out/reel.mp4
    python autoreel.py auto   media/ track.wav
    python autoreel.py watch  out/

`watch` is the unattended mode: anything that lands in the folder -- including
exports from the browser reelmaker -- gets analyzed, described and uploaded
once, and never twice.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import metadata
import reel

HERE = Path(__file__).resolve().parent
STATE = HERE / '.uploaded.json'
PROFILE = HERE / 'profile.json'

VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.webm', '.mkv'}


# --- the record of what has already gone up -------------------------------

def _state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding='utf8'))
        except json.JSONDecodeError:
            pass
    return {}


def _remember(path: Path, video_id: str, title: str) -> None:
    state = _state()
    state[_key(path)] = {'id': video_id, 'title': title,
                         'at': metadata.stamp(), 'file': path.name}
    STATE.write_text(json.dumps(state, indent=2), encoding='utf8')


def _key(path: Path) -> str:
    """Identity of a file: name plus size, so a re-render counts as new."""
    try:
        return '%s:%d' % (path.name, path.stat().st_size)
    except OSError:
        return path.name


def _settled(path: Path, wait: float = 2.0) -> bool:
    """True once the file has stopped growing -- it may still be rendering."""
    try:
        first = path.stat().st_size
        time.sleep(wait)
        return first > 0 and path.stat().st_size == first
    except OSError:
        return False


# --- steps ----------------------------------------------------------------

def describe(path: Path, args, extra: dict = None) -> dict:
    facts = dict(extra or {})
    if args.analyze:
        import analyzer
        print('analyzing...')
        facts.update({k: v for k, v in analyzer.probe(path).items() if v is not None})
    facts.setdefault('path', str(path))
    if not facts.get('duration') and not facts.get('duration_sec'):
        # Needed even without the analyzer: it decides Short vs regular video.
        try:
            facts['duration'] = reel.audio_duration(path)
        except RuntimeError:
            facts['duration'] = 0
    facts.setdefault('duration', facts.get('duration_sec', 0))

    meta = metadata.build(facts, metadata.load_profile(args.profile))
    if args.title:
        meta['title'] = args.title[:metadata.TITLE_MAX]
    if args.privacy:
        meta['privacyStatus'] = args.privacy
    return meta


def send(path: Path, meta: dict, args) -> str:
    import youtube

    print('\n%s\n  title:   %s\n  privacy: %s\n  tags:    %s'
          % (path.name, meta['title'], meta['privacyStatus'], ', '.join(meta['tags'])))
    if args.dry_run:
        print('  (dry run -- nothing uploaded)')
        return ''

    service = youtube.authenticate(headless=args.headless)
    response = youtube.upload(service, path, meta)
    vid = response['id']
    _remember(path, vid, meta['title'])
    return vid


# --- commands -------------------------------------------------------------

def cmd_make(args) -> int:
    settings = reel.ReelSettings(
        cut=args.cut, rate=args.rate, segment=args.segment, order=args.order,
        fit=args.fit, zoom=not args.no_zoom, bpm=args.bpm, seed=args.seed,
        length=args.length, fx=args.fx or [])
    facts = reel.build(args.media, args.audio, args.out, settings)
    print(json.dumps(facts, indent=2))
    return 0


def cmd_upload(args) -> int:
    path = Path(args.video)
    if not args.force and _key(path) in _state():
        print('%s already uploaded -- use --force to send it again' % path.name)
        return 0
    send(path, describe(path, args), args)
    return 0


def cmd_auto(args) -> int:
    out = Path(args.out or (HERE / 'out' / ('reel_%s.mp4' % time.strftime('%Y%m%d_%H%M%S'))))
    settings = reel.ReelSettings(
        cut=args.cut, rate=args.rate, segment=args.segment, order=args.order,
        fit=args.fit, zoom=not args.no_zoom, bpm=args.bpm, seed=args.seed,
        length=args.length, fx=args.fx or [])
    facts = reel.build(args.media, args.audio, out, settings)
    facts['audio_track'] = facts.pop('audio', None)
    send(out, describe(out, args, facts), args)
    return 0


def cmd_watch(args) -> int:
    folder = Path(args.folder)
    folder.mkdir(parents=True, exist_ok=True)
    print('watching %s -- ctrl-c to stop' % folder)

    # Everything already present at startup is treated as history, not backlog,
    # unless --catch-up says otherwise.
    if not args.catch_up:
        for p in folder.iterdir():
            if p.suffix.lower() in VIDEO_EXT and _key(p) not in _state():
                _remember(p, '', '(present at startup)')

    # A dry run must not write to the state file -- that would make the file
    # look uploaded and block the real run later. Track it for this process
    # only, so the loop still stops repeating itself.
    handled = set()

    while True:
        try:
            for path in sorted(folder.iterdir()):
                if path.suffix.lower() not in VIDEO_EXT:
                    continue
                if _key(path) in _state() or _key(path) in handled:
                    continue
                if not _settled(path):
                    continue
                try:
                    send(path, describe(path, args), args)
                    handled.add(_key(path))
                except Exception as e:
                    print('  failed: %s' % e, file=sys.stderr)
                    if 'quota' in str(e).lower():
                        print('  sleeping an hour for quota')
                        time.sleep(3600)
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print('\nstopped')
            return 0


def cmd_status(args) -> int:
    import youtube
    state = _state()
    print('uploaded: %d' % sum(1 for v in state.values() if v.get('id')))
    print('quota left today: %d units (%d uploads)'
          % (youtube.quota_left(), youtube.quota_left() // youtube.UPLOAD_COST))
    for v in list(state.values())[-10:]:
        if v.get('id'):
            print('  %s  %s  https://youtu.be/%s' % (v['at'], v['title'][:40], v['id']))
    return 0


# --- cli ------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='autoreel', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='command', required=True)

    def reel_flags(p):
        p.add_argument('--cut', type=int, default=2,
                       help='0=1/4 beat 1=1/2 2=1 beat 3=2 4=1 bar 5=2 bars 6=4 bars')
        p.add_argument('--rate', type=float, default=1.0)
        p.add_argument('--segment', type=int, default=0, help='which 30s of the track')
        p.add_argument('--order', default='sequential',
                       choices=['sequential', 'shuffle', 'pingpong'])
        p.add_argument('--fit', default='cover', choices=['cover', 'contain'])
        p.add_argument('--no-zoom', action='store_true', help='disable Ken Burns')
        p.add_argument('--bpm', type=float, help='override tempo detection')
        p.add_argument('--seed', type=int)
        p.add_argument('--length', type=float, default=reel.SEGMENT_LENGTH)
        p.add_argument('--fx', nargs='*', default=[], choices=['bw'])

    def upload_flags(p):
        p.add_argument('--title')
        p.add_argument('--privacy', choices=['private', 'unlisted', 'public'])
        p.add_argument('--profile', default=str(PROFILE))
        p.add_argument('--no-analyze', dest='analyze', action='store_false',
                       help='skip YOLO/librosa; metadata comes from the profile alone')
        p.add_argument('--dry-run', action='store_true')
        p.add_argument('--headless', action='store_true',
                       help='console OAuth, for a machine with no browser')

    p = sub.add_parser('make', help='render a reel, do not upload')
    p.add_argument('media'); p.add_argument('audio')
    p.add_argument('-o', '--out', default='out/reel.mp4')
    reel_flags(p); p.set_defaults(func=cmd_make)

    p = sub.add_parser('upload', help='upload an existing video')
    p.add_argument('video'); p.add_argument('--force', action='store_true')
    upload_flags(p); p.set_defaults(func=cmd_upload)

    p = sub.add_parser('auto', help='render a reel and upload it')
    p.add_argument('media'); p.add_argument('audio')
    p.add_argument('-o', '--out')
    reel_flags(p); upload_flags(p); p.set_defaults(func=cmd_auto)

    p = sub.add_parser('watch', help='upload anything that appears in a folder')
    p.add_argument('folder')
    p.add_argument('--interval', type=float, default=10.0)
    p.add_argument('--catch-up', action='store_true',
                   help='also upload what is already in the folder')
    upload_flags(p); p.set_defaults(func=cmd_watch)

    p = sub.add_parser('status', help='what has gone up, and quota left')
    p.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)
    if not hasattr(args, 'analyze'):
        args.analyze = False
    try:
        return args.func(args)
    except RuntimeError as e:
        print('error: %s' % e, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
