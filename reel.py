"""
Beat-cut 9:16 reel builder.

A Python port of the cutting logic in dspm-archive's browser `reelmaker`:
photos and clips are cut to a beat grid derived from a music file, and the
result is a vertical video of SEGMENT_LENGTH seconds.

The browser version records in real time via MediaRecorder, which means a tab
has to stay in the foreground for the whole take. This one renders each cut
with ffmpeg and concatenates, so it runs headless on a server or a Pi -- which
is what makes unattended uploading possible. Speed is roughly real time, since
the Ken Burns zoompan filter dominates; turn it off with `--no-zoom` and it is
several times faster.
"""
from __future__ import annotations

import math
import random
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# --- kept identical to reelmaker.js so both tools cut the same way ---------

SEGMENT_LENGTH = 30           # seconds; one reel
CUT_DIVISIONS = [0.25, 0.5, 1, 2, 4, 8, 16]   # in beats

WIDTH, HEIGHT = 1080, 1920
FPS = 30

IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}
VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.webm', '.mkv', '.avi'}


def ffmpeg_bin() -> str:
    exe = shutil.which('ffmpeg')
    if not exe:
        raise RuntimeError('ffmpeg not found on PATH')
    return exe


def _run(args: list) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or '').strip().splitlines()[-12:]
        raise RuntimeError('ffmpeg failed:\n' + '\n'.join(tail))


# --- tempo ----------------------------------------------------------------

def detect_tempo(audio_path) -> dict:
    """
    BPM and beat phase, following reelmaker's conventions: search 60-200 BPM,
    and among near-ties (a tempo and its own half or double score alike)
    prefer the candidate closest to 120 BPM in log space.

    Returns {'bpm', 'offset', 'confidence', 'duration'}.
    """
    import librosa
    import numpy as np

    y, sr = librosa.load(str(audio_path), sr=None, mono=True)
    duration = float(librosa.get_duration(y=y, sr=sr))

    onset = librosa.onset.onset_strength(y=y, sr=sr)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr, onset_envelope=onset)
    bpm = float(np.atleast_1d(tempo)[0]) or 120.0

    # fold into 60-200, then apply the near-120 convention
    while bpm and bpm < 60:
        bpm *= 2
    while bpm > 200:
        bpm /= 2
    for alt in (bpm / 2, bpm * 2):
        if 60 <= alt <= 200 and abs(math.log(alt / 120)) < abs(math.log(bpm / 120)):
            bpm = alt

    # phase: first detected onset peak, wrapped into one beat period
    period = 60.0 / bpm
    peaks = librosa.onset.onset_detect(onset_envelope=onset, sr=sr, units='time')
    offset = float(peaks[0]) % period if len(peaks) else 0.0

    return {'bpm': round(bpm, 2), 'offset': round(offset, 4),
            'confidence': 0.0, 'duration': duration}


def audio_duration(path) -> float:
    """
    Duration in seconds, asked of ffmpeg directly.

    ffprobe would be the obvious tool, but some ffmpeg builds ship without it
    (Faircamp's bundled one, for instance), and requiring it would be a silly
    reason for the whole pipeline to fail.
    """
    proc = subprocess.run([ffmpeg_bin(), '-hide_banner', '-i', str(path),
                           '-f', 'null', '-'], capture_output=True, text=True)
    for line in (proc.stderr or '').splitlines():
        line = line.strip()
        if line.startswith('Duration:'):
            hh, mm, ss = line.split('Duration:')[1].split(',')[0].strip().split(':')
            return int(hh) * 3600 + int(mm) * 60 + float(ss)
    raise RuntimeError('could not read the duration of %s' % path)


def cut_times(bpm: float, offset: float, cut: int = 2, rate: float = 1.0,
              length: float = SEGMENT_LENGTH) -> list:
    """Cut boundaries in seconds, relative to the start of the segment."""
    beats = CUT_DIVISIONS[max(0, min(cut, len(CUT_DIVISIONS) - 1))]
    step = (60.0 / (bpm * rate)) * beats
    if step <= 0.02:
        step = 0.02
    times, t = [], offset % step
    if t > 0.001:
        times.append(0.0)
    while t < length - 0.02:
        times.append(round(t, 4))
        t += step
    times.append(round(length, 4))
    return times


# --- media ----------------------------------------------------------------

def gather_media(folder) -> list:
    root = Path(folder)
    files = [p for p in sorted(root.iterdir())
             if p.is_file() and p.suffix.lower() in IMAGE_EXT | VIDEO_EXT]
    if not files:
        raise RuntimeError('no photos or videos in %s' % root)
    return files


def _order(files: list, order: str, needed: int, seed) -> list:
    n = len(files)
    if order == 'shuffle':
        rnd = random.Random(seed)
        pool = []
        while len(pool) < needed:
            block = files[:]
            rnd.shuffle(block)
            pool += block
        return pool[:needed]
    if order == 'pingpong' and n > 2:
        cycle = files + files[-2:0:-1]
    else:
        cycle = files
    return [cycle[i % len(cycle)] for i in range(needed)]


