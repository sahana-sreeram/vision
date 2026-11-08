"""Dictation prompts: a microphone bubble at the text cursor, then a status bar.

The bubble opens only after a click that places the text cursor, with its tail
on that cursor, and goes away if you start typing. Clicking the bubble starts
dictation. The bar uses the same purple speech-bubble colors and is the way
to stop. Targets are at least 44 pt tall, text is high contrast, and nothing
flashes. The app you were typing in gets keyboard focus back, so dictated
text lands there and not in these windows.
"""

import math
import os
import sys
import time
import tkinter as tk
from tkinter import font as tkfont

from ..caret import (FIELD_WITHOUT_POSITION, document_frame, field_frame, focused_text_field,
                     is_text_control, same_element, text_length)

TICK_MS = 100
WATCH_MS = 250            # how often to check which text field has focus
CLICK_POLL_MS = 25        # how often to check for a new click; reading the counter is cheap
FAST_WATCH_MS = 40        # re-check this often after a click until the bubble is up
CLICK_GRACE = 1.5         # Google Docs moves focus a moment after the click
ARM_DELAY = 0.4           # the second click of a double-click must not start dictation
POINTER_HALF = 10         # half the height of the I-beam pointer, so the card clears it
NEAR_POINTER = 64         # caret this close to the click is the one that click just placed
TOP_MARGIN = 46           # clears the menu bar

# Windows has no transparent colour; a key colour is made see-through with -transparentcolor.
CLEAR = "systemTransparent" if sys.platform == "darwin" else "#010203"
PURPLE = "#4C1D95"          # dark purple: bubble stroke, microphone, label, Stop
PURPLE_HOVER = "#3B1578"
FILL = "#F4EEFB"            # pale purple, the light interior of the reference icon
FILL_HOVER = "#E7D8F6"
BUBBLE_R = 40               # circle radius; the whole bubble is the click target
HAND_CURSOR = "pointinghand" if sys.platform == "darwin" else "hand2"
BUBBLE_STROKE = 3
BAR_H = 68
BAR_STROKE = 3

STATUS_TEXT = {"loading": "Getting ready…", "listening": "Listening", "finishing": "Finishing…"}


def _inside(rect, px, py, pad):
    x, y, w, h = rect
    return x - pad <= px <= x + w + pad and y - pad <= py <= y + h + pad


def _bubble_anchor(caret, px, py, page, frame):
    """Tail position for a click, or None when the click is not in the text.

    `page` is the Google Docs document, whose focused control is a hidden
    field somewhere else. The visible caret is where the user clicked.
    """
    click = (px, py - POINTER_HALF, 2, 2 * POINTER_HALF)
    if _near_click(caret, px, py):
        return caret
    if page is not None and _inside(page, px, py, 8):
        return click
    if frame is not None and _inside(frame, px, py, 12):
        return click
    return None


def _near_click(caret, px, py):
    if not caret or caret == FIELD_WITHOUT_POSITION:
        return False
    cx, cy, _cw, ch = caret
    return abs(cx - px) <= NEAR_POINTER and cy - NEAR_POINTER <= py <= cy + ch + NEAR_POINTER


def _family():
    return tkfont.nametofont("TkDefaultFont").actual("family")


def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    steps = max(8, int(r * 1.5))
    pts = []
    for cx, cy, start in ((x2 - r, y1 + r, -90), (x2 - r, y2 - r, 0),
                          (x1 + r, y2 - r, 90), (x1 + r, y1 + r, 180)):
        for i in range(steps + 1):
            a = math.radians(start + i * 90 / steps)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
    kw.setdefault("width", 0)
    return canvas.create_polygon(pts, **kw)


