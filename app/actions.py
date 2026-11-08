"""Action registry and the pyautogui / speech backend that performs them."""

import sys
import threading
import time
from dataclasses import dataclass

CONTINUOUS, ONESHOT, BUTTON = "continuous", "oneshot", "button"

SCROLL_STEP = 60
SCROLL_INTERVAL = 0.1      # seconds between scroll steps while held
WHISPER_MODEL = "base"     # local faster-whisper model; downloaded on first dictate
LIVE_RATE = 16000
LIVE_CHUNK_SECONDS = 1.2   # transcribe about this often so words appear while you speak
LIVE_SPEECH_RMS = 0.006    # above this mic's room noise (~0.0045), low enough for quiet speech


@dataclass(frozen=True)
class Action:
    id: str
    label: str
    kind: str
    moves_cursor: bool = False
    default_hold_ms: int = 120
    hint: str = ""


ACTIONS: dict[str, Action] = {a.id: a for a in [
    Action("move", "Move pointer", CONTINUOUS, moves_cursor=True, default_hold_ms=0),
    Action("left_button", "Left button", BUTTON, moves_cursor=True, default_hold_ms=0,
           hint="tap = click, hold = drag, two taps = double click"),
    Action("left_click", "Left click", ONESHOT),
    Action("right_click", "Right click", ONESHOT),
    Action("double_click", "Double click", ONESHOT),
    Action("drag", "Drag", BUTTON, moves_cursor=True, hint="holds the left button while moving"),
    Action("move_window", "Move window", BUTTON, moves_cursor=True,
           hint="hold to carry the window under the pointer"),
    Action("scroll_up", "Scroll up", CONTINUOUS),
    Action("scroll_down", "Scroll down", CONTINUOUS),
    Action("press_enter", "Enter", ONESHOT, default_hold_ms=400, hint="presses the Enter key"),
    Action("press_delete", "Delete", ONESHOT, default_hold_ms=400,
           hint="deletes the character before the text cursor"),
    Action("toggle_gestures", "Pause / resume", ONESHOT, default_hold_ms=1500,
           hint="works while paused too"),
]}

TOGGLE_ACTION = "toggle_gestures"
KEY_ACTIONS = {"press_enter": "enter", "press_delete": "backspace"}  # action -> pyautogui key


class Backend:
    """Interface the engine drives. Replace in tests."""

    def move_to(self, x: float, y: float): ...
    def mouse_down(self): ...
    def mouse_up(self): ...
    def click(self, button: str = "left"): ...
    def double_click(self): ...
    def scroll(self, amount: int): ...
    def press_key(self, key: str): ...
    speech_available = False
    dictation_state = "idle"

    def start_dictate(self) -> bool: return False
    def stop_dictate(self): ...
    def screen_size(self) -> tuple[int, int]: return (1920, 1080)
    def set_own_targets(self, rects): ...
    def target_at(self, x, y):
        """Screen rect (x, y, w, h) of a button-like control under the point, or None."""
        return None
    # window carrying (optional): return False if unsupported, the engine then drags instead
    def grab_window_at(self, x: int, y: int) -> bool: return False
    def move_grabbed_window(self, dx: int, dy: int): ...
    def release_window(self): ...


class _TargetLookup:
    """Asks UI Automation what is under a point on its own thread: a lookup takes
    20-40 ms, too long for the camera loop to wait on. The answer for the most recent
    point is handed back on the next request near it."""
    NEAR_PX = 40
    FRESH_S = 0.4

    def __init__(self):
        self._pending = None
        self._result = None          # (x, y, rect, time)
        self._wake = threading.Event()
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def request(self, x, y):
        with self._lock:
            self._pending = (x, y)
            r = self._result
        self._wake.set()
        if r and abs(r[0] - x) <= self.NEAR_PX and abs(r[1] - y) <= self.NEAR_PX                 and time.monotonic() - r[3] <= self.FRESH_S:
            return r[2]
        return None

    def _run(self):
        from .caret_win import clickable_at
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                point, self._pending = self._pending, None
            if point is None:
                continue
            rect = clickable_at(*point)
            with self._lock:
                self._result = (point[0], point[1], rect, time.monotonic())


