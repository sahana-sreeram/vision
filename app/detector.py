"""Webcam frame -> 21 hand landmarks via the mediapipe Tasks HandLandmarker."""

import os

# MediaPipe ships its own OpenCV. With more than one thread, a bad frame makes
# warpPerspective throw on a worker thread, and that abort takes down the app.
os.environ.setdefault("OPENCV_FOR_THREADS_NUM", "1")

import multiprocessing
import queue
import time
import urllib.request
from pathlib import Path

import cv2
import mediapipe as mp

from .handmodel import BONES

MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
             "hand_landmarker/float16/latest/hand_landmarker.task")
MODEL_PATH = Path(__file__).resolve().parent.parent / "assets" / "hand_landmarker.task"

FINGERTIPS = (4, 8, 12, 16, 20)


def ensure_model(path: Path = MODEL_PATH, log=print) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        log(f"Downloading hand landmark model to {path} ...")
        urllib.request.urlretrieve(MODEL_URL, path)
        log("Done.")
    return path


def _single_thread_bundled_opencv():
    """Call cv::setNumThreads(1) inside MediaPipe's private OpenCV.

    The symbol is not exported, so it is resolved from the loaded image.
    Failure is ignored: the detector process below still contains the crash.
    """
    try:
        import ctypes
        from ctypes import c_char_p, c_int, c_int64, c_uint32, c_void_p
        libc = ctypes.CDLL(None)
        count = libc._dyld_image_count
        count.restype = c_uint32
        name_of = libc._dyld_get_image_name
        name_of.argtypes = [c_uint32]
        name_of.restype = c_char_p
        header_of = libc._dyld_get_image_header
        header_of.argtypes = [c_uint32]
        header_of.restype = c_void_p
        slide_of = libc._dyld_get_image_vmaddr_slide
        slide_of.argtypes = [c_uint32]
        slide_of.restype = c_int64
        base = slide = None
        for i in range(count()):
            name = name_of(i) or b""
            if b"mediapipe" in name and b"_framework_bindings" in name and name.endswith(b".so"):
                base, slide = header_of(i), slide_of(i)
                break
        if base is None:
            return

        def u32(off):
            return ctypes.c_uint32.from_address(base + off).value

        def u64(off):
            return ctypes.c_uint64.from_address(base + off).value

        link_vm = link_file = symoff = nsyms = stroff = None
        off, ncmds = 32, u32(16)
        for _ in range(ncmds):
            cmd, size = u32(off), u32(off + 4)
            if cmd == 0x19 and ctypes.string_at(base + off + 8, 16).split(b"\0", 1)[0] == b"__LINKEDIT":
                link_vm, link_file = u64(off + 24), u64(off + 40)
            elif cmd == 0x2:
                symoff, nsyms, stroff = u32(off + 8), u32(off + 12), u32(off + 16)
            off += size
        sym = base + link_vm + (symoff - link_file)
        strings = base + link_vm + (stroff - link_file)
        target = None
        for i in range(nsyms):
            ent = sym + i * 16
            strx = ctypes.c_uint32.from_address(ent).value
            if strx and ctypes.string_at(strings + strx) == b"__ZN2cv13setNumThreadsEi":
                target = ctypes.c_uint64.from_address(ent + 8).value
                break
        if target is not None:
            ctypes.CFUNCTYPE(None, c_int)(slide + target)(1)
    except Exception:
        pass


class HandDetector:
    def __init__(self, model_path: Path = MODEL_PATH, max_hands=1, confidence=0.5):
        _single_thread_bundled_opencv()
        vision = mp.tasks.vision
        options = vision.HandLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=max_hands,
            min_hand_detection_confidence=confidence,
            min_hand_presence_confidence=confidence,
            min_tracking_confidence=confidence,
        )
        self._landmarker = vision.HandLandmarker.create_from_options(options)
        self._last_ts = -1

    def detect(self, frame_bgr, timestamp_ms: int):
        """Returns a list of 21 (x, y) pixel tuples for the first hand, or None."""
        frame_bgr = _prepare_frame(frame_bgr)
        if frame_bgr is None:
            return None
        if timestamp_ms <= self._last_ts:
            timestamp_ms = self._last_ts + 1
        self._last_ts = timestamp_ms
        try:
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = self._landmarker.detect_for_video(image, timestamp_ms)
        except Exception:
            return None
        if not result.hand_landmarks:
            return None
        h, w = frame_bgr.shape[:2]
        # z is relative depth in roughly the same units as x; keep it so pinch
        # geometry can be measured in 3D and survive the hand tilting
        return [(lm.x * w, lm.y * h, lm.z * w) for lm in result.hand_landmarks[0]]

    def close(self):
        self._landmarker.close()


