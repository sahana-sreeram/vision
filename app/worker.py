"""Background thread: capture -> detect -> features -> engine. Publishes state for the UI."""

import threading
import time

import cv2

from . import features as feat
from .detector import HandTracker, draw_hand
from .engine import Engine


class Worker(threading.Thread):
    def __init__(self, engine: Engine, camera_index=0, frame_size=(640, 480), on_toggle=None):
        super().__init__(daemon=True, name="gesture-worker")
        self.engine = engine
        self.on_toggle = on_toggle
        self.engine_lock = threading.Lock()   # UI holds this when reconfiguring the engine
        self.camera_index = camera_index
        self.frame_w, self.frame_h = frame_size
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # Started here, on the main thread: a spawned process is unreliable from run().
        self.tracker = HandTracker()
        self._state = {"frame": None, "raw_frame": None, "features": None,
                       "active": None, "candidate": None, "fps": 0.0, "error": None}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def stop(self):
        self._stop.set()

    def run(self):
        cap = cv2.VideoCapture(self.camera_index)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.frame_w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.frame_h)
        if not cap.isOpened():
            self._publish(error=f"Could not open camera {self.camera_index}")
            self.tracker.close()
            return
        detector = self.tracker

        prev = time.monotonic()
        margin = self.engine.margin
        try:
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None or frame.size == 0:
                    self._publish(error="Camera read failed")
                    time.sleep(0.1)
                    continue
                if frame.shape[1] != self.frame_w or frame.shape[0] != self.frame_h:
                    frame = cv2.resize(frame, (self.frame_w, self.frame_h))
                raw = frame.copy()
                now = time.monotonic()
                landmarks = detector.detect(frame, int(now * 1000))
                f = feat.extract(landmarks) if landmarks else None

                with self.engine_lock:
                    self.engine.update(f, now)
                    active, candidate = self.engine.active, self.engine.candidate
                    cursor_label = self.engine.cursor_label
                    cursor_score = self.engine.cursor_score
                    adaptive_enabled = self.engine.adaptive.enabled
                    toggle = self.engine.toggle_requested
                    self.engine.toggle_requested = False
                if toggle and self.on_toggle:
                    self.on_toggle()

                cv2.rectangle(frame, (margin, margin), (self.frame_w - margin, self.frame_h - margin),
                              (255, 0, 255), 1)
                if landmarks:
                    draw_hand(frame, landmarks, (0, 255, 0) if active else (255, 0, 255))
                    px, py = f.pointer
                    cv2.circle(frame, (int(px), int(py)), 8, (0, 200, 255), 2)

                fps = 1.0 / max(now - prev, 1e-6)
                prev = now
                self._publish(frame=frame, raw_frame=raw, features=f, active=active,
                              candidate=candidate, fps=fps, error=None, cursor_label=cursor_label,
                              cursor_score=cursor_score, adaptive_enabled=adaptive_enabled)
        finally:
            with self.engine_lock:
                self.engine.release()
            self.tracker.close()
            cap.release()

    def _publish(self, **kv):
        with self._lock:
            self._state.update(kv)