class PyAutoGuiBackend(Backend):
    def __init__(self):
        import pyautogui
        pyautogui.PAUSE = 0
        pyautogui.FAILSAFE = False
        self._gui = pyautogui
        self._last_scroll = 0.0
        self._dictation_state = "idle"  # idle | loading | listening | finishing
        self._stop_listening = threading.Event()
        self._whisper_model = None
        self.speech_available = False
        self.speech_error = ""
        try:
            import speech_recognition as sr
            from faster_whisper import WhisperModel
            self._sr = sr
            self._WhisperModel = WhisperModel
            sr.Microphone()  # raises if PyAudio or a microphone is missing
            self.speech_available = True
        except Exception as e:  # missing PyAudio, no microphone, or no Whisper
            self.speech_error = str(e)

    def screen_size(self):
        return self._gui.size()

    # clickable targets, for the magnetic cursor
    def set_own_targets(self, rects):
        """Rects of this app's own buttons and switches; UI Automation cannot see inside Tk."""
        self._own_targets = list(rects)

    def target_at(self, x, y):
        inside = [r for r in getattr(self, "_own_targets", ())
                  if r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]]
        if inside:
            return min(inside, key=lambda r: r[2] * r[3])
        if sys.platform == "win32":
            if not hasattr(self, "_lookup"):
                self._lookup = _TargetLookup()
            return self._lookup.request(x, y)
        return None

    def move_to(self, x, y):
        self._gui.moveTo(x, y)

    def mouse_down(self):
        self._gui.mouseDown()

    def mouse_up(self):
        self._gui.mouseUp()

    def click(self, button="left"):
        self._gui.click(button=button)

    def press_key(self, key):
        self._gui.press(key)

    def double_click(self):
        self._gui.doubleClick()

    # window carrying
    def grab_window_at(self, x, y):
        if sys.platform != "win32":
            return False
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        hwnd = u.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        if not hwnd:
            return False
        hwnd = u.GetAncestor(hwnd, 2)  # GA_ROOT: the top-level window
        if hwnd in (u.GetDesktopWindow(), u.GetShellWindow()):
            return False
        cls = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(hwnd, cls, 64)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
            return False
        if u.IsZoomed(hwnd):          # maximised windows do not move
            return False
        rect = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        self._grab = (hwnd, rect.left, rect.top)
        return True

    def move_grabbed_window(self, dx, dy):
        if not getattr(self, "_grab", None):
            return
        import ctypes
        hwnd, left, top = self._grab
        SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10
        ctypes.windll.user32.SetWindowPos(hwnd, 0, int(left + dx), int(top + dy), 0, 0,
                                          SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    def release_window(self):
        self._grab = None

    def scroll(self, amount):
        now = time.monotonic()
        if now - self._last_scroll >= SCROLL_INTERVAL:
            self._gui.scroll(amount)
            self._last_scroll = now

    @property
    def dictation_state(self):
        return self._dictation_state

    @property
    def listening(self):
        return self._dictation_state != "idle"

    def start_dictate(self) -> bool:
        if not self.speech_available or self._dictation_state != "idle":
            return False
        self._stop_listening.clear()
        self._dictation_state = "loading"
        threading.Thread(target=self._dictate_worker, daemon=True).start()
        return True

    def stop_dictate(self):
        if self._dictation_state != "idle":
            print("Dictate off.", file=sys.stderr)
        self._stop_listening.set()

    def _whisper(self):
        if self._whisper_model is None:
            print(f"Loading Whisper ({WHISPER_MODEL}).", file=sys.stderr)
            self._whisper_model = self._WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        return self._whisper_model

    def _transcribe_samples(self, samples, prompt):
        segments, _info = self._whisper().transcribe(
            samples,
            beam_size=1,
            language="en",
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
            without_timestamps=True,
        )
        return "".join(segment.text for segment in segments).strip()

    def _dictate_worker(self):
        import numpy as np
        rate = LIVE_RATE
        mic = self._sr.Microphone(sample_rate=rate, chunk_size=int(rate * 0.1))
        blocks = []
        lock = threading.Lock()
        stop_read = threading.Event()

        def read_mic():
            try:
                with mic as source:
                    while not stop_read.is_set():
                        raw = source.stream.read(source.CHUNK)
                        block = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
                        with lock:
                            blocks.append(block)
            except Exception as e:
                print(f"Dictate failed: {e}", file=sys.stderr)
                stop_read.set()

        def snapshot():
            with lock:
                if not blocks:
                    return np.empty(0, dtype=np.float32)
                return np.concatenate(blocks)

        def consume(end, keep):
            """Drop audio before `end`, leaving `keep` samples of overlap. New mic
            samples appended during transcription stay in the buffer."""
            with lock:
                if not blocks:
                    return 0
                audio = np.concatenate(blocks)
                end = min(end, audio.size)
                drop = max(0, end - keep)
                rest = audio[drop:]
                blocks.clear()
                if rest.size:
                    blocks.append(rest)
                return end - drop

        try:
            self._whisper()
            print("Listening. Press Stop on screen to end.", file=sys.stderr)
            reader = threading.Thread(target=read_mic, daemon=True)
            reader.start()
            self._dictation_state = "listening"
            typed = ""
            processed = 0
            overlap = int(0.25 * rate)
            while not stop_read.is_set() and not self._stop_listening.is_set():
                time.sleep(0.12)
                audio = snapshot()
                pending = audio.size - processed
                if pending < int(LIVE_CHUNK_SECONDS * rate):
                    continue
                fresh = audio[processed:]
                if float(np.sqrt(np.mean(fresh ** 2))) < LIVE_SPEECH_RMS:
                    processed = consume(audio.size, 0)
                    continue
                piece = self._transcribe_samples(audio[max(0, processed - overlap):], typed[-160:])
                addition = _new_dictation(typed, piece)
                if addition:
                    self._gui.write(addition, interval=0)
                    typed += addition
                processed = consume(audio.size, overlap)
            self._dictation_state = "finishing"
            audio = snapshot()
            tail = audio[processed:]
            if tail.size >= int(0.3 * rate) and float(np.sqrt(np.mean(tail ** 2))) >= LIVE_SPEECH_RMS:
                piece = self._transcribe_samples(audio[max(0, processed - overlap):], typed[-160:])
                addition = _new_dictation(typed, piece)
                if addition:
                    self._gui.write(addition, interval=0)
        except Exception as e:
            print(f"Dictate failed: {e}", file=sys.stderr)
        finally:
            stop_read.set()
            self._dictation_state = "idle"


def _new_dictation(already, piece):
    """Text from this chunk that has not been typed yet."""
    piece = " ".join(piece.split())
    if not piece:
        return ""
    prev = already.strip()
    if prev:
        low_piece = piece.lower()
        low_prev = prev.lower()
        if low_piece.startswith(low_prev):
            piece = piece[len(prev):].strip()
        else:
            # The chunk overlaps the previous audio, so drop a repeated ending.
            cut = 0
            limit = min(len(low_prev), len(low_piece), 40)
            for n in range(limit, 0, -1):
                if low_piece.startswith(low_prev[-n:]):
                    cut = n
                    break
            piece = piece[cut:].strip()
    if not piece:
        return ""
    if already and not already.endswith((" ", "\n")) and piece[0] not in ",.!?;:'":
        return " " + piece
    return piece