class PillButton:
    def __init__(self, canvas, x, y, w, h, text, font, bg, hover, fg, command):
        self.canvas = canvas
        tag = f"button{id(self)}"
        self.shape = round_rect(canvas, x, y, x + w, y + h, h / 2, fill=bg, tags=(tag,))
        canvas.create_text(x + w / 2, y + h / 2, text=text, fill=fg, font=font, tags=(tag,))
        canvas.tag_bind(tag, "<Enter>", lambda e: self._hover(hover, HAND_CURSOR))
        canvas.tag_bind(tag, "<Leave>", lambda e: self._hover(bg, ""))
        canvas.tag_bind(tag, "<ButtonRelease-1>", lambda e: command())

    def _hover(self, color, cursor):
        self.canvas.itemconfigure(self.shape, fill=color)
        self.canvas.configure(cursor=cursor)


class Floating(tk.Toplevel):
    """Borderless, rounded, shadowed window that floats above every app."""

    def __init__(self, master, title, width, height):
        super().__init__(master, bg=CLEAR)
        self.title(title)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        if sys.platform == "darwin":
            self.attributes("-transparent", True)
        elif sys.platform == "win32":
            self.attributes("-transparentcolor", CLEAR)
        # wm geometry rejects decimals ("120x122.08+…"), which left this window at its
        # default spot instead of beside the caret.
        self.width, self.height = int(math.ceil(width)), int(math.ceil(height))
        self.canvas = tk.Canvas(self, width=self.width, height=self.height, bg=CLEAR,
                                highlightthickness=0, bd=0)
        self.canvas.pack()

    def show_at(self, x, y):
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = int(min(max(8, x), sw - self.width - 8))
        y = int(min(max(TOP_MARGIN - 16, y), sh - self.height - 8))
        self.geometry(f"{int(self.width)}x{int(self.height)}+{x}+{y}")
        _float_above_apps(self)
        _never_take_focus(self)
        return x, y


def _bubble_layout(flip_x=False, flip_y=False):
    """Speech bubble matching the reference: round body, tail toward the cursor.

    The unflipped tail sits at the lower left, so the bubble rests above and
    to the right of the text cursor. Flips keep that tail on the cursor when
    the bubble would otherwise leave the screen.
    """
    r = BUBBLE_R
    pad = BUBBLE_STROKE + 4
    tdx, tdy = -0.92 * r, 1.04 * r
    if flip_x:
        tdx = -tdx
    if flip_y:
        tdy = -tdy
    minx, maxx = min(-r, tdx), max(r, tdx)
    miny, maxy = min(-r, tdy), max(r, tdy)
    return {
        "w": maxx - minx + pad * 2,
        "h": maxy - miny + pad * 2,
        "cx": pad - minx,
        "cy": pad - miny,
        "r": r,
        "tip": (pad - minx + tdx, pad - miny + tdy),
        "stroke": BUBBLE_STROKE,
    }


def _stadium(canvas, x1, y1, x2, y2, color, tags):
    """Filled capsule. Ovals keep the ends smooth at this size."""
    w, h = x2 - x1, y2 - y1
    if w >= h:
        canvas.create_oval(x1, y1, x1 + h, y2, fill=color, width=0, tags=tags)
        canvas.create_oval(x2 - h, y1, x2, y2, fill=color, width=0, tags=tags)
        if w > h + 0.5:
            canvas.create_rectangle(x1 + h / 2, y1, x2 - h / 2, y2, fill=color, width=0, tags=tags)
    else:
        canvas.create_oval(x1, y1, x2, y1 + w, fill=color, width=0, tags=tags)
        canvas.create_oval(x1, y2 - w, x2, y2, fill=color, width=0, tags=tags)
        if h > w + 0.5:
            canvas.create_rectangle(x1, y1 + w / 2, x2, y2 - w / 2, fill=color, width=0, tags=tags)


