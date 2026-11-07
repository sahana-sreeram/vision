"""Thin always-visible pane on the right: switches and the gesture legend."""

import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw, ImageTk

from ..actions import ACTIONS, TOGGLE_ACTION
from ..controller import HOTKEY_LABEL
from ..icons import icon_for, tray_image
from .editor import MappingEditor

PANE_WIDTH = 250   # logical pixels; scaled for the display's DPI at runtime
ICON = 52
POLL_MS = 100

# Same purples as the dictation microphone bubble.
BG = "#F4EEFB"
CARD = "#ffffff"
BORDER = "#E2D5F3"
TILE = "#EFE6FA"
TEXT = "#2A1454"
MUTED = "#6B5B8A"
FAINT = "#A596C2"
PURPLE = "#4C1D95"
PURPLE_SOFT = "#E7D8F6"
VIOLET = "#A78BFA"
VIOLET_SOFT = "#F5F0FD"
RED = "#d9534f"
GREY = "#D4CBE3"
SCROLL = "#C4B5E0"

FONT = "Segoe UI"


class Pane(tk.Tk):
    def __init__(self, controller):
        super().__init__()
        self.c = controller
        self.title("Gestures")
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.scale = max(1.0, self.winfo_fpixels("1i") / 96)
        self.width = int(PANE_WIDTH * self.scale)
        self.icon = int(ICON * self.scale)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{self.width}x{sh - int(90 * self.scale)}+{sw - self.width - int(10 * self.scale)}+0")
        self.minsize(self.width, 120)
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self._app_icon = ImageTk.PhotoImage(tray_image(64))
        self.iconphoto(True, self._app_icon)

        self._rows: dict[str, dict] = {}
        self._photos: list = []
        self._show_unassigned = False   # unassigned actions stay tucked away until asked for
        self._actions_visible = bool(controller.store.get_setting("actions_visible", True))
        self._style()
        self._build()
        self.refresh_rows()
        self._sync_switches()
        self.c.dispatch = lambda fn: self.after(0, fn)
        self.c.listen(self._on_event)
        self.after(POLL_MS, self._poll)

    # look
    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Pane.Vertical.TScrollbar", background=SCROLL, troughcolor=BG, bordercolor=BG,
                     lightcolor=SCROLL, darkcolor=SCROLL, arrowcolor=MUTED, relief="flat",
                     width=int(10 * self.scale))
        st.map("Pane.Vertical.TScrollbar", background=[("active", MUTED)])
        st.configure("Link.TButton", background=BG, foreground=MUTED, bordercolor=BG, focuscolor=BG,
                     relief="flat", padding=(4, 2), font=(FONT, 8))
        st.map("Link.TButton", foreground=[("active", PURPLE)], background=[("active", BG)])

    def _pad(self, n):
        return int(n * self.scale)

    def _card(self, parent, **kw):
        outer = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        inner = tk.Frame(outer, bg=CARD, **kw)
        inner.pack(fill="both", expand=True)
        outer.inner = inner
        return outer

    def _section(self, parent, text):
        tk.Label(parent, text=text.upper(), bg=BG, fg=FAINT, anchor="w",
                 font=(FONT, 8, "bold")).pack(fill="x", padx=self._pad(14), pady=(self._pad(12), self._pad(4)))

    def _switch_row(self, parent, title, command, first=False):
        """One line of the switches card: title on the left, switch on the right."""
        if not first:
            tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=self._pad(12))
        row = tk.Frame(parent, bg=CARD, padx=self._pad(12), pady=self._pad(7), cursor="hand2")
        row.pack(fill="x")
        label = tk.Label(row, text=title, bg=CARD, fg=TEXT, anchor="w", font=(FONT, 10))
        label.pack(side="left", fill="x", expand=True)
        switch = ToggleSwitch(row, self.scale, command=command, bg=CARD, small=True)
        switch.pack(side="right")
        for w in (row, label):
            w.bind("<Button-1>", lambda e: command())
        return switch

    # layout
    def _build(self):
        # switches card
        card = self._card(self)
        card.pack(fill="x", padx=self._pad(12), pady=(self._pad(12), 0))
        self.switch_card = card
        self.sw_gestures = self._switch_row(card.inner, "Gesture control", self.c.toggle_paused, first=True)
        self.sw_camera = self._switch_row(card.inner, "Camera view", self.c.toggle_camera)
        self.sw_magnifier = self._switch_row(card.inner, "Magnifier", self.c.toggle_magnifier)

        # bottom strip of the card: a chevron handle that folds the action list away
        tk.Frame(card.inner, bg=BORDER, height=1).pack(fill="x", padx=self._pad(12))
        self.actions_chevron = ChevronBar(card.inner, self.scale, command=self._toggle_actions)
        self.actions_chevron.pack(fill="x")

        self.status = tk.Label(self, text="", bg=BG, fg=RED, font=(FONT, 8), anchor="w",
                               wraplength=self.width - self._pad(28), justify="left")
        self.status.pack(fill="x", padx=self._pad(14))

        # footer (packed before the list so the list takes the remaining space)
        self.foot = tk.Frame(self, bg=BG)
        self.foot.pack(side="bottom", fill="x", padx=self._pad(10), pady=(self._pad(4), self._pad(8)))
        ttk.Button(self.foot, text="Reset to defaults", style="Link.TButton",
                   command=self._reset_defaults).pack(side="right")

        body = tk.Frame(self, bg=BG)
        self.body = body
        body.pack(fill="both", expand=True, pady=(self._pad(8), 0))
        self.bar = ttk.Scrollbar(body, orient="vertical", style="Pane.Vertical.TScrollbar")
        self.bar.pack(side="right", fill="y", padx=(0, self._pad(4)))
        self.canvas = tk.Canvas(body, bg=BG, highlightthickness=0, yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.configure(command=self.canvas.yview)
        self.rows_frame = tk.Frame(self.canvas, bg=BG)
        self._rows_window = self.canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        self.rows_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._rows_window, width=e.width))
        self.bind_all("<MouseWheel>", self._on_wheel)
        self._apply_actions_visibility()

    def _toggle_actions(self):
        self._actions_visible = not self._actions_visible
        self.c.store.set_setting("actions_visible", self._actions_visible)
        self._apply_actions_visibility()

    def _apply_actions_visibility(self):
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = sw - self.width - int(10 * self.scale)
        if self._actions_visible:
            self.actions_chevron.set_up(True)     # points up: click to hide the list
            self.body.pack(fill="both", expand=True, after=self.switch_card, pady=(self._pad(8), 0))
            self.foot.pack(side="bottom", fill="x", padx=self._pad(10), pady=(self._pad(4), self._pad(8)))
            self.geometry(f"{self.width}x{sh - int(90 * self.scale)}+{x}+0")
        else:
            self.actions_chevron.set_up(False)    # points down: click to show the list
            self.body.pack_forget()
            self.foot.pack_forget()
            self.update_idletasks()
            self.geometry(f"{self.width}x{self.winfo_reqheight()}+{x}+0")

    def _on_wheel(self, e):
        self.canvas.yview_scroll(-int(e.delta / 120), "units")

    def refresh_rows(self):
        for w in self.rows_frame.winfo_children():
            w.destroy()
        self._rows.clear()
        self._photos.clear()
        profile = self.c.profile
        assigned = [a for a in ACTIONS.values() if profile.mapping_for(a.id)]
        unassigned = [a for a in ACTIONS.values() if not profile.mapping_for(a.id)]
        wrap = self.width - self.icon - self._pad(76)
        actions = list(assigned)
        if unassigned:
            if self._show_unassigned:
                actions += unassigned
            n = len(unassigned)
            label = (f"Hide unassigned actions" if self._show_unassigned
                     else f"{n} unassigned action{'s' if n != 1 else ''}  ·  show")
        for action in actions:
            mapping = profile.mapping_for(action.id)
            card = self._card(self.rows_frame, cursor="hand2")
            card.pack(fill="x", padx=(self._pad(12), self._pad(4)), pady=(0, self._pad(6)))
            inner = card.inner
            accent = tk.Frame(inner, bg=CARD, width=self._pad(4))
            accent.pack(side="left", fill="y")
            content = tk.Frame(inner, bg=CARD, padx=self._pad(8), pady=self._pad(7))
            content.pack(side="left", fill="both", expand=True)

            img = icon_for(mapping, self.icon) if mapping else _blank(self.icon)
            photo = ImageTk.PhotoImage(_tile(img, self.icon, self._pad(6)))
            self._photos.append(photo)
            icon = tk.Label(content, image=photo, bg=CARD)
            icon.pack(side="left")

            text = tk.Frame(content, bg=CARD)
            text.pack(side="left", fill="x", expand=True, padx=(self._pad(10), 0))
            tk.Label(text, text=action.label, bg=CARD, fg=TEXT if mapping else MUTED, anchor="w",
                     justify="left", wraplength=wrap, font=(FONT, 10, "bold")).pack(fill="x")
            desc = mapping.gesture.describe() if mapping else "Not assigned"
            if mapping and mapping.hold_ms >= 300:
                desc += f"  ·  hold {mapping.hold_ms / 1000:g}s"
            tk.Label(text, text=desc, bg=CARD, fg=TEXT if mapping else FAINT, anchor="w", justify="left",
                     wraplength=wrap, font=(FONT, 9)).pack(fill="x")
            if action.hint:
                tk.Label(text, text=action.hint, bg=CARD, fg=MUTED, anchor="w", justify="left",
                         wraplength=wrap, font=(FONT, 8)).pack(fill="x")
            for w in (inner, accent, content, icon, text, *text.winfo_children()):
                w.bind("<Button-1>", lambda e, a=action.id: self._edit(a))
            self._rows[action.id] = {"inner": inner, "accent": accent, "state": None}
        if unassigned:
            ttk.Button(self.rows_frame, text=label, style="Link.TButton",
                       command=self._toggle_unassigned).pack(anchor="w", padx=self._pad(10), pady=(0, self._pad(6)))
        self.canvas.yview_moveto(0)
        self._sync_switches()

    def _toggle_unassigned(self):
        self._show_unassigned = not self._show_unassigned
        self.refresh_rows()

    # events from controller
    def _on_event(self, event):
        if event == "profile":
            self.refresh_rows()
        else:
            self._sync_switches()

    def _sync_switches(self):
        self.sw_gestures.set(not self.c.paused)
        self.sw_camera.set(self.c.camera_visible)
        self.sw_magnifier.set(self.c.magnifier_on)

    # user actions
    def _edit(self, action_id):
        # one editor at a time; clicking another row switches it to that action
        old = getattr(self, "_editor", None)
        if old is not None and old.winfo_exists():
            if old._recording:
                old._stop_record("")
            old.destroy()
        self._editor = MappingEditor(self, self.c, action_id)

    def _reset_defaults(self):
        if messagebox.askyesno("Reset", "Put all gestures back to their defaults?", parent=self):
            self.c.reset_defaults()

    # live highlight
    def _poll(self):
        s = self.c.worker.snapshot()
        active = s["active"].action if s["active"] else None
        candidate = s["candidate"].action if s["candidate"] else None
        for action_id, row in self._rows.items():
            state = "active" if action_id == active else "candidate" if action_id == candidate else None
            if state != row["state"]:
                row["state"] = state
                bg = PURPLE_SOFT if state == "active" else VIOLET_SOFT if state == "candidate" else CARD
                accent = PURPLE if state == "active" else VIOLET if state == "candidate" else CARD
                _set_bg(row["inner"], bg)
                row["accent"].configure(bg=accent)
        if s["error"]:
            self.status.configure(text=s["error"])
        else:
            self.status.configure(text="")
        self.after(POLL_MS, self._poll)


