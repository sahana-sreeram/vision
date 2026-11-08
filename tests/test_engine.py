from app import features
from app.actions import Backend
from app.defaults import BUNDLED
from app.engine import Engine
from app.gestures import Gesture
from app.handmodel import landmarks
from app.profiles import Mapping, Profile


class FakeBackend(Backend):
    def __init__(self):
        self.events = []
        self.pos = None

    def screen_size(self):
        return (1920, 1080)

    def move_to(self, x, y):
        self.pos = (x, y)

    def mouse_down(self):
        self.events.append("down")

    def mouse_up(self):
        self.events.append("up")

    def click(self, button="left"):
        self.events.append(f"click:{button}")

    def double_click(self):
        self.events.append("dblclick")

    def scroll(self, amount):
        self.events.append(f"scroll:{amount}")

    def dictate(self):
        self.events.append("dictate")


def feat(pattern, pinch=None, orientation="up", center=(320, 240)):
    return features.extract(landmarks(pattern, pinch=pinch, orientation=orientation, center=center))


def make_engine(profile=None):
    b = FakeBackend()
    e = Engine(b, frame_size=(640, 480), margin=100, smooth=1)
    e.set_profile(profile or next(p for p in BUNDLED() if p.id == "default"))
    return e, b


def snap(e, pose, t):
    """Open the hand, then close into `pose`, so the pinch counts as a snap."""
    e.update(feat("01000"), t)
    e.update(pose, t + 0.05)
    return t + 0.05


def test_held_pinch_without_a_snap_does_not_click():
    e, b = make_engine()
    pinched = feat("11111", pinch="index")
    e.update(pinched, 0.0)
    e.update(pinched, 0.2)
    e.update(pinched, 0.5)
    assert b.events == []


def test_oneshot_fires_once_after_hold():
    e, b = make_engine()
    f = feat("11111", pinch="middle")  # right click, hold 120 ms
    t = snap(e, f, 0.0)
    assert b.events == [] and e.candidate is not None and e.active is None
    e.update(f, t + 0.05)
    assert b.events == []
    e.update(f, t + 0.13)
    assert b.events == ["click:right"]
    e.update(f, t + 0.5)
    e.update(f, t + 1.0)
    assert b.events == ["click:right"]
    # release and re-pinch fires again
    t = snap(e, f, t + 1.1)
    e.update(f, t + 0.13)
    assert b.events == ["click:right", "click:right"]


def test_button_down_up_and_release_on_hand_loss():
    e, b = make_engine()
    f = feat("11111", pinch="index")  # left_button, hold 0
    t = snap(e, f, 0.0)
    assert b.events == ["down"]
    e.update(f, t + 0.1)
    assert b.events == ["down"]
    e.update(None, t + 0.2)
    assert b.events == ["down"]          # within LOST_TIMEOUT
    e.update(None, t + 0.6)
    assert b.events == ["down", "up"]


def test_button_released_on_gesture_change():
    e, b = make_engine()
    t = snap(e, feat("11111", pinch="index"), 0.0)
    e.update(feat("01000"), t + 0.1)
    assert b.events == ["down", "up"]


def test_move_tracks_pointer_and_mirrors_x():
    e, b = make_engine()
    e.update(feat("01000", center=(150, 240)), 0.0)
    left = b.pos
    e.update(feat("01000", center=(500, 240)), 0.1)
    right = b.pos
    assert left[0] > right[0]  # hand on the camera's left = user's right
    assert 0 <= left[0] < 1920 and 0 <= left[1] < 1080


def test_scroll_runs_every_frame_and_respects_orientation():
    e, b = make_engine()
    f = feat("01100", orientation="down")
    e.update(f, 0.0)
    e.update(f, 0.2)
    e.update(f, 0.3)
    assert b.events == ["scroll:-60", "scroll:-60"]
    e.update(feat("01100", orientation="up"), 0.4)
    e.update(feat("01100", orientation="up"), 0.6)
    assert b.events[-1] == "scroll:60"


def test_pause_releases_and_blocks():
    e, b = make_engine()
    snap(e, feat("11111", pinch="index"), 0.0)
    e.set_paused(True)
    assert b.events == ["down", "up"]
    e.update(feat("11111", pinch="index"), 0.5)
    assert b.events == ["down", "up"]