def paint_mic(canvas, cx, cy, h, color, tags=()):
    """Microphone from the reference icon, centered on (cx, cy)."""
    top = cy - h * 0.47
    cap_w, cap_h = h * 0.34, h * 0.50
    _stadium(canvas, cx - cap_w / 2, top, cx + cap_w / 2, top + cap_h, color, tags)
    thick = max(2.8, h * 0.072)
    # Cradle opens upward around the lower half of the capsule.
    arm_y = top + cap_h * 0.62
    radius = cap_w / 2 + thick + h * 0.03
    south = arm_y + radius
    canvas.create_arc(cx - radius, arm_y - radius, cx + radius, south,
                      start=180, extent=180, style="arc", outline=color, width=thick, tags=tags)
    stem_w = max(3.0, thick * 0.62)
    stem_top = south + thick * 0.15
    stem_h = max(3.5, h * 0.09)
    canvas.create_rectangle(cx - stem_w / 2, stem_top, cx + stem_w / 2, stem_top + stem_h,
                            fill=color, width=0, tags=tags)
    base_w, base_h = cap_w * 0.92, max(4.0, h * 0.075)
    _stadium(canvas, cx - base_w / 2, stem_top + stem_h - 1,
             cx + base_w / 2, stem_top + stem_h - 1 + base_h, color, tags)


def _tail_points(cx, cy, r, tipx, tipy):
    """Base of the tail, just inside the circle, plus the tip. Overlaps the disk."""
    dx, dy = tipx - cx, tipy - cy
    dist = math.hypot(dx, dy) or 1
    if dist <= r + 1:
        dist = r + 12
        dx, dy = dx / (math.hypot(dx, dy) or 1) * dist, dy / (math.hypot(dx, dy) or 1) * dist
        tipx, tipy = cx + dx, cy + dy
    theta = math.atan2(dy, dx)
    beta = math.acos(min(1.0, r / dist))
    pts = []
    for i in range(4):
        a = theta + beta - 2 * beta * i / 3
        pts.extend((cx + r * 0.96 * math.cos(a), cy + r * 0.96 * math.sin(a)))
    pts.extend((tipx, tipy))
    return pts


def paint_bubble(canvas, x, y, flip_x, flip_y):
    """Draw the bubble with its top-left at (x, y). Returns the layout."""
    lay = _bubble_layout(flip_x, flip_y)
    cx, cy = lay["cx"] + x, lay["cy"] + y
    tipx, tipy = lay["tip"][0] + x, lay["tip"][1] + y
    r, stroke = lay["r"], lay["stroke"]
    dist = math.hypot(tipx - cx, tipy - cy) or 1
    canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=PURPLE, width=0, tags=("hit",))
    # The tail points at the caret, right under the pointer, so it is not part of the button.
    canvas.create_polygon(_tail_points(cx, cy, r, tipx, tipy), fill=PURPLE, width=0)
    inner = r - stroke
    inset = stroke * dist / r
    in_tip = (tipx + (cx - tipx) / dist * inset, tipy + (cy - tipy) / dist * inset)
    canvas.create_oval(cx - inner, cy - inner, cx + inner, cy + inner,
                       fill=FILL, width=0, tags=("hit", "bubble-fill"))
    canvas.create_polygon(_tail_points(cx, cy, inner, *in_tip), fill=FILL, width=0,
                          tags=("bubble-fill",))
    paint_mic(canvas, cx, cy - r * 0.02, r * 1.15, PURPLE, tags=("hit",))
    return lay