class ChevronBar(tk.Canvas):
    """Full-width handle with a line-drawn chevron; click to fold/unfold."""

    def __init__(self, master, scale=1.0, command=None, **kw):
        self.h = int(26 * scale)
        self.scale = scale
        super().__init__(master, height=self.h, bg=CARD, highlightthickness=0, bd=0, cursor="hand2", **kw)
        self.command = command
        self.up = True
        self.hover = False
        self.bind("<Button-1>", lambda e: self.command and self.command())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Configure>", lambda e: self._draw())

    def set_up(self, up: bool):
        self.up = up
        self._draw()

    def _set_hover(self, on):
        self.hover = on
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width() or 200
        self.configure(bg=TILE if self.hover else CARD)
        cx, cy = w / 2, self.h / 2
        half, rise = 7 * self.scale, 3.5 * self.scale
        color = TEXT if self.hover else FAINT
        if self.up:
            pts = (cx - half, cy + rise, cx, cy - rise, cx + half, cy + rise)
        else:
            pts = (cx - half, cy - rise, cx, cy + rise, cx + half, cy - rise)
        self.create_line(*pts, fill=color, width=max(2, int(2 * self.scale)), capstyle="round", joinstyle="round")


class ToggleSwitch(tk.Canvas):
    """Pill-shaped on/off switch with a sliding knob."""

    def __init__(self, master, scale=1.0, command=None, small=False, **kw):
        base_w, base_h = (38, 22) if small else (46, 26)
        self.w, self.h = int(base_w * scale), int(base_h * scale)
        super().__init__(master, width=self.w, height=self.h, highlightthickness=0, bd=0,
                         cursor="hand2", **kw)
        self.command = command
        self.on = True
        self.bind("<Button-1>", lambda e: self.command and self.command())
        self._draw()

    def set(self, on: bool):
        if on != self.on:
            self.on = on
            self._draw()

    def _draw(self):
        self.delete("all")
        w, h, r = self.w, self.h, self.h / 2
        color = PURPLE if self.on else GREY
        self.create_oval(0, 0, h, h, fill=color, outline=color)
        self.create_oval(w - h, 0, w, h, fill=color, outline=color)
        self.create_rectangle(r, 0, w - r, h, fill=color, outline=color)
        pad = max(2, int(2 * self.h / 26))
        x = w - h + pad if self.on else pad
        self.create_oval(x, pad, x + h - 2 * pad, h - pad, fill="white", outline=BORDER)


def _set_bg(widget, bg):
    try:
        widget.configure(bg=bg)
    except tk.TclError:
        return
    for child in widget.winfo_children():
        if isinstance(child, tk.Canvas):
            continue
        _set_bg(child, bg)


def _tile(img: Image.Image, size: int, radius: int) -> Image.Image:
    """Icon on a soft rounded tile."""
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=TILE)
    inset = int(size * 0.08)
    icon = img.resize((size - 2 * inset, size - 2 * inset), Image.LANCZOS)
    tile.alpha_composite(icon, (inset, inset))
    return tile


def _blank(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = size * 0.18
    d.ellipse([size / 2 - r, size / 2 - r, size / 2 + r, size / 2 + r], outline=FAINT, width=2)
    return img