def test_most_specific_mapping_wins():
    p = Profile("t", "t", [
        Mapping("scroll_up", Gesture("*****"), 0),
        Mapping("left_click", Gesture("01000"), 0),
    ])
    e, b = make_engine(p)
    e.update(feat("01000"), 0.0)
    assert b.events == ["click:left"]


def test_pinch_start_freezes_cursor_and_drag_needs_real_motion():
    import dataclasses
    e, b = make_engine()
    positions = []
    for i, t in enumerate([0.0, 0.1, 0.2, 0.3]):
        e.update(feat("01000", center=(200 + i * 40, 240)), t)
        positions.append(b.pos)
    # thumb and index closing fast: the cursor is put back where it was ~120 ms ago and frozen
    closing = dataclasses.replace(feat("01000", center=(400, 240)), pinch_gap=0.4)
    e.update(closing, 0.33)
    before = b.pos
    assert before == positions[1]          # rewound ~150 ms, to the t=0.1 position, not the latest
    # pinch lands: button down at the frozen position, small wobble ignored
    e.update(feat("11111", pinch="index", center=(400, 240)), 0.36)
    e.update(feat("11111", pinch="index", center=(404, 242)), 0.4)
    assert b.events == ["down"] and b.pos == before
    # moving the whole hand drags
    e.update(feat("11111", pinch="index", center=(520, 240)), 0.5)
    assert b.pos != before
    e.update(feat("01000", center=(520, 240)), 0.6)
    assert b.events == ["down", "up"]


def test_small_motion_after_long_hold_is_not_locked():
    e, b = make_engine()
    e.hover_lock = False   # hover lock is optional (tray menu); this checks the off behaviour
    for i in range(31):
        e.update(feat("01000", center=(300, 240)), i / 30)
    before = b.pos
    e.update(feat("01000", center=(300.25, 240)), 1.04)
    assert b.pos[0] < before[0]  # responds on the very next frame
    assert abs(b.pos[0] - before[0]) < 2


def test_toggle_gesture_fires_even_while_paused_and_only_that():
    e, b = make_engine()
    e.set_paused(True)
    # a normal gesture does nothing while paused
    e.update(feat("11111", pinch="index"), 0.0)
    e.update(feat("11111", pinch="index"), 0.5)
    assert b.events == [] and not e.toggle_requested
    # the toggle gesture (thumb + pinky, 1.5 s) still works
    e.update(feat("10001"), 1.0)
    e.update(feat("10001"), 2.0)
    assert not e.toggle_requested
    e.update(feat("10001"), 2.6)
    assert e.toggle_requested
    e.toggle_requested = False
    e.update(feat("10001"), 5.0)      # latched: no repeat while held
    assert not e.toggle_requested


def test_suspended_engine_does_nothing_and_releases_buttons():
    e, b = make_engine()
    snap(e, feat("11111", pinch="index"), 0.0)
    e.set_suspended(True)
    assert b.events == ["down", "up"]
    e.update(feat("11111", pinch="index"), 0.5)
    e.update(feat("10001"), 1.0)
    e.update(feat("10001"), 3.0)
    assert b.events == ["down", "up"] and not e.toggle_requested
    e.set_suspended(False)
    snap(e, feat("11111", pinch="index"), 3.2)
    assert b.events == ["down", "up", "down"]


def test_classifier_adjusts_filter_without_replacing_gesture_logic():
    class MovingClassifier:
        def reset(self):
            pass

        def update(self, now, x, y):
            assert 0 <= x <= 1 and 0 <= y <= 1
            return 'MOVING', 0.95

    e, b = make_engine()
    e.adaptive.enabled = True   # adaptive One Euro is opt-in (toggle 'a' in the camera window)
    e.cursor_state = MovingClassifier()
    e.update(feat('01000', center=(200, 240)), 0.0)
    e.update(feat('01000', center=(250, 240)), 0.1)
    assert e.cursor_label == 'MOVING'
    assert e.filter_x.min_cutoff > 1.0
    assert e.filter_x.min_cutoff == e.filter_y.min_cutoff
    assert b.pos is not None
    e.adaptive.enabled = False
    e.update(feat('01000', center=(300, 240)), 0.2)
    assert e.filter_x.min_cutoff == 1.0 and e.filter_x.beta == 0.005
    e.update(None, 0.3)
    assert e.cursor_label == 'NO HAND'


