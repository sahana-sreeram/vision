"""Dialog for changing one action's gesture: pick a preset, or record your own pose."""

import tkinter as tk
from collections import Counter
from tkinter import messagebox, ttk

from PIL import ImageTk

from ..actions import ACTIONS
from ..gestures import PRESET_GESTURES, Gesture
from ..icons import render, save_snapshot
from ..profiles import ROOT, Mapping

COUNTDOWN_S = 3        # "get ready" countdown before recording starts
RECORD_S = 4           # how long the pose is recorded
SAMPLE_MS = 50
PREVIEW = 128
CUSTOM = "Custom (recorded)"

# Same purples as the side pane and the dictation microphone bubble.
BG = "#F4EEFB"
CARD = "#ffffff"
BORDER = "#E2D5F3"
TEXT = "#2A1454"
MUTED = "#6B5B8A"
FAINT = "#A596C2"
PURPLE = "#4C1D95"
PURPLE_DARK = "#3B1578"
RED = "#d9534f"
FONT = "Segoe UI"


class MappingEditor(tk.Toplevel):
    def __init__(self, master, controller, action_id):
        super().__init__(master, bg=BG)
        self.c = controller
        self.action = ACTIONS[action_id]
        self.title(self.action.label)
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.scale = getattr(master, "scale", 1.0)

        current = controller.profile.mapping_for(action_id)
        self.gesture = current.gesture if current else PRESET_GESTURES[0][1]
        self.image_path = current.image if current else None
        self._recorded_frame = None
        self._recorded_bbox = None
        self._samples = []
        self._recording = False
        self._photo = None

        self._style()
        self._build(current)
        self._show_preview()
        self.update_idletasks()
        x = master.winfo_rootx() - self.winfo_reqwidth() - int(12 * self.scale)
        self.geometry(f"+{max(0, x)}+{master.winfo_rooty() + int(80 * self.scale)}")

    def _style(self):
        st = ttk.Style(self)
        st.configure("Accent.TButton", background=PURPLE, foreground="white", bordercolor=PURPLE,
                     focuscolor=PURPLE, padding=(12, 5), font=(FONT, 9, "bold"))
        st.map("Accent.TButton", background=[("active", PURPLE_DARK)], bordercolor=[("active", PURPLE_DARK)])
        st.configure("Quiet.TButton", background=CARD, foreground=TEXT, bordercolor=BORDER,
                     focuscolor=CARD, padding=(12, 5), font=(FONT, 9))
        st.map("Quiet.TButton", background=[("active", BG)])
        st.configure("Danger.TButton", background=CARD, foreground=RED, bordercolor=BORDER,
                     focuscolor=CARD, padding=(12, 5), font=(FONT, 9))
        st.map("Danger.TButton", background=[("active", BG)])

    def _p(self, n):
        return int(n * self.scale)

    def _label(self, parent, text, **kw):
        kw.setdefault("bg", BG)
        kw.setdefault("fg", TEXT)
        kw.setdefault("font", (FONT, 9))
        kw.setdefault("anchor", "w")
        return tk.Label(parent, text=text, **kw)

    def _build(self, current):
        body = tk.Frame(self, bg=BG, padx=self._p(16), pady=self._p(14))
        body.pack(fill="both", expand=True)

        self._label(body, self.action.label, font=(FONT, 13, "bold")).grid(row=0, column=0, sticky="w")
        if self.action.hint:
            self._label(body, self.action.hint, fg=MUTED, font=(FONT, 8)).grid(row=1, column=0, sticky="w")

        # preview card on the right
        card = tk.Frame(body, bg=BORDER, padx=1, pady=1)
        card.grid(row=0, column=1, rowspan=6, padx=(self._p(16), 0), sticky="n")
        self.preview = tk.Label(card, bg=CARD, width=PREVIEW, height=PREVIEW)
        self.preview.pack()
        self.desc = tk.Label(card, text="", bg=CARD, fg=TEXT, font=(FONT, 8), wraplength=PREVIEW - 8,
                             justify="center", pady=4)
        self.desc.pack(fill="x")

        self._label(body, "GESTURE", fg=FAINT, font=(FONT, 8, "bold")).grid(
            row=2, column=0, sticky="w", pady=(self._p(14), self._p(2)))
        names = [n for n, _ in PRESET_GESTURES]
        self.preset_box = ttk.Combobox(body, state="readonly", values=names + [CUSTOM], width=34, font=(FONT, 9))
        self.preset_box.grid(row=3, column=0, sticky="we")
        self.preset_box.bind("<<ComboboxSelected>>", self._on_preset)
        self._select_preset_for(self.gesture)

        rec = tk.Frame(body, bg=BG)
        rec.grid(row=4, column=0, sticky="we", pady=(self._p(8), 0))
        self.record_btn = ttk.Button(rec, text="Record my own pose", style="Quiet.TButton",
                                     command=self._toggle_record)
        self.record_btn.pack(side="left")
        self.record_status = tk.Label(rec, text="", bg=BG, fg=MUTED, font=(FONT, 9), anchor="w",
                                      wraplength=int(230 * self.scale), justify="left")
        self.record_status.pack(side="left", padx=self._p(8), fill="x", expand=True)
        self.progress = tk.Canvas(body, height=self._p(6), bg=BORDER, highlightthickness=0)
        self.progress.grid(row=5, column=0, sticky="we", pady=(self._p(6), 0))
        self.progress.grid_remove()

        self.hold_ms = current.hold_ms if current else self.action.default_hold_ms

        btns = tk.Frame(body, bg=BG)
        btns.grid(row=8, column=0, columnspan=2, sticky="we", pady=(self._p(18), 0))
        ttk.Button(btns, text="Save", style="Accent.TButton", command=self._save).pack(side="right")
        ttk.Button(btns, text="Cancel", style="Quiet.TButton", command=self.destroy).pack(side="right", padx=(0, 8))
        if current:
            ttk.Button(btns, text="Clear mapping", style="Danger.TButton", command=self._clear).pack(side="left")
        body.columnconfigure(0, weight=1)

    # preset handling
    def _select_preset_for(self, gesture):
        for name, g in PRESET_GESTURES:
            if g == gesture:
                self.preset_box.set(name)
                return
        self.preset_box.set(CUSTOM)

    def _on_preset(self, _):
        name = self.preset_box.get()
        if name == CUSTOM:
            return
        self.gesture = dict(PRESET_GESTURES)[name]
        self.image_path = None
        self._recorded_frame = None
        self._show_preview()

    def _show_preview(self):
        self._photo = ImageTk.PhotoImage(render(self.gesture, PREVIEW))
        self.preview.configure(image=self._photo, width=PREVIEW, height=PREVIEW)
        self.desc.configure(text=self.gesture.describe())

    # recording
    def _toggle_record(self):
        if self._recording:
            self._stop_record("Cancelled")
        else:
            self._start_record()

    def _start_record(self):
        import time
        self._recording = True
        self._samples = []
        self._frames = {}            # pose -> (raw_frame, bbox), one per pose seen
        self._t0 = time.monotonic()
        self.c.suspend(True)         # gestures must not click things while we record
        self.record_btn.configure(text="Stop")
        self.progress.grid()
        self._draw_progress(0)
        self.protocol("WM_DELETE_WINDOW", lambda: (self._stop_record(""), self.destroy()))
        self._record_tick()

    def _stop_record(self, message):
        self._recording = False
        self.c.suspend(False)
        self.record_btn.configure(text="Record my own pose")
        self.record_status.configure(text=message, fg=MUTED)
        self._draw_progress(0)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._show_preview()

    def _record_tick(self):
        if not self._recording or not self.winfo_exists():
            return
        import time
        elapsed = time.monotonic() - self._t0
        s = self.c.worker.snapshot()
        f = s["features"]
        pose = (f.fingers_str, f.pinch, f.orientation) if f is not None else None
        self._show_live(pose)

        if elapsed < COUNTDOWN_S:
            n = int(COUNTDOWN_S - elapsed) + 1
            self.record_status.configure(text=f"Get your hand ready...  {n}", fg=MUTED)
            self._draw_progress(0)
        else:
            rec = elapsed - COUNTDOWN_S
            if pose is not None:
                self._samples.append(pose)
                self._frames.setdefault(pose, (s["raw_frame"], f.bbox))
            left = max(0.0, RECORD_S - rec)
            self.record_status.configure(
                text=f"Recording, hold the pose...  {left:.0f}s" if pose else "Recording, no hand in view",
                fg=PURPLE_DARK if pose else RED)
            self._draw_progress(min(1.0, rec / RECORD_S))
            if rec >= RECORD_S:
                self._finish_record()
                return
        self.after(SAMPLE_MS, self._record_tick)

    def _finish_record(self):
        from collections import Counter
        if len(self._samples) < RECORD_S * 1000 / SAMPLE_MS * 0.4:
            self._stop_record("Not enough of your hand in view. Try again.")
            self.record_status.configure(fg=RED)
            return
        pose, count = Counter(self._samples).most_common(1)[0]
        fingers, pinch, orientation = pose
        if pinch:
            fingers = "*****"
        self.gesture = Gesture(fingers, pinch, orientation if orientation != "up" else "any")
        self._recorded_frame, self._recorded_bbox = self._frames[pose]
        self.image_path = self.c.snapshot_path(self.action.id)
        self._select_preset_for(self.gesture)
        steady = count / len(self._samples)
        msg = "Captured. Press Save to keep it."
        if steady < 0.6:
            msg = "Captured, but the pose kept changing. Record again if this is not what you meant."
        self._stop_record(msg)
        self.record_status.configure(fg=PURPLE_DARK if steady >= 0.6 else RED)

    def _show_live(self, pose):
        if pose is None:
            self.desc.configure(text="Waiting for a hand...")
            return
        fingers, pinch, orientation = pose
        g = Gesture("*****" if pinch else fingers, pinch, orientation if orientation != "up" else "any")
        self._photo = ImageTk.PhotoImage(render(g, PREVIEW))
        self.preview.configure(image=self._photo)
        self.desc.configure(text="Seeing: " + g.describe())

    def _draw_progress(self, frac):
        self.progress.delete("all")
        w = self.progress.winfo_width() or int(260 * self.scale)
        self.progress.create_rectangle(0, 0, w * frac, self._p(6), fill=PURPLE, outline=PURPLE)

    # save / clear
    def _save(self):
        hold = self.hold_ms
        conflict = self.c.profile.conflict(self.gesture, self.action.id)
        replace = False
        if conflict:
            other = ACTIONS[conflict.action].label
            if not messagebox.askyesno(
                    "Gesture already used",
                    f"'{self.gesture.describe()}' already triggers {other}.\n\n"
                    f"Use it for {self.action.label} instead and leave {other} unassigned?",
                    parent=self):
                return
            replace = True
        image = None
        if self._recorded_frame is not None and self.image_path:
            save_snapshot(self._recorded_frame, self._recorded_bbox, ROOT / self.image_path)
            image = self.image_path
        elif self.image_path and (ROOT / self.image_path).exists():
            image = self.image_path
        self.c.set_mapping(Mapping(self.action.id, self.gesture, hold, image), replace_conflict=replace)
        self.destroy()

    def _clear(self):
        self.c.clear_mapping(self.action.id)
        self.destroy()