class MicBubble(Floating):
    """The speech-bubble microphone. Clicking it starts dictation."""

    def __init__(self, master, anchor, on_start):
        sw, sh = master.winfo_screenwidth(), master.winfo_screenheight()
        ax, ay, aw, ah = anchor
        aim_x, aim_y = ax + aw / 2, ay + ah / 2
        layout, origin = _place_bubble(aim_x, aim_y, sw, sh)
        super().__init__(master, "Start speech-to-text", layout["w"], layout["h"])
        self._layout = paint_bubble(self.canvas, 0, 0, layout["flip_x"], layout["flip_y"])
        self._on_start = on_start
        self.canvas.magnet = True      # the gesture cursor snaps onto the bubble
        self._armed_at = time.monotonic() + ARM_DELAY
        self.canvas.tag_bind("hit", "<Enter>", lambda e: self._hover(True))
        self.canvas.tag_bind("hit", "<Leave>", lambda e: self._hover(False))
        # Mouse-down, not release: the first click on a borderless window is
        # otherwise swallowed as the window activates.
        self.canvas.tag_bind("hit", "<Button-1>", self._press)
        self._origin = self.show_at(*origin)

    def armed(self):
        return time.monotonic() >= self._armed_at

    def contains(self, x, y):
        """True when screen point (x, y) is on the round body, not the tail or corners."""
        ox, oy = self._origin
        lay = self._layout
        return math.hypot(x - ox - lay["cx"], y - oy - lay["cy"]) <= lay["r"]

    def _press(self, event):
        lay = self._layout
        if self.armed() and math.hypot(event.x - lay["cx"], event.y - lay["cy"]) <= lay["r"]:
            self._on_start()

    def _hover(self, on):
        self.canvas.itemconfigure("bubble-fill", fill=FILL_HOVER if on else FILL)
        self.canvas.configure(cursor=HAND_CURSOR if on else "")


def _place_bubble(aim_x, aim_y, sw, sh):
    """Put the tail tip on (aim_x, aim_y), flipping so the bubble stays on screen."""
    best = None
    for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
        lay = _bubble_layout(flip_x, flip_y)
        lay["flip_x"], lay["flip_y"] = flip_x, flip_y
        x, y = aim_x - lay["tip"][0], aim_y - lay["tip"][1]
        cx = min(max(8, x), sw - lay["w"] - 8)
        cy = min(max(TOP_MARGIN - 16, y), sh - lay["h"] - 8)
        # Unclamped placements win. Otherwise keep the tail as close to the cursor as possible.
        err = abs(cx - x) + abs(cy - y)
        rank = (err > 0.5, err, flip_x or flip_y)
        if best is None or rank < best[0]:
            best = (rank, lay, (cx, cy))
    return best[1], best[2]


