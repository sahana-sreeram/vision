"""Owns the gesture set, settings, engine and worker; the UI talks to this and listens for changes."""

import sys

from .actions import PyAutoGuiBackend
from .engine import Engine
from .profiles import ROOT, Mapping, Profile, ProfileStore
from .worker import Worker

HOTKEY = "<ctrl>+<alt>+g"
HOTKEY_LABEL = "Ctrl+Alt+G"
PROFILE_ID = "default"


class Controller:
    def __init__(self, camera_index=0, frame_size=(640, 480)):
        self.store = ProfileStore()
        self.store.ensure_bundled()
        self.profile: Profile = self.store.load(PROFILE_ID)
        self.backend = PyAutoGuiBackend()
        self.engine = Engine(self.backend, frame_size=frame_size)
        self.worker = Worker(self.engine, camera_index, frame_size, on_toggle=self.toggle_paused)
        self.engine.set_profile(self.profile)

        self.paused = False
        self.camera_visible = False   # always starts hidden; the switch shows it for this session
        self.magnifier_on = bool(self.store.get_setting("magnifier", True))
        self.hover_lock = bool(self.store.get_setting("hover_lock", True))
        self.engine.hover_lock = self.hover_lock

        self._listeners = []
        # UI sets this to hop listener calls onto its thread (e.g. tk's after)
        self.dispatch = lambda fn: fn()
        self._hotkey = None

    # lifecycle
    def start(self):
        self.worker.start()

    def stop(self):
        self.backend.stop_dictate()
        if self._hotkey:
            self._hotkey.stop()
        self.worker.stop()
        self.worker.join(timeout=2)
        with self.worker.engine_lock:
            self.engine.release()

    # listeners: fn(event) with event in {"profile", "paused", "camera", "magnifier"}
    def listen(self, fn):
        self._listeners.append(fn)

    def _notify(self, event):
        def run():
            for fn in list(self._listeners):
                fn(event)
        self.dispatch(run)

    # global hotkey
    def start_hotkey(self, combo: str = HOTKEY) -> bool:
        try:
            from pynput import keyboard
            self._hotkey = keyboard.GlobalHotKeys({combo: self.toggle_paused})
            self._hotkey.daemon = True
            self._hotkey.start()
            return True
        except Exception as e:
            print(f"Global hotkey unavailable: {e}", file=sys.stderr)
            return False

    # gesture set
    def reset_defaults(self):
        self.store.ensure_bundled(overwrite=True)
        self.profile = self.store.load(PROFILE_ID)
        with self.worker.engine_lock:
            self.engine.set_profile(self.profile)
        self._notify("profile")

    def set_mapping(self, mapping: Mapping, replace_conflict: bool = False):
        conflict = self.profile.conflict(mapping.gesture, mapping.action)
        if conflict and replace_conflict:
            self.profile.clear_mapping(conflict.action)
        self.profile.set_mapping(mapping)
        self._save_and_apply()

    def clear_mapping(self, action_id: str):
        self.profile.clear_mapping(action_id)
        self._save_and_apply()

    def _save_and_apply(self):
        self.store.save(self.profile)
        with self.worker.engine_lock:
            self.engine.refresh_profile()
        self._notify("profile")

    def snapshot_path(self, action_id: str) -> str:
        """Path for a recorded gesture image, relative to the repo root when possible."""
        path = self.store.dir / "images" / f"{self.profile.id}_{action_id}.png"
        try:
            return str(path.relative_to(ROOT))
        except ValueError:
            return str(path)

    # gesture control on/off
    def set_paused(self, paused: bool):
        self.paused = paused
        with self.worker.engine_lock:
            self.engine.set_paused(paused)
        self._notify("paused")

    def toggle_paused(self):
        self.set_paused(not self.paused)

    def suspend(self, suspended: bool):
        """Stop all gesture actions temporarily (while recording a pose)."""
        with self.worker.engine_lock:
            self.engine.set_suspended(suspended)

    # camera view and magnifier
    def set_camera_visible(self, visible: bool):
        self.camera_visible = visible
        self._notify("camera")

    def toggle_camera(self):
        self.set_camera_visible(not self.camera_visible)

    def set_magnifier(self, on: bool):
        self.magnifier_on = on
        self.store.set_setting("magnifier", on)
        self._notify("magnifier")

    def toggle_magnifier(self):
        self.set_magnifier(not self.magnifier_on)

    def set_hover_lock(self, on: bool):
        """Lock the cursor after holding still briefly; off lets tiny corrections through."""
        self.hover_lock = on
        self.store.set_setting("hover_lock", on)
        with self.worker.engine_lock:
            self.engine.hover_lock = on
        self._notify("hover")

    def toggle_hover_lock(self):
        self.set_hover_lock(not self.hover_lock)
