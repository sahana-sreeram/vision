"""Zoom box that follows the pointer. Tk/PIL on Windows and Linux, AppKit on macOS."""

import sys
import tkinter as tk

import pyautogui
from PIL import Image, ImageDraw, ImageGrab, ImageTk

ZOOM = 2.0
BOX_W, BOX_H = 240, 180      # logical pixels
GAP = 24                     # space between the source area and the box
POLL_MS = 40


class Magnifier:
    """Zoom box centred on the cursor showing what is under it.

    On Windows the window is excluded from screen capture and made click-through, so
    it can sit over the magnified area. Elsewhere it is placed beside the area instead.
    """

    def __init__(self, root: tk.Tk, enabled: bool = False):
        self.root = root
        scale = max(1.0, root.winfo_fpixels("1i") / 96)
        self.w, self.h = int(BOX_W * scale), int(BOX_H * scale)
        self.gap = int(GAP * scale)
        self.src_w, self.src_h = int(self.w / ZOOM), int(self.h / ZOOM)
        self.sw, self.sh = root.winfo_screenwidth(), root.winfo_screenheight()

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.withdraw()
        self.label = tk.Label(self.win, bd=0, bg="black", highlightthickness=2, highlightbackground="white")
        self.label.pack()
        self._photo = None
        self.enabled = False
        self._after = None
        self._centered = False   # True once the window is hidden from capture (Windows)
        self._prepared = False
        self.set_enabled(enabled)

    def _prepare_window(self):
        """Windows: hide the box from screen captures and let clicks pass through it."""
        self._prepared = True
        if sys.platform != "win32":
            return
        import ctypes
        u = ctypes.windll.user32
        self.win.update_idletasks()
        hwnd = u.GetParent(self.win.winfo_id()) or self.win.winfo_id()
        WDA_EXCLUDEFROMCAPTURE = 0x11
        GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE, LWA_ALPHA = -20, 0x80000, 0x20, 0x08000000, 0x2
        style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        u.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        u.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
        self._centered = bool(u.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        if enabled:
            self.win.deiconify()
            if not self._prepared:
                self._prepare_window()
            if self._after is None:
                self._tick()
        else:
            if self._after is not None:
                self.root.after_cancel(self._after)
                self._after = None
            self.win.withdraw()

    def close(self):
        self.set_enabled(False)
        self.win.destroy()

    def _tick(self):
        self._after = None
        if not self.enabled:
            return
        try:
            self._update()
        except Exception:
            pass  # capture can fail transiently (locked screen, display change)
        self._after = self.root.after(POLL_MS, self._tick)

    def _update(self):
        x, y = pyautogui.position()
        l, t = int(x - self.src_w / 2), int(y - self.src_h / 2)
        img = ImageGrab.grab(bbox=(l, t, l + self.src_w, t + self.src_h), all_screens=True)
        img = img.resize((self.w, self.h), Image.NEAREST)
        d = ImageDraw.Draw(img)
        cx, cy, arm = self.w // 2, self.h // 2, int(8 * self.w / 240)
        d.line([(cx - arm, cy), (cx + arm, cy)], fill=(255, 220, 0), width=2)
        d.line([(cx, cy - arm), (cx, cy + arm)], fill=(255, 220, 0), width=2)
        self._photo = ImageTk.PhotoImage(img)
        self.label.configure(image=self._photo)

        if self._centered:
            bx, by = x - self.w // 2, y - self.h // 2
        else:
            # beside the source area: right of it when there is room, else left
            bx = x + self.src_w // 2 + self.gap
            if bx + self.w > self.sw:
                bx = x - self.src_w // 2 - self.gap - self.w
            by = min(max(y - self.h // 2, 0), self.sh - self.h)
        self.win.geometry(f"+{int(bx)}+{int(by)}")


class MacMagnifier:
    """Wraps the AppKit magnifier from mac_magnifier.py (centred on the cursor, excluded from capture)."""

    def __init__(self, root: tk.Tk, enabled: bool = False):
        from .mac_magnifier import ScreenMagnifier  # macOS only
        self.root = root
        self._cls = ScreenMagnifier
        self._mag = None
        self._after = None
        self.enabled = False
        self.set_enabled(enabled)

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        if enabled:
            if self._mag is None:
                self._mag = self._cls()
            if self._after is None:
                self._tick()
        else:
            if self._after is not None:
                self.root.after_cancel(self._after)
                self._after = None
            if self._mag is not None:
                self._mag.close()
                self._mag = None

    def close(self):
        self.set_enabled(False)

    def _tick(self):
        self._after = None
        if not self.enabled or self._mag is None:
            return
        try:
            self._mag.update()
        except Exception:
            pass
        self._after = self.root.after(POLL_MS, self._tick)


def create_magnifier(root: tk.Tk, enabled: bool):
    if sys.platform == "darwin":
        try:
            return MacMagnifier(root, enabled)
        except Exception as e:
            print(f"AppKit magnifier unavailable ({e}); using the portable one", file=sys.stderr)
    return Magnifier(root, enabled)
