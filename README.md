# autoreel

Cut photos and clips to the beat of a music file, make a vertical video, and
put it on YouTube — unattended.

It is the headless counterpart to the browser [`reelmaker`][reelmaker]: same
beat grid and the same cut divisions, but rendered with ffmpeg instead of
`MediaRecorder`, so it runs on a server or a Pi with no tab in the foreground.
That is the whole reason it exists — a browser recording needs a human and a
visible window; this does not.

```
autoreel.py    the CLI: make / upload / auto / watch / status
server.py      the same thing as a local web service
ui.html        its browser UI (one file, no build step)
reel.py        the beat-cut renderer (a Python port of reelmaker's cutting)
tempo.py       BPM + beat phase, with no dependencies at all
analyzer.py    what is in the video: objects (YOLO), day vs night
metadata.py    analyzer facts -> title, description, tags
youtube.py     OAuth + resumable upload + quota accounting
```

## Install

```bash
pip install -r requirements.txt
```

That is three Google client libraries, and nothing else. **ffmpeg must be on
`PATH`** — it does the decoding and the rendering. `ffprobe` is *not* required;
some builds ship without it, so durations are read from ffmpeg itself.

Rendering and tempo detection need **no third-party packages whatsoever**.
`tempo.py` decodes through ffmpeg and runs the beat search in plain Python, so
there is no librosa, numpy, numba or scipy to install — which is what makes
this practical on a Raspberry Pi, where that stack is a slow and fragile build.

The content analyzer (`--analyze`) is the one optional extra, and it is the
heavy one: `pip install librosa opencv-python ultralytics numpy`. Without it
everything still works; titles just come from your profile rather than from
what YOLO saw in the frames.

## Use it

Render a reel and stop:

```bash
python autoreel.py make media/ track.wav -o out/reel.mp4 --cut 3 --order shuffle
```

Render one and upload it:

```bash
python autoreel.py auto media/ track.wav
```

Upload something that already exists:

```bash
python autoreel.py upload out/reel.mp4
```

Watch a folder and upload whatever lands in it — point the browser reelmaker's
downloads here and the loop is closed:

```bash
python autoreel.py watch out/
```

Always try it with `--dry-run` first. It prints the exact title, tags and
privacy it would send, and uploads nothing.

```bash
python autoreel.py auto media/ track.wav --dry-run
```

## Run it locally instead

```bash
python server.py
```

Then open **http://127.0.0.1:8092**. Drop photos, clips and a music file onto
the page, set the cut, render, preview the result, publish. Same code as the
CLI — `server.py` calls straight into `reel.py` and `youtube.py`.

It is **standard library only**: no Flask, no build step, nothing to install
beyond what a render already needs. Files sort themselves into `media/` and
`audio/` by extension, renders land in `out/`, and all three are gitignored.

To reach it from a phone on the same network, bind wider — with a token:

```bash
python server.py --host 0.0.0.0 --token yourtoken
```

Then open `http://<laptop-ip>:8092/?token=yourtoken`.

**Reads are open; render, upload and publish require the token.** Binding to a
routable address without one is refused outright, because this process can
publish to your YouTube channel and a venue LAN is not a trusted network. Pass
`--insecure` to override deliberately. (Loopback needs no token.)

Port 8092 is chosen to sit clear of the dspm services — 8080 PWA, 8081/8082
bridges, 8088 dropbox, 8090 status board.

### The knobs

| Flag | |
|---|---|
| `--cut 0..6` | `1/4 beat`, `1/2`, `1 beat` (default), `2 beats`, `1 bar`, `2 bars`, `4 bars` |
| `--rate` | stretches the beat grid, 0.25–4 |
| `--segment N` | which 30-second block of the track to use |
| `--order` | `sequential` · `shuffle` · `pingpong` |
| `--fit` | `cover` (default) or `contain` |
| `--no-zoom` | turn off Ken Burns — several times faster |
| `--bpm` | skip tempo detection and use this tempo |
| `--length` | reel length in seconds, default 30 |
| `--privacy` | `private` (default) · `unlisted` · `public` |
| `--no-analyze` | skip the YOLO/librosa analyzer; metadata comes from the profile alone |

It uploads **private by default**. Change that deliberately, in
`profile.json` or with `--privacy`.

## Connecting it to YouTube

1. Make a project in the Google Cloud console and enable **YouTube Data API v3**.
2. Create an OAuth client of type **Desktop app**.
3. Download the JSON and save it beside the scripts as `client_secret.json`.

The first upload opens a browser for consent and writes `token.json`; after
that it runs unattended. On a headless box use `--headless` for console
consent. Both files are gitignored, and neither should ever be committed.

## Quota — read this before planning a posting schedule

An upload costs **1600 units** against a default **10,000 units/day** project
quota. That is **six uploads per day**, per Google Cloud *project* — not per
channel, so extra channels do not help. Raising it requires a compliance audit.

`autoreel` counts what it has spent in `.quota.json` and refuses to begin a
transfer it cannot finish, rather than failing halfway through. `watch` sleeps
an hour when it hits the ceiling. Check it any time:

```bash
python autoreel.py status
```

## Metadata

Rule-based, not generative: the same video always produces the same title, and
nothing is asserted about the footage that the analyzer did not detect. Copy
`profile.example.json` to `profile.json` to set the standing description, fixed
tags, category and default privacy.

Vertical video of 3 minutes or less gets ` #Shorts` appended to the title,
which is what actually classifies a Short — the aspect ratio alone does not.

## What is verified

Tempo detection was checked against generated click tracks: **90 → 90.00,
100 → 100.00, 128 → 128.00, 174 → 173.99 BPM**, with the beat phase inside
about 1.5 ms, taking ~0.4 s per track on the pure-Python path. A real
3-minute MP3 detected as 133 BPM in 2.3 s and rendered end to end.

No reliability score is reported, and that is a measured decision rather than
an omission. The natural candidate — how tightly onset energy folds onto a
single phase — separates a click track (19.7× uniform) from everything else,
but scores real music at 4.01× and a beatless sine tone at 4.00×. Sharpening
the envelope with adaptive whitening moved those to 5.54× and 4.73×, still too
close to act on, and did not change either detected tempo. A confidence number
that cannot tell music from a test tone would be worse than none.

Rendering, the beat grid, the duration probe, metadata, the watch loop's
dedup, and the quota refusal were all exercised on generated test media:
a 12-second reel came out `1080x1920`, H.264 + AAC, 13 cuts at 128 BPM.

The server was driven end to end in a browser: files uploaded through the page
arrived byte-identical, a render ran from the UI (22 cuts, 128 BPM, 19s) and
played back in the preview, and a dry-run publish produced the expected title
and tags. Auth was checked directly — reads 200 without a token, mutations 401
without it and 401 with a wrong one, malformed JSON 400, and a routable bind
with no token refused to start.

The **upload call itself has not been run against the live API** — that needs
your OAuth client, and it would post a real video. `--dry-run` covers
everything up to the transfer.

## Notes

- Nothing is ever uploaded twice: `.uploaded.json` keys on filename + size, so
  a re-render counts as a new video but a re-scan does not.
- `watch` waits for a file to stop growing before touching it, so a video still
  being written is not uploaded half-finished.
- A dry run is never written to the upload state — it would make the file look
  uploaded and block the real run later.

[reelmaker]: https://github.com/baglady/dspm-archive