def _prepare_frame(frame):
    """A contiguous even-sized BGR image, or None when the camera frame is unusable.

    An empty or odd-sized frame is what makes MediaPipe's warp throw."""
    if frame is None or getattr(frame, "ndim", 0) != 3 or frame.shape[2] != 3 or frame.size == 0:
        return None
    frame = frame[:frame.shape[0] & ~1, :frame.shape[1] & ~1]
    if frame.shape[0] < 2 or frame.shape[1] < 2:
        return None
    if not frame.flags["C_CONTIGUOUS"] or frame.dtype.name != "uint8":
        frame = frame.copy() if frame.dtype.name == "uint8" else frame.astype("uint8")
    return frame


def _serve(in_q, out_q, model_path):
    """Run the landmarker in this process. An abort here no longer kills the app."""
    try:
        detector = HandDetector(Path(model_path))
    except Exception as e:
        out_q.put(("err", str(e)))
        return
    out_q.put(("ready", None))
    try:
        while True:
            item = in_q.get()
            if item is None:
                break
            timestamp_ms, frame = item
            out_q.put(("ok", detector.detect(frame, timestamp_ms)))
    finally:
        detector.close()


class HandTracker:
    """HandDetector in its own process, so a MediaPipe abort only drops one frame."""

    def __init__(self, model_path: Path = MODEL_PATH):
        self._ctx = multiprocessing.get_context("spawn")
        self._model = str(model_path)
        self._proc = self._in = self._out = None
        self._retry_at = 0.0
        self._start()

    def _start(self):
        self._in = self._ctx.Queue(maxsize=1)
        self._out = self._ctx.Queue(maxsize=1)
        self._proc = self._ctx.Process(target=_serve, args=(self._in, self._out, self._model),
                                       daemon=True)
        self._proc.start()
        try:
            status, payload = self._out.get(timeout=60)
        except queue.Empty:
            self.close()
            raise RuntimeError("Hand tracker did not start")
        if status != "ready":
            self.close()
            raise RuntimeError(payload or "Hand tracker failed to start")

    def detect(self, frame, timestamp_ms: int):
        frame = _prepare_frame(frame)
        if frame is None:
            return None
        if time.monotonic() < self._retry_at:
            return None
        if self._proc is None or not self._proc.is_alive():
            self._restart()
            return None
        try:
            self._in.put((int(timestamp_ms), frame), timeout=0.5)
            status, marks = self._out.get(timeout=2)
        except Exception:
            self._restart()
            return None
        return marks if status == "ok" else None

    def _restart(self):
        print("Hand tracking stopped unexpectedly and is restarting.", flush=True)
        self.close()
        try:
            self._start()
        except Exception as e:
            self._retry_at = time.monotonic() + 5
            print(f"Hand tracking failed to restart: {e}", flush=True)

    def close(self):
        proc, in_q, out_q = self._proc, self._in, self._out
        self._proc = self._in = self._out = None
        if in_q is not None:
            try:
                in_q.put(None, timeout=0.2)
            except Exception:
                pass
            try:
                in_q.close()
                in_q.cancel_join_thread()
            except Exception:
                pass
        if out_q is not None:
            try:
                out_q.close()
                out_q.cancel_join_thread()
            except Exception:
                pass
        if proc is None:
            return
        proc.join(timeout=1)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=1)


def draw_hand(frame, landmarks, color=(0, 255, 0)):
    pts = [(int(p[0]), int(p[1])) for p in landmarks]
    for a, b in BONES:
        cv2.line(frame, pts[a], pts[b], (255, 255, 255), 2)
    for i, p in enumerate(pts):
        cv2.circle(frame, p, 6 if i in FINGERTIPS else 3, color, cv2.FILLED)
