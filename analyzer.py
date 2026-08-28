"""
What is actually in the video: tempo and brightness from the audio, objects
and time-of-day from the frames.

The heavy dependencies (torch, ultralytics, opencv, librosa) are imported
inside the methods, so an upload-only run does not pay for them -- and
`--no-analyze` needs none of them installed at all.
"""
from __future__ import annotations

from collections import Counter


class AudioAnalyzer:
    def analyze(self, video_path):
        """
        Extract audio from the video and describe it: BPM, brightness.

        librosa gives the spectral centroid as well, but it is a heavy
        dependency and not always installed, so tempo falls back to the
        dependency-free detector -- which is the one the renderer uses anyway.
        """
        try:
            import librosa
            import numpy as np

            y, sr = librosa.load(str(video_path), sr=None)
            beat, _ = librosa.beat.beat_track(y=y, sr=sr)
            centroids = librosa.feature.spectral_centroid(y=y, sr=sr)[0]

            return {
                'bpm': round(float(np.atleast_1d(beat)[0]), 1),
                'audio_brightness': float(np.mean(centroids)),
                'duration': float(librosa.get_duration(y=y, sr=sr)),
            }
        except ImportError:
            import tempo
            found = tempo.detect(video_path)
            return {'bpm': found['bpm'], 'duration': found['duration'],
                    'audio_brightness': None}
        except Exception as e:
            return {'bpm': 0, 'error': str(e)}


class VideoAnalyzer:
    """Object detection over sampled frames. Loads YOLO once, on first use."""

    def __init__(self, model_name: str = 'yolov8n.pt', sample_interval: float = 5.0):
        self.model_name = model_name
        self.sample_interval = sample_interval
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from ultralytics import YOLO
            self._model = YOLO(self.model_name)   # downloads on first run
        return self._model

    def analyze(self, video_path):
        import cv2
        import numpy as np

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return {'error': 'could not open video'}

        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        duration = frame_count / fps if fps > 0 else 0

        step = max(1, int(fps * self.sample_interval))
        detected, brightness_values = [], []

        current = 0
        while current < frame_count:
            cap.set(cv2.CAP_PROP_POS_FRAMES, current)
            ok, frame = cap.read()
            if not ok:
                break

            for result in self.model(frame, verbose=False):
                for box in result.boxes:
                    detected.append(self.model.names[int(box.cls[0])])

            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            brightness_values.append(float(np.mean(hsv[:, :, 2])))
            current += step

        cap.release()

        avg_brightness = float(np.mean(brightness_values)) if brightness_values else 0.0
        return {
            'duration_sec': duration,
            'objects': [name for name, _ in Counter(detected).most_common(5)],
            'avg_brightness': avg_brightness,
            'time_of_day': 'Day' if avg_brightness > 100 else 'Night',
        }


def probe(video_path, audio: bool = True, video: bool = True) -> dict:
    """Both analyzers, merged into the flat dict the metadata step expects."""
    facts = {}
    if video:
        facts.update(VideoAnalyzer().analyze(video_path))
    if audio:
        a = AudioAnalyzer().analyze(video_path)
        facts['bpm'] = a.get('bpm') or facts.get('bpm')
        facts['audio_brightness'] = a.get('audio_brightness')
        facts.setdefault('duration_sec', a.get('duration', 0))
    return facts


if __name__ == '__main__':
    import json
    import sys

    if len(sys.argv) < 2:
        raise SystemExit('usage: python analyzer.py <video>')
    print(json.dumps(probe(sys.argv[1]), indent=2))
