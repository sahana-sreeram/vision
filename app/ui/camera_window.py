"""Small always-on-top camera preview docked at the top-left of the screen."""

import tkinter as tk

import cv2
from PIL import Image, ImageTk

from ..actions import ACTIONS

VIEW_W, VIEW_H = 320, 240
POLL_MS = 33


class CameraWindow(tk.Toplevel):
    def __init__(self, master, controller):
        super().__init__(master)
        self.withdraw()
        self.c = controller
        self.title("Camera")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.geometry(f"{VIEW_W}x{VIEW_H}+10+10")
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self.label = tk.Label(self, bg="black", width=VIEW_W, height=VIEW_H)
        self.label.pack()
        self.bind("a", self._toggle_adaptive)
        self._photo = None
        self.after(POLL_MS, self._poll)

    def _toggle_adaptive(self, event=None):
        with self.c.worker.engine_lock:
            self.c.engine.adaptive.enabled = not self.c.engine.adaptive.enabled

    def show(self):
        self.deiconify()
        self.lift()

    def _poll(self):
        s = self.c.worker.snapshot()
        frame = s["frame"]
        if frame is not None:
            frame = cv2.resize(frame, (VIEW_W, VIEW_H))
            frame = cv2.flip(frame, 1)  # mirror so it behaves like a mirror for the user
            text, color = self._caption(s)
            if text:
                cv2.putText(frame, text, (8, VIEW_H - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
                cv2.putText(frame, text, (8, VIEW_H - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
            if self.c.paused:
                cv2.putText(frame, "PAUSED", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
            if getattr(self.c.backend, "listening", False):
                cv2.putText(frame, "DICTATING", (VIEW_W - 118, 22),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (80, 255, 80), 2)
            status = s.get('cursor_label', 'UNTRAINED')
            adaptive = 'ON' if s.get('adaptive_enabled', False) else 'OFF'
            cv2.putText(frame, f'{status} {s.get("cursor_score", 0):.2f}',
                        (8, 42), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
            cv2.putText(frame, f'Adaptive {adaptive} (a)', (8, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            self._photo = ImageTk.PhotoImage(Image.fromarray(rgb))
            self.label.configure(image=self._photo)
        elif s["error"]:
            self.label.configure(text=s["error"], fg="white", image="")
        self.after(POLL_MS, self._poll)

    @staticmethod
    def _caption(s):
        m = s["active"] or s["candidate"]
        if not m:
            return "", None
        label = ACTIONS[m.action].label
        if s["active"]:
            return f"{m.gesture.describe()} -> {label}", (80, 255, 80)
        return f"{m.gesture.describe()} -> {label} (hold)", (200, 200, 200)