@dataclass
class ReelSettings:
    cut: int = 2                 # index into CUT_DIVISIONS
    rate: float = 1.0
    segment: int = 0             # which SEGMENT_LENGTH block of the track
    order: str = 'sequential'    # sequential | shuffle | pingpong
    fit: str = 'cover'           # cover | contain
    zoom: bool = True            # Ken Burns on stills
    bpm: float = None            # override detection
    seed: int = None
    length: float = SEGMENT_LENGTH
    fx: list = field(default_factory=list)


def _clip_filter(is_image: bool, dur: float, settings: ReelSettings) -> str:
    """Scale/crop one source to 1080x1920, with optional Ken Burns."""
    if settings.fit == 'contain':
        base = ('scale=%d:%d:force_original_aspect_ratio=decrease,'
                'pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black'
                % (WIDTH, HEIGHT, WIDTH, HEIGHT))
    else:
        base = ('scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d'
                % (WIDTH, HEIGHT, WIDTH, HEIGHT))

    chain = ['fps=%d' % FPS, base]
    if is_image and settings.zoom:
        frames = max(2, int(dur * FPS))
        chain = ['scale=%d:%d:force_original_aspect_ratio=increase'
                 % (WIDTH * 2, HEIGHT * 2),
                 'crop=%d:%d' % (WIDTH * 2, HEIGHT * 2),
                 "zoompan=z='min(zoom+0.0015,1.20)':d=%d"
                 ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=%dx%d:fps=%d"
                 % (frames, WIDTH, HEIGHT, FPS)]
    if 'bw' in settings.fx:
        chain.append('hue=s=0')
    chain.append('setsar=1')
    return ','.join(chain)


def build(media_dir, audio, out, settings: ReelSettings = None, log=print) -> dict:
    """
    Render one beat-cut reel. Returns the facts about it that the metadata
    step wants: bpm, cuts, sources used, duration.
    """
    settings = settings or ReelSettings()
    ff = ffmpeg_bin()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # An explicit BPM means the tempo detector -- and therefore librosa and
    # numpy -- is not needed at all.
    if settings.bpm:
        tempo = {'bpm': settings.bpm, 'offset': 0.0,
                 'duration': audio_duration(audio)}
    else:
        tempo = detect_tempo(audio)
    bpm = settings.bpm or tempo['bpm']
    seg_start = settings.segment * SEGMENT_LENGTH
    length = min(settings.length, max(1.0, tempo['duration'] - seg_start))

    times = cut_times(bpm, tempo['offset'], settings.cut, settings.rate, length)
    spans = [(times[i], times[i + 1] - times[i]) for i in range(len(times) - 1)]
    spans = [(s, d) for s, d in spans if d > 0.03]

    files = gather_media(media_dir)
    picks = _order(files, settings.order, len(spans), settings.seed)
    log('%.1f BPM | %d cuts | %d sources | %.1fs' % (bpm, len(spans), len(files), length))

    work = Path(tempfile.mkdtemp(prefix='reel_'))
    try:
        parts = []
        for i, ((_, dur), src) in enumerate(zip(spans, picks)):
            is_image = src.suffix.lower() in IMAGE_EXT
            part = work / ('%04d.mp4' % i)
            args = [ff, '-y', '-hide_banner', '-loglevel', 'error']
            if is_image:
                args += ['-loop', '1', '-t', '%.4f' % dur, '-i', str(src)]
            else:
                # step into longer videos so repeats aren't all the same frame
                seek = (i * 1.7) % 5.0
                args += ['-ss', '%.3f' % seek, '-t', '%.4f' % dur,
                         '-i', str(src), '-an']
            args += ['-vf', _clip_filter(is_image, dur, settings),
                     '-t', '%.4f' % dur,
                     '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                     '-pix_fmt', 'yuv420p', '-r', str(FPS), str(part)]
            _run(args)
            parts.append(part)

        listing = work / 'parts.txt'
        listing.write_text(''.join("file '%s'\n" % p.as_posix() for p in parts),
                           encoding='utf8')
        silent = work / 'silent.mp4'
        _run([ff, '-y', '-hide_banner', '-loglevel', 'error',
              '-f', 'concat', '-safe', '0', '-i', str(listing),
              '-c', 'copy', str(silent)])

        _run([ff, '-y', '-hide_banner', '-loglevel', 'error',
              '-i', str(silent),
              '-ss', '%.3f' % seg_start, '-t', '%.3f' % length, '-i', str(audio),
              '-map', '0:v:0', '-map', '1:a:0',
              '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
              '-movflags', '+faststart', '-shortest', str(out)])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    log('wrote %s' % out)
    return {'path': str(out), 'bpm': bpm, 'cuts': len(spans),
            'duration': length, 'sources': [p.name for p in files],
            'audio': Path(audio).name}