class StatusPill(Floating):
    """Listening bar in the same pale-purple, dark-purple speech-bubble style."""

    def __init__(self, master, on_stop):
        f = _family()
        title = tkfont.Font(family=f, size=16, weight="bold")
        text_w = max(title.measure(t) for t in STATUS_TEXT.values())
        self.stop_w = 92
        mic_h = 32
        width = int(16 + mic_h * 0.55 + 12 + text_w + 16 + self.stop_w + 14)
        super().__init__(master, "Speech-to-text status", width, BAR_H)
        c, h = self.canvas, BAR_H
        stroke = BAR_STROKE
        _stadium(c, stroke, stroke, width - stroke, h - stroke, PURPLE, ())
        _stadium(c, stroke * 2, stroke * 2, width - stroke * 2, h - stroke * 2, FILL, ())
        paint_mic(c, 16 + mic_h * 0.28, h / 2, mic_h, PURPLE)
        self.label = c.create_text(16 + mic_h * 0.55 + 12, h / 2, anchor="w", fill=PURPLE, font=title)
        self.stop = PillButton(c, width - 12 - self.stop_w, (h - 44) / 2, self.stop_w, 44, "Stop",
                               (f, 15, "bold"), PURPLE, PURPLE_HOVER, "#ffffff", on_stop)
        self.stop_tag = c.gettags(self.stop.shape)[0]
        self._state = None
        self.show_at((self.winfo_screenwidth() - width) // 2, TOP_MARGIN)

    def set_state(self, state):
        if state == self._state:
            return
        self._state = state
        self.canvas.itemconfigure(self.label, text=STATUS_TEXT[state])
        self.canvas.itemconfigure(self.stop_tag, state="hidden" if state == "finishing" else "normal")


class DictationUI:
    def __init__(self, root, controller):
        self.root = root
        self.c = controller
        self.card = None
        self.pill = None
        self._target_app = None
        self._card_field = None    # text field the open card belongs to
        self._card_chars = None    # its length when the card opened; typing closes the card
        self._watching = False     # becomes True after the first watch, so startup does not prompt
        self._clicks = _click_count()
        self._click_at = None      # time of a click not yet matched to a text field
        self._click_xy = None      # pointer at that click; the caret is placed there
        self._shown_click = None   # click the open bubble was placed for
        self._ignore_clicks_until = 0.0
        self._watch_job = None
        if sys.platform == "win32":
            _watch_clicks_windows()
        root.after(TICK_MS, self._tick)
        root.after(CLICK_POLL_MS, self._poll_clicks)
        self._schedule_watch(WATCH_MS)

    def _schedule_watch(self, ms):
        if self._watch_job is not None:
            self.root.after_cancel(self._watch_job)
        self._watch_job = self.root.after(ms, self._watch)

    def _poll_clicks(self):
        """Check right away after a click, so the bubble appears with the caret."""
        try:
            if _click_count() != self._clicks:
                self._schedule_watch(0)
        finally:
            self.root.after(CLICK_POLL_MS, self._poll_clicks)

    def _click_pending(self):
        """A recent click that has not got its bubble yet; focus may still be moving."""
        return (self._click_at is not None and time.monotonic() - self._click_at <= CLICK_GRACE
                and (self.card is None or self._click_xy != self._shown_click))

    def _open_card(self, element, anchor):
        self._card_field = element
        self._card_chars = text_length(element)
        self._shown_click = self._click_xy
        self.card = MicBubble(self.root, anchor, self._start)
        # Showing a window activates this app; give the keyboard back right away.
        self._return_focus()

    def _watch(self):
        """Offer dictation only after a click that leaves the caret in a text
        field. A focus change with no click does not show it. Typing or leaving
        the field withdraws it."""
        self._watch_job = None
        try:
            now = time.monotonic()
            clicks = _click_count()
            if clicks != self._clicks and now >= self._ignore_clicks_until:
                pointer = self.root.winfo_pointerxy()
                if self.card is not None and self.card.contains(*pointer):
                    # Backup for a press the bubble window did not receive.
                    self._clicks = clicks
                    if self.card.armed():
                        self._start()
                    return
                self._click_at = now
                self._click_xy = pointer
            self._clicks = clicks
            # Focus can land a moment after the click when switching apps.
            clicked = self._click_at is not None and now - self._click_at <= CLICK_GRACE
            if self.c.backend.dictation_state != "idle" or _we_are_frontmost():
                return
            field = focused_text_field(retry=False)
            element = field[0] if field else None
            caret = field[1] if field else None
            anchor = self._anchor(element, caret) if clicked else None
            typed = (is_text_control(element) and self._card_chars is not None
                     and text_length(element) not in (self._card_chars, None))
            moved = clicked and self._click_xy != self._shown_click
            if self.card is not None and typed:
                self._close_card()
            elif self.card is not None and clicked and anchor is None:
                self._close_card()
            elif self.card is not None and anchor is not None and (
                    moved or not same_element(element, self._card_field)):
                self._close_card()
                self._target_app = _frontmost_app()
                self._open_card(element, anchor)
            elif self.card is not None and not same_element(element, self._card_field):
                self._close_card()
            if (self.card is None and not typed and anchor is not None and self._watching
                    and not self.c.paused and element is not None):
                self._target_app = _frontmost_app()
                self._open_card(element, anchor)
            self._watching = True
        finally:
            if self._watch_job is None:
                self._schedule_watch(FAST_WATCH_MS if self._click_pending() else WATCH_MS)

    def _anchor(self, element, caret):
        """Where the tail points, or None if this click did not place the caret.

        The click is where the caret was just put. A caret rect far from that
        click is the field's corner, not the cursor, so the click is used.
        A click outside the text field does not show the bubble.
        """
        if self._click_xy is None or element is None:
            return None
        px, py = self._click_xy
        return _bubble_anchor(caret, px, py, document_frame(element), field_frame(element))

    def _close_card(self):
        if self.card is not None:
            self.card.destroy()
            self.card = None
            self._card_field = None
            self._card_chars = None

    def _own_click(self):
        """Clicks on the card or pill must not count as clicking into the text field."""
        self._click_at = None
        self._ignore_clicks_until = time.monotonic() + 0.6

    def _start(self):
        self._own_click()
        self._close_card()
        if self.c.backend.start_dictate():
            self._show_pill()
        self._return_focus()

    def _stop(self):
        self._own_click()
        self.c.backend.stop_dictate()
        if self.pill is not None:
            self.pill.set_state("finishing")
        self._return_focus()

    def _return_focus(self):
        if self._target_app is not None:
            self.root.after(60, _activate, self._target_app)

    def _show_pill(self):
        if self.pill is None:
            self.pill = StatusPill(self.root, self._stop)
        self.pill.set_state(self.c.backend.dictation_state if self.c.backend.dictation_state != "idle"
                            else "loading")

    def _tick(self):
        try:
            state = self.c.backend.dictation_state
            if state == "idle":
                if self.pill is not None:
                    self.pill.destroy()
                    self.pill = None
            else:
                self._show_pill()
        finally:
            self.root.after(TICK_MS, self._tick)


def _float_above_apps(win):
    """Tk's -topmost does not lift borderless windows above other apps on macOS,
    so the window would fall behind the app that gets focus back."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import (NSApplication, NSStatusWindowLevel,
                            NSWindowCollectionBehaviorCanJoinAllSpaces,
                            NSWindowCollectionBehaviorFullScreenAuxiliary)
        win.update_idletasks()
        title = win.title()
        for ns_window in NSApplication.sharedApplication().windows():
            if ns_window.title() == title:
                ns_window.setLevel_(NSStatusWindowLevel)
                ns_window.setHidesOnDeactivate_(False)
                ns_window.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                                 | NSWindowCollectionBehaviorFullScreenAuxiliary)
                ns_window.setHasShadow_(True)
                ns_window.setIgnoresMouseEvents_(False)
                ns_window.invalidateShadow()
    except Exception:
        pass


def _never_take_focus(win):
    """Windows: keep keyboard focus in the app being dictated into when this window appears."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        win.update_idletasks()
        u = ctypes.windll.user32
        hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
        GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000
        u.SetWindowLongW(hwnd, GWL_EXSTYLE, u.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE)
    except Exception:
        pass


def _frontmost_app():
    """The app that had keyboard focus before the card opened."""
    if sys.platform == "win32":
        from ..caret_win import foreground_window
        hwnd, pid = foreground_window()
        return hwnd if hwnd and pid != os.getpid() else None
    if sys.platform != "darwin":
        return None
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
    except Exception:
        return None
    if app is None or app.processIdentifier() == os.getpid():
        return None
    return app


_win_clicks = 0


def _watch_clicks_windows():
    """Count left-button releases system-wide with a low-level mouse hook (pynput)."""
    from pynput import mouse

    def on_click(x, y, button, pressed):
        global _win_clicks
        if button == mouse.Button.left and not pressed:
            _win_clicks += 1

    mouse.Listener(on_click=on_click, daemon=True).start()


def _click_count():
    """System-wide count of left-button releases, real or from the gesture
    backend. Reading it needs no extra permission."""
    if sys.platform == "win32":
        return _win_clicks
    if sys.platform != "darwin":
        return 0
    try:
        import Quartz
        return Quartz.CGEventSourceCounterForEventType(
            Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGEventLeftMouseUp)
    except Exception:
        return 0


def _we_are_frontmost():
    if sys.platform == "win32":
        from ..caret_win import foreground_window
        return foreground_window()[1] == os.getpid()
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
    except Exception:
        return False
    return app is not None and app.processIdentifier() == os.getpid()


def _activate(app):
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.SetForegroundWindow(app)
        except Exception:
            pass
        return
    try:
        from AppKit import NSApplicationActivateIgnoringOtherApps
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    except Exception:
        pass
