"""
Tempo detection with no dependencies.

A port of `detectTempo` from dspm-archive's browser reelmaker, which does the
whole thing in plain JavaScript. Audio is decoded by ffmpeg -- already required
for rendering -- so nothing here needs librosa, numpy, numba or scipy.

That matters for two reasons: the scientific stack is a painful install on a
Raspberry Pi, and both tools then agree on the tempo, because it is literally
the same algorithm.

    from tempo import detect
    detect('track.wav')   # {'bpm':128.0, 'offset':0.0, 'duration':.., ...}

numpy is used if it happens to be installed, purely for speed. The result is
the same either way.

No reliability score is returned, deliberately. The obvious candidate -- how
tightly the onset energy folds onto one phase -- was measured and does not
discriminate: a click track scores 19.7x uniform, but real music and a beatless
sine tone both score ~4x. A number that cannot tell those apart would be worse
than none, so the BPM is simply reported and left editable.
"""
from __future__ import annotations

import array
import math
import shutil
import subprocess
from pathlib import Path

SR = 8000              # plenty: everything that matters here is under 160 Hz
HOP = 64               # 8 ms -- the resolution of the detected beat phase
LOWPASS_HZ = 160.0
MAX_ANALYSIS = 60.0    # seconds of audio to look at
BPM_MIN, BPM_MAX, BPM_STEP = 60.0, 200.0, 0.25
BINS = 32

try:
    import numpy as _np
except ImportError:
    _np = None


def _decode(path) -> array.array:
    """Mono 8 kHz signed 16-bit PCM, straight out of ffmpeg."""
    exe = shutil.which('ffmpeg')
    if not exe:
        raise RuntimeError('ffmpeg not found on PATH')
    proc = subprocess.run(
        [exe, '-hide_banner', '-loglevel', 'error', '-i', str(path),
         '-t', str(MAX_ANALYSIS), '-vn', '-ac', '1', '-ar', str(SR),
         '-f', 's16le', '-'],
        capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        tail = (proc.stderr or b'').decode('utf8', 'replace').strip().splitlines()[-3:]
        raise RuntimeError('could not decode %s\n%s' % (Path(path).name, '\n'.join(tail)))
    pcm = array.array('h')
    pcm.frombytes(proc.stdout[:len(proc.stdout) // 2 * 2])
    return pcm


def _onset_envelope(pcm):
    """
    Low-pass, then the positive frame-to-frame energy difference: the same
    envelope reelmaker builds, and the thing the comb filter searches.
    """
    a = 1.0 - math.exp(-2.0 * math.pi * LOWPASS_HZ / SR)

    if _np is not None:
        x = _np.frombuffer(pcm.tobytes(), dtype=_np.int16).astype(_np.float32) / 32768.0
        # one-pole low pass, done as a cheap cascade of two boxcars: the exact
        # filter shape does not matter, only that the highs are gone
        win = max(1, int(SR / (2 * math.pi * LOWPASS_HZ)))
        k = _np.ones(win, dtype=_np.float32) / win
        y = _np.convolve(_np.convolve(x, k, 'same'), k, 'same')
        n = len(y) // HOP
        frames = y[:n * HOP].reshape(n, HOP)
        energy = (frames * frames).sum(axis=1)
        onset = _np.diff(energy, prepend=energy[:1])
        onset[onset < 0] = 0
        peak = float(onset.max()) or 1.0
        return (onset / peak).tolist()

    y, prev = [], 0.0
    for s in pcm:
        prev += a * (s / 32768.0 - prev)
        y.append(prev)
    n = len(y) // HOP
    energy = [0.0] * n
    for i in range(n):
        base = i * HOP
        energy[i] = sum(v * v for v in y[base:base + HOP])
    onset = [0.0] * n
    for i in range(1, n):
        d = energy[i] - energy[i - 1]
        onset[i] = d if d > 0 else 0.0
    top = max(onset) or 1.0
    return [v / top for v in onset]


def _peaks(onset) -> list:
    """Local maxima that stand out from the neighbourhood."""
    if not onset:
        return []
    mean = sum(onset) / len(onset)
    floor = mean * 1.5
    out = []
    for i in range(1, len(onset) - 1):
        v = onset[i]
        if v > floor and v >= onset[i - 1] and v > onset[i + 1]:
            out.append(i)
    return out


def detect(path) -> dict:
    """
    {'bpm', 'offset', 'confidence', 'duration'} -- offset is the phase of the
    first beat, in seconds.
    """
    pcm = _decode(path)
    duration_analysed = len(pcm) / SR
    onset = _onset_envelope(pcm)
    dt = HOP / SR
    peaks = _peaks(onset)
    if len(peaks) < 4:
        return {'bpm': 120.0, 'offset': 0.0, 'beats_matched': 0,
                'duration': _duration(path, duration_analysed)}

    # --- comb search: how tightly does the onset energy fold onto one phase?
    best_score = 0.0
    candidates = []
    nz = [(i, v) for i, v in enumerate(onset) if v]
    bpm = BPM_MIN
    while bpm <= BPM_MAX + 1e-9:
        pf = (60.0 / bpm) / dt
        acc = [0.0] * BINS
        total = 0.0
        for i, v in nz:
            acc[int((i % pf) / pf * BINS) % BINS] += v
            total += v
        peak = max(acc)
        score = (peak / total) if total else 0.0
        candidates.append((bpm, score, (acc.index(peak) + 0.5) / BINS * (60.0 / bpm)))
        if score > best_score:
            best_score = score
        bpm += BPM_STEP

    # Among near-ties -- a pulse and its own half or double fold alike --
    # take the one closest to 120 BPM in log space. A convention, not a fact
    # about the music, which is why the BPM field stays editable.
    shortlist = [c for c in candidates if c[1] > best_score * 0.92]
    shortlist.sort(key=lambda c: abs(math.log(c[0] / 120.0)))
    bpm, score, phase = shortlist[0]

    # --- refine: least-squares fit of t = phase + k * period over the peaks
    # that already sit near a beat. This is what gets the answer off the
    # 0.25 BPM search grid and onto the real tempo.
    period = 60.0 / bpm
    times = [p * dt for p in peaks]
    matched = 0
    for _ in range(2):
        sk = st = skk = skt = 0.0
        count = 0
        for t in times:
            k = round((t - phase) / period)
            if abs(t - (phase + k * period)) > period * 0.25:
                continue
            sk += k; st += t; skk += k * k; skt += k * t; count += 1
        if count < 4:
            break
        denom = count * skk - sk * sk
        if not denom:
            break
        slope = (count * skt - sk * st) / denom
        intercept = (st - slope * sk) / count
        if not (0.2 < slope < 1.2):            # 50-300 BPM sanity
            break
        period, phase, matched = slope, intercept, count

    bpm = 60.0 / period
    while phase < 0:
        phase += period
    phase %= period

    return {'bpm': round(bpm, 2), 'offset': round(phase, 4),
            'beats_matched': matched,
            'duration': _duration(path, duration_analysed)}


def _duration(path, analysed: float) -> float:
    """Full track length -- the analysis window may have been shorter."""
    try:
        import reel
        return reel.audio_duration(path)
    except Exception:
        return analysed


if __name__ == '__main__':
    import json
    import sys
    if len(sys.argv) < 2:
        raise SystemExit('usage: python tempo.py <audio>')
    print(json.dumps(detect(sys.argv[1]), indent=2))
