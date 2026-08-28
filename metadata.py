"""
Turn what the analyzer saw into a title, description and tags.

Deliberately rule-based, not generative: the same video always produces the
same metadata, and nothing is claimed about the footage that the analyzer did
not actually detect.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

# YouTube's own limits.
TITLE_MAX = 100
DESC_MAX = 5000
TAGS_MAX_CHARS = 500

# A detected object is only interesting if it is worth saying out loud.
BORING = {'person'}   # almost every clip has one; it makes for dull titles


def _slug_words(name: str) -> list:
    """Human words out of a filename: 'reel_2026-08-27_night' -> [night]."""
    stem = Path(name).stem
    parts = re.split(r'[\s_\-.]+', stem)
    return [p for p in parts if p and not p.isdigit() and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', p)]


def build(facts: dict, profile: dict = None) -> dict:
    """
    `facts` merges the reel build result and the analyzer output. `profile`
    is the user's standing preferences (channel voice, fixed tags, privacy).

    Returns a dict ready to hand to youtube.upload().
    """
    profile = profile or {}
    objects = [o for o in facts.get('objects', []) if o not in BORING][:3]
    bpm = facts.get('bpm') or facts.get('audio', {}).get('bpm')
    tod = facts.get('time_of_day')
    dur = float(facts.get('duration') or facts.get('duration_sec') or 0)

    # --- title
    prefix = profile.get('title_prefix', '').strip()
    subject = ', '.join(objects) if objects else (profile.get('subject') or 'visuals')
    bits = []
    if prefix:
        bits.append(prefix)
    if tod:
        bits.append(tod.lower())
    bits.append(subject)
    if bpm:
        bits.append('%d BPM' % round(float(bpm)))
    title = ' - '.join(b for b in bits if b)
    title = title[0].upper() + title[1:] if title else 'Untitled reel'
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX - 1].rstrip() + '…'

    # --- description
    lines = []
    if profile.get('description'):
        lines += [profile['description'].strip(), '']
    detail = []
    if bpm:
        detail.append('Cut to the beat at %.1f BPM.' % float(bpm))
    if facts.get('cuts'):
        detail.append('%d cuts across %.0f seconds.' % (facts['cuts'], dur))
    if facts.get('audio_track'):
        detail.append('Audio: %s' % facts['audio_track'])
    if objects:
        detail.append('Seen in frame: %s.' % ', '.join(objects))
    if detail:
        lines += [' '.join(detail), '']
    if profile.get('links'):
        lines += [profile['links'].strip(), '']
    lines.append('Made with reelmaker + auto-uploader.')
    description = '\n'.join(lines).strip()[:DESC_MAX]

    # --- tags
    tags = list(profile.get('tags', []))
    tags += objects
    if tod:
        tags.append(tod.lower())
    tags += _slug_words(facts.get('path', ''))[:2]
    if _is_short(facts):
        tags.append('shorts')
    seen, clean = set(), []
    for t in tags:
        t = str(t).strip().lower()
        if t and t not in seen and len(t) <= 30:
            seen.add(t)
            clean.append(t)
    while sum(len(t) + 1 for t in clean) > TAGS_MAX_CHARS:
        clean.pop()

    return {
        'title': title,
        'description': description,
        'tags': clean,
        'categoryId': str(profile.get('category_id', 10)),   # 10 = Music
        'privacyStatus': profile.get('privacy', 'private'),
        'madeForKids': bool(profile.get('made_for_kids', False)),
        'short': _is_short(facts),
    }


def _is_short(facts: dict) -> bool:
    """YouTube treats vertical video of 3 minutes or less as a Short."""
    dur = float(facts.get('duration') or facts.get('duration_sec') or 0)
    return 0 < dur <= 180


def load_profile(path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding='utf8'))


def stamp() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M')
