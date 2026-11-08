"""Per-frame state machine: features -> matched mapping -> hold -> fire/hold/release."""

from cursor_stabilization import OneEuroFilter
from cursor_stabilization.cursor_state import CursorState
from cursor_stabilization.adaptive_smoothing import AdaptiveSmoothing

from .actions import (ACTIONS, BUTTON, CONTINUOUS, KEY_ACTIONS, ONESHOT, SCROLL_STEP,
                      TOGGLE_ACTION, Backend)
from .features import PINCH_FINGERS, HandFeatures
from .profiles import Mapping, Profile

LOST_TIMEOUT = 0.3    # seconds without a hand before held buttons are released
DRAG_DEADZONE_PX = 40  # knuckle travel (screen px) while a button is held before a drag starts
HOVER_BREAK_PX = 32    # knuckle movement needed to leave a locked position
APPROACH_GAP = 0.65    # thumb-index gap (hand sizes) below which a pinch may be starting
APPROACH_DROP = 0.12   # gap must have shrunk by this much within APPROACH_WINDOW to count
APPROACH_WINDOW = 0.45 # seconds; long enough to catch a slow, deliberate pinch
CURL_SPEED = 3.5       # fingertip speed relative to the knuckle (hand sizes / s) that means "clicking"
REWIND_S = 0.15        # when a click starts, put the cursor back where it was this long ago
TRAIL_S = 0.5          # how much cursor history to keep for the rewind
CURL_CONFIRM = 0.5     # a curl is real if, next frame, the tip is still this fraction of the jump away
EASE_S = 0.15          # after a click freeze ends, close the gap to the finger over this long
SNAP_WINDOW = 0.25     # a click only counts if the pinched fingers snapped shut this recently
SNAP_DROP = 0.20       # how far the gap must fall, in hand sizes, to count as a snap
CLICK_ACTIONS = {"left_button", "left_click", "right_click", "double_click"}
HOLD_ACTIONS = {"move_window", "drag"}   # kept through brief detection dropouts
RELEASE_GRACE_S = 0.3                    # how long a hold survives without its gesture
DAMP_S = 0.4           # after a click starts, use heavy smoothing for this long (One Euro is too eager)
HOVER_RADIUS_PX = 18   # target wobble treated as "holding still" (hover lock)
MAGNET_POLL_S = 0.1    # how often to ask what button-like control is under the finger
MAGNET_MARGIN_PX = 20  # the finger must leave a snapped control by this much to let go
HOVER_MS = 350         # how long still before the cursor locks in place (hover lock)