def test_curling_finger_does_not_move_cursor_but_hand_motion_does():
    import dataclasses
    e, b = make_engine()
    f0 = feat("01000", center=(300, 240))
    for i in range(6):
        e.update(f0, i * 0.033)
    settled = b.pos
    # fingertip drops 60 px toward the palm in one frame (a curl); knuckle unchanged
    curled = dataclasses.replace(f0, pointer=(f0.pointer[0], f0.pointer[1] + 60))
    for i in range(6, 12):
        e.update(curled, i * 0.033)       # stays curled for 200 ms, pinch not landed yet
    assert abs(b.pos[1] - settled[1]) < 3
    # the whole hand moving (knuckle and tip together) still points
    e.update(feat("01000", center=(300, 330)), 12 * 0.033)
    e.update(feat("01000", center=(300, 330)), 13 * 0.033)
    assert b.pos[1] - settled[1] > 30


def test_hover_lock_ignores_small_drift_until_clear_move():
    e, b = make_engine()
    e.update(feat("01000", center=(300, 240)), 0.0)
    e.update(feat("01000", center=(301, 240)), 0.2)
    e.update(feat("01000", center=(302, 241)), 0.5)   # still for 500 ms: locks
    locked = b.pos
    e.update(feat("01000", center=(305, 243)), 0.6)
    assert b.pos == locked
    e.update(feat("01000", center=(380, 240)), 0.7)   # clear move: follows again
    assert b.pos != locked


def test_hover_lock_can_be_turned_off():
    e, b = make_engine()
    e.hover_lock = False
    e.update(feat("01000", center=(300, 240)), 0.0)
    e.update(feat("01000", center=(301, 240)), 0.2)
    e.update(feat("01000", center=(302, 241)), 0.5)
    p = b.pos
    e.update(feat("01000", center=(306, 244)), 0.6)   # small correction still moves
    assert b.pos != p


def test_click_start_switches_to_heavy_smoothing_and_resets_filters():
    import dataclasses
    e, b = make_engine()
    e.hover_lock = False
    assert e.adaptive.enabled
    for i in range(5):
        e.update(feat("01000", center=(200 + i * 30, 240)), i * 0.1)
    closing = dataclasses.replace(feat("01000", center=(400, 240)), pinch_gap=0.4)
    e.update(closing, 0.5)
    assert e._damp_until > 0.5
    # the One Euro filters were reset to the rewound cursor position
    assert (e.filter_x._value, e.filter_y._value) == (e._prev_x, e._prev_y)


def test_all_fingertips_pinch_carries_the_window_under_the_cursor():
    e, b = make_engine()
    b.grabbed = None
    b.moves = []
    b.grab_window_at = lambda x, y: setattr(b, "grabbed", (x, y)) or True
    b.move_grabbed_window = lambda dx, dy: b.moves.append((dx, dy))
    b.release_window = lambda: setattr(b, "grabbed", "released")
    e.update(feat("01000", center=(300, 240)), 0.0)
    f = feat("11111", pinch="all", center=(300, 240))
    e.update(f, 0.1)
    e.update(f, 0.3)                      # past hold: window grabbed, no mouse button
    assert b.grabbed is not None and b.events == []
    e.update(feat("11111", pinch="all", center=(300, 330)), 0.4)   # hand moves down
    e.update(feat("11111", pinch="all", center=(300, 330)), 0.5)
    assert b.moves and b.moves[-1][1] > 0
    e.update(feat("01000", center=(300, 330)), 0.6)
    e.update(feat("01000", center=(300, 330)), 1.0)   # past the release grace
    assert b.grabbed == "released" and b.events == []


def test_move_window_falls_back_to_drag_when_unsupported():
    e, b = make_engine()                  # FakeBackend.grab_window_at -> False
    f = feat("11111", pinch="all")
    e.update(f, 0.0)
    e.update(f, 0.2)
    assert b.events == ["down"]
    e.update(feat("01000"), 0.3)
    e.update(feat("01000"), 0.7)          # past the release grace
    assert b.events == ["down", "up"]