class Engine:
    def __init__(self, backend: Backend, frame_size=(640, 480), margin=100, smooth=8):
        self.backend = backend
        self.frame_w, self.frame_h = frame_size
        self.margin = margin
        self.smooth = smooth
        self.screen_w, self.screen_h = backend.screen_size()
        self.cursor_state = CursorState()
        self.cursor_label, self.cursor_score = 'UNTRAINED', 0.0
        self.filter_x, self.filter_y = OneEuroFilter(), OneEuroFilter()
        # Hybrid smoothing. Pointing uses time-based One Euro (low lag on big moves), with
        # classifier-tuned parameters when a local model exists. The moment a click starts
        # (finger curl / pinch closing) the engine switches to the fixed low-pass
        # (1/smooth per frame) for DAMP_S, because One Euro passes the curl straight
        # through to the cursor. Toggle 'a' to use the fixed low-pass all the time.
        self.adaptive = AdaptiveSmoothing(self.filter_x, self.filter_y, enabled=True)
        self.hover_lock = True      # lock the cursor after holding still briefly (settable)

        self.profile: Profile | None = None
        self._ordered: list[Mapping] = []
        self.paused = False
        self.suspended = False          # True while the editor is recording a pose: nothing fires
        self.toggle_requested = False   # set when the toggle gesture fires; the worker acts on it
        self.candidate: Mapping | None = None
        self.active: Mapping | None = None
        self._candidate_since = 0.0
        self._button_down = False
        self._grabbing = False      # carrying a window (move_window action)
        self._hold_lost_at = None   # when a held gesture stopped matching (release grace)
        self._grab_cursor = None    # cursor screen position when the window was grabbed
        self._last_seen = 0.0
        self._prev_seen = 0.0           # time of the frame before _last_seen
        # cursor stabilisation
        self._lock = None           # cursor held here while hovering or starting a pinch
        self._still_ref = None      # (target, since) used by the hover lock
        self._damp_until = 0.0      # heavy smoothing is used until this time
        self._press_knuckle = None  # knuckle screen position when a button went down
        self._press_cursor = None
        self._drag_started = False
        self._gap_history = []      # recent (time, fingertip gaps); a click needs a snap in here
        self._approach_t = None     # when the gap that is now closing was last fully open
        self._magnet = None         # screen rect of the control the cursor is snapped onto
        self._magnet_polled = -1.0
        self._trail = []            # recent (time, x, y) cursor positions, for the rewind
        self._last_tip = None       # (time, fingertip offset from knuckle) from the previous frame
        self._lock_knuckle = None   # knuckle screen position when the lock was taken
        self._curl_pending = None   # (offset before a fast tip move, jump length, time) awaiting confirmation
        self._click_seen = None     # when the current click motion was first seen (for the rewind)
        self._ease = None           # (start time, dx, dy) gap being closed after a freeze ends
        self._prev_x = self.screen_w / 2
        self._prev_y = self.screen_h / 2

    # configuration
    def set_profile(self, profile: Profile):
        self.release()
        self._clear_lock()
        self.profile = profile
        self._ordered = profile.ordered_mappings()

    def refresh_profile(self):
        if self.profile:
            self.set_profile(self.profile)

    def set_paused(self, paused: bool):
        self.paused = paused
        if paused:
            self.release()
            self._clear_lock()

    def set_suspended(self, suspended: bool):
        self.suspended = suspended
        if suspended:
            self.release()
            self._clear_lock()

    # per frame
    def update(self, features: HandFeatures | None, now: float):
        if self.suspended:
            return
        if features is None:
            self._last_tip = None
            self._curl_pending = None
            self.cursor_state.reset()
            self.cursor_label, self.cursor_score = 'NO HAND', 0.0
            self.adaptive.update('NO HAND', now)
            if now - self._last_seen > LOST_TIMEOUT:
                self._clear_lock()
                if self.candidate:
                    self.release()
            return
        self._prev_seen, self._last_seen = self._last_seen, now
        self._remember_gaps(features, now)

        candidates = self._ordered
        if self.paused:
            candidates = [m for m in candidates if m.action == TOGGLE_ACTION]
        match = next((m for m in candidates if m.gesture.matches(features)), None)
        # Direction hysteresis: the current gesture keeps matching anywhere in its own
        # direction sector, so wobbling near the start threshold doesn't reset its hold.
        if (match is None and self.candidate is not None and self.candidate in candidates
                and self.candidate.gesture.matches(features, margin=0.0)):
            match = self.candidate
        # A pinch that is simply held, or drifted into while hovering, is not a
        # click. Only a snap — the gap slamming shut — may start one. Once the
        # button is down, holding the pinch keeps the drag going.
        if (match and match.action in CLICK_ACTIONS and match.gesture.pinch
                and not self._button_down and match is not self.candidate
                and not self._snapped(match.gesture.pinch)):
            skipped = match.action
            match = next((m for m in candidates
                          if m.action != skipped and m.gesture.matches(features)), None)

        # A grabbed window or drag must not drop because one frame misread the hand:
        # keep it going for a short grace period before letting go.
        if (self.active is not None and self.active.action in HOLD_ACTIONS
                and match is not self.active):
            if self._hold_lost_at is None:
                self._hold_lost_at = now
            if now - self._hold_lost_at < RELEASE_GRACE_S:
                match = self.active
        else:
            self._hold_lost_at = None

        if match is not self.candidate:
            self.release()
            self.candidate = match
            self._candidate_since = now
        if match is None:
            self.cursor_label, self.cursor_score = 'INACTIVE', 0.0
            self.adaptive.update('INACTIVE', now)
            return

        if self.active is None and (now - self._candidate_since) * 1000 >= match.hold_ms:
            self._activate(match)
        if self.active is not None:
            self._run(self.active, features)

    def release(self):
        self.cursor_state.reset()
        self.cursor_label, self.cursor_score = 'INACTIVE', 0.0
        if self._button_down:
            self.backend.mouse_up()
            self._button_down = False
        if self._grabbing:
            self.backend.release_window()
            self._grabbing = False
        self._hold_lost_at = None
        self._press_knuckle = None
        self._drag_started = False
        self.active = None
        self.candidate = None

    # internals
    def _activate(self, m: Mapping):
        self.active = m
        action = ACTIONS[m.action]
        if not action.moves_cursor:
            # A click freeze belongs to pointing/clicking; drop it once another gesture has
            # actually taken over (held past its hold time). One-frame misreads, common at
            # the edges of the camera view, must not cancel it mid-pinch.
            self._clear_lock()
        if action.kind == ONESHOT:
            self._fire(m.action)
        elif action.kind == BUTTON:
            if m.action == "move_window":
                self._grab_cursor = self._cursor_screen()
                self._grabbing = self.backend.grab_window_at(*self._grab_cursor)
            if not self._grabbing:
                self.backend.mouse_down()   # ordinary drag (also the fallback for move_window)
                self._button_down = True
            self._press_knuckle = None
            self._drag_started = False

    def _fire(self, action_id: str):
        b = self.backend
        if action_id == "left_click":
            b.click("left")
        elif action_id == "right_click":
            b.click("right")
        elif action_id == "double_click":
            b.double_click()
        elif action_id in KEY_ACTIONS:
            b.press_key(KEY_ACTIONS[action_id])
        elif action_id == TOGGLE_ACTION:
            self.toggle_requested = True

    def _run(self, m: Mapping, f: HandFeatures):
        action = ACTIONS[m.action]
        if action.moves_cursor:
            # Use the same raw index-tip mapping as the training recorder.
            tx, ty = self._to_screen(tuple(int(v) for v in f.pointer))
            self.cursor_label, self.cursor_score = self.cursor_state.update(
                self._last_seen, 1.0 - tx / self.screen_w, ty / self.screen_h)
            self.adaptive.update(self.cursor_label, self._last_seen)
            if self._button_down or self._grabbing:
                self._drag_cursor(f)
            else:
                self._point_cursor(f, self._last_seen)
        if not action.moves_cursor:
            self.cursor_state.reset()
            self.cursor_label, self.cursor_score = 'INACTIVE', 0.0
            self.adaptive.update('INACTIVE', self._last_seen)
        if action.kind == CONTINUOUS:
            if m.action == "scroll_up":
                self.backend.scroll(SCROLL_STEP)
            elif m.action == "scroll_down":
                self.backend.scroll(-SCROLL_STEP)

    def _to_screen(self, point):
        px, py = point
        tx = _interp(px, self.margin, self.frame_w - self.margin, 0, self.screen_w)
        ty = _interp(py, self.margin, self.frame_h - self.margin, 0, self.screen_h)
        return tx, ty

    def _clicking(self, f: HandFeatures, now: float) -> bool | str:
        """Is a click starting? The index curls (fingertip moves fast relative to the
        knuckle) or the thumb and index close on each other quickly.

        Returns True, False, or "pending": a fast tip move seen this frame that is only
        confirmed as a curl if the tip is still displaced next frame. Landmark jitter
        snaps back; a real curl stays curled."""
        kx, ky = f.knuckle
        offset = (f.pointer[0] - kx, f.pointer[1] - ky)
        curling = False
        if self._curl_pending is not None:
            before, jump, seen = self._curl_pending
            self._curl_pending = None
            curling = _dist(offset, before) >= CURL_CONFIRM * jump
            if curling:
                self._click_seen = seen
        elif self._last_tip is not None:
            t0, prev = self._last_tip
            dt = max(now - t0, 1e-3)
            jump = _dist(offset, prev)
            if jump / dt / max(f.hand_size, 1.0) >= CURL_SPEED:
                if self._lock is None:
                    self._curl_pending = (prev, jump, now)  # starting a freeze needs confirmation
                else:
                    curling = True                      # already frozen: stay frozen while moving
        self._last_tip = (now, offset)

        # Any fingertip closing on the thumb, fast or slow, is a pinch starting. Remember
        # when that gap was last open so the cursor can be put back to before the drift.
        closing = False
        self._approach_t = None
        window = [(t, g) for t, g in self._gap_history if now - t <= APPROACH_WINDOW]
        current = (f.pinch_gap,) + tuple(f.gaps[1:])
        for i, gap in enumerate(current):
            series = [(t, g[i]) for t, g in window if len(g) > i]
            if not series or gap >= APPROACH_GAP:
                continue
            peak = max(gs for _, gs in series)
            if peak - gap >= APPROACH_DROP:
                closing = True
                self._approach_t = max(t for t, gs in series if gs == peak)
                break
        if curling or closing:
            return True
        return "pending" if self._curl_pending is not None else False

    def _clear_lock(self):
        self._lock = None
        self._magnet = None
        self._lock_knuckle = None
        self._curl_pending = None
        self._ease = None
        self._still_ref = None

    def _take_lock(self, f: HandFeatures):
        self._lock = (self._prev_x, self._prev_y)
        self._lock_knuckle = self._to_screen(f.knuckle)
        self._ease = None
        self._still_ref = None

    def _point_cursor(self, f: HandFeatures, now: float):
        target = self._to_screen(f.pointer)

        # A click starting: freeze, and undo the little the curl already moved the cursor.
        clicking = self._clicking(f, now)
        if clicking == "pending":
            return                      # hold for one frame until the curl is confirmed
        if clicking:
            self._damp_until = now + DAMP_S
            if self._lock is None:
                if self._magnet is None:
                    # Measured from when the motion was first seen, not from the confirmation.
                    seen = self._click_seen if self._click_seen is not None else now
                    when = seen - REWIND_S
                    if self._approach_t is not None:
                        when = min(when, self._approach_t)   # a slow pinch drifted for longer
                    self._rewind(when)
                self._take_lock(f)   # already sitting on a snapped control: lock right there
            self._click_seen = None
            return

        # While locked, only the hand itself moving (the knuckle) releases the lock.
        # The fingertip is ignored here because a curl moves it without the hand moving.
        if self._lock is not None:
            if _dist(self._to_screen(f.knuckle), self._lock_knuckle) < HOVER_BREAK_PX:
                return
            self._lock = None
            self._magnet = None
            self._resume_tracking(target, now)

        # Magnetic controls: over a button, switch or link, the cursor sits on its
        # centre until the finger clearly leaves it. Small targets stop being hard.
        finger = self._mirror(target)
        if self._magnet is not None:
            if _inside(self._magnet, finger, MAGNET_MARGIN_PX):
                return
            self._magnet = None
            self._resume_tracking(target, now)
        elif now - self._magnet_polled >= MAGNET_POLL_S - 1e-6:
            self._magnet_polled = now
            rect = self.backend.target_at(*finger)
            if rect is not None:
                self._magnet = rect
                x, y = self.screen_w - (rect[0] + rect[2] / 2), rect[1] + rect[3] / 2
                self.filter_x.reset(x, now)
                self.filter_y.reset(y, now)
                self._emit(x, y)
                self._trail = []        # a click here must not rewind to before the snap
                self._still_ref = None
                return

        # Hover lock (optional): after holding still briefly, ignore small drift.
        if self.hover_lock:
            if self._still_ref is None or _dist(target, self._still_ref[0]) > HOVER_RADIUS_PX:
                self._still_ref = (target, now)
            elif (now - self._still_ref[1]) * 1000 >= HOVER_MS:
                self._take_lock(f)
                return

        self._glide_to(target)

    def _resume_tracking(self, target, now):
        """Leaving a lock: track the finger from here without a leap."""
        if self.adaptive.enabled:
            # One Euro would close the gap to the finger in a single frame. Track the
            # finger from here and fade the remaining gap out over EASE_S instead.
            self.filter_x.reset(target[0], now)
            self.filter_y.reset(target[1], now)
            # Timed from the previous frame so this frame already moves part of the way.
            start = max(self._prev_seen, now - EASE_S)
            self._ease = (start, self._prev_x - target[0], self._prev_y - target[1])

    def _mirror(self, p):
        """Engine coordinates to screen coordinates (the camera is mirrored)."""
        return (min(max(self.screen_w - p[0], 0), self.screen_w - 1),
                min(max(p[1], 0), self.screen_h - 1))

    def _drag_cursor(self, f: HandFeatures):
        """While a button is held, move by how far the knuckle travelled since the press."""
        k = self._to_screen(f.knuckle)
        if self._press_knuckle is None:
            self._press_knuckle = k
            self._press_cursor = (self._prev_x, self._prev_y)
            return
        dx, dy = k[0] - self._press_knuckle[0], k[1] - self._press_knuckle[1]
        if not self._drag_started:
            if (dx * dx + dy * dy) ** 0.5 < DRAG_DEADZONE_PX:
                return
            self._drag_started = True
        self._lock = None
        self._glide_to((self._press_cursor[0] + dx, self._press_cursor[1] + dy))
        if self._grabbing:
            cx, cy = self._cursor_screen()
            self.backend.move_grabbed_window(cx - self._grab_cursor[0], cy - self._grab_cursor[1])

    def _glide_to(self, target):
        tx, ty = target
        if self.adaptive.enabled and self._last_seen >= self._damp_until:
            x = self.filter_x.update(tx, self._last_seen)
            y = self.filter_y.update(ty, self._last_seen)
            if self._ease is not None:
                t0, ox, oy = self._ease
                remaining = 1.0 - (self._last_seen - t0) / EASE_S
                if remaining > 0:
                    x, y = x + ox * remaining, y + oy * remaining
                else:
                    self._ease = None
        else:
            x = self._prev_x + (tx - self._prev_x) / self.smooth
            y = self._prev_y + (ty - self._prev_y) / self.smooth
            self.filter_x.update(x, self._last_seen)   # keep in step for a smooth switch-over
            self.filter_y.update(y, self._last_seen)
        self._emit(x, y)
        self._trail.append((self._last_seen, x, y))
        self._trail = [t for t in self._trail if self._last_seen - t[0] <= TRAIL_S]

    def _rewind(self, when: float):
        """Move the cursor back to where it was at time `when` (or the oldest known)."""
        if not self._trail:
            return
        older = [t for t in self._trail if t[0] <= when]
        _, x, y = older[-1] if older else self._trail[0]
        # Move the filters back too, or they resume from the pre-rewind position and the
        # cursor leaps forward when the freeze ends.
        self.filter_x.reset(x, self._last_seen)
        self.filter_y.reset(y, self._last_seen)
        self._emit(x, y)
        self._trail = []

    def _cursor_screen(self):
        """Where the cursor currently is on screen (mirrored x)."""
        sx = min(max(self.screen_w - self._prev_x, 0), self.screen_w - 1)
        sy = min(max(self._prev_y, 0), self.screen_h - 1)
        return sx, sy

    def _emit(self, x, y):
        self._prev_x, self._prev_y = x, y
        # camera is mirrored relative to the user, so flip x
        sx = min(max(self.screen_w - x, 0), self.screen_w - 1)
        sy = min(max(y, 0), self.screen_h - 1)
        self.backend.move_to(sx, sy)


    def _remember_gaps(self, f: HandFeatures, now: float):
        self._gap_history.append((now, f.gaps))
        keep = max(SNAP_WINDOW, APPROACH_WINDOW)
        self._gap_history = [(t, g) for t, g in self._gap_history if now - t <= keep]

    def _snapped(self, finger: str) -> bool:
        now = self._gap_history[-1][0] if self._gap_history else 0.0
        recent = [g[PINCH_FINGERS.index(finger)] for t, g in self._gap_history
                  if now - t <= SNAP_WINDOW]
        if len(recent) < 2:
            return False
        return max(recent) - recent[-1] >= SNAP_DROP


def _inside(rect, p, pad=0):
    x, y, w, h = rect
    return x - pad <= p[0] < x + w + pad and y - pad <= p[1] < y + h + pad


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _interp(v, lo, hi, out_lo, out_hi):
    if v <= lo:
        return out_lo
    if v >= hi:
        return out_hi
    return out_lo + (v - lo) / (hi - lo) * (out_hi - out_lo)