def test_window_hold_survives_brief_detection_dropouts():
    e, b = make_engine()
    b.grabbed = None
    b.grab_window_at = lambda x, y: setattr(b, "grabbed", (x, y)) or True
    b.move_grabbed_window = lambda dx, dy: None
    b.release_window = lambda: setattr(b, "grabbed", "released")
    f = feat("11111", pinch="all")
    e.update(f, 0.0)
    e.update(f, 0.2)
    assert b.grabbed not in (None, "released")
    e.update(feat("11111", pinch="index"), 0.25)   # one misread frame
    e.update(None, 0.3)                            # one lost frame
    e.update(f, 0.35)
    assert b.grabbed != "released" and e.active.action == "move_window"
    e.update(feat("01000"), 0.5)                   # really let go: grace runs out
    e.update(feat("01000"), 0.9)
    assert b.grabbed == "released"


def test_slow_pinch_locks_and_rewinds_to_where_it_began():
    import dataclasses
    e, b = make_engine()
    e.hover_lock = False

    def frame(center, gap):
        return dataclasses.replace(feat("01000", center=center), pinch_gap=gap, gaps=(gap, 1.0, 1.0, 1.0))

    pos = {}
    for i, t in enumerate([0.0, 0.1, 0.2, 0.3]):
        e.update(frame((200 + i * 40, 240), 0.60), t)
        pos[t] = b.pos
    # the thumb and index close slowly over 300 ms while the tip keeps drifting
    for i, gap in enumerate([0.56, 0.52, 0.47]):
        t = 0.4 + i * 0.1
        e.update(frame((340 + i * 20, 240), gap), t)
        pos[t] = b.pos
    assert e._lock is not None
    # x is mirrored on screen: earlier (smaller camera x) means a larger screen x.
    # The cursor went back to where it was while the gap was still open (t = 0.3),
    # not just 150 ms before the lock.
    assert b.pos[0] > pos[0.5][0]
    assert b.pos[0] <= pos[0.2][0]


def cam(e, sx, sy):
    """Hand centre that puts the index fingertip on screen point (sx, sy); x is mirrored."""
    f0 = feat("01000", center=(320, 240))
    dx, dy = f0.pointer[0] - 320, f0.pointer[1] - 240
    return (e.margin + (e.screen_w - sx) / e.screen_w * (e.frame_w - 2 * e.margin) - dx,
            e.margin + sy / e.screen_h * (e.frame_h - 2 * e.margin) - dy)


def test_cursor_snaps_onto_small_controls_and_stays_until_the_finger_leaves():
    e, b = make_engine()
    e.hover_lock = False
    button = (1000, 500, 100, 40)
    b.target_at = lambda x, y: button if 1000 <= x < 1100 and 500 <= y < 540 else None
    e.update(feat("01000", center=cam(e, 700, 300)), 0.0)
    e.update(feat("01000", center=cam(e, 1010, 510)), 0.2)   # finger arrives over the button
    assert b.pos == (1050.0, 520.0)                           # cursor sits on its centre
    e.update(feat("01000", center=cam(e, 1090, 535)), 0.3)   # wobble inside: stays put
    e.update(feat("01000", center=cam(e, 1115, 535)), 0.4)   # just past the edge: still held
    assert b.pos == (1050.0, 520.0)
    e.update(feat("01000", center=cam(e, 1300, 535)), 0.5)   # clearly away: moves again
    e.update(feat("01000", center=cam(e, 1300, 535)), 0.7)
    assert b.pos != (1050.0, 520.0)


def test_click_on_a_snapped_control_does_not_rewind_off_it():
    import dataclasses
    e, b = make_engine()
    e.hover_lock = False
    button = (1000, 500, 100, 40)
    b.target_at = lambda x, y: button if 1000 <= x < 1100 and 500 <= y < 540 else None
    for i, t in enumerate([0.0, 0.1, 0.2]):
        e.update(feat("01000", center=cam(e, 600 + i * 100, 300)), t)
    e.update(feat("01000", center=cam(e, 1020, 520)), 0.3)
    assert b.pos == (1050.0, 520.0)
    closing = dataclasses.replace(feat("01000", center=cam(e, 1020, 520)), pinch_gap=0.4)
    e.update(closing, 0.35)
    assert b.pos == (1050.0, 520.0)
    e.update(feat("11111", pinch="index", center=cam(e, 1020, 520)), 0.4)
    e.update(feat("11111", pinch="index", center=cam(e, 1020, 520)), 0.45)
    assert b.events == ["down"] and b.pos == (1050.0, 520.0)
