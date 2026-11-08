"""Windows counterpart of caret.py: focused text field and text cursor via UI Automation."""

import ctypes
import os
import threading
from ctypes import wintypes

FIELD_WITHOUT_POSITION = ()
MIN_LINE_HEIGHT = 6

# UIA control types, patterns and properties
UIA_ComboBoxControlTypeId = 50003
UIA_EditControlTypeId = 50004
UIA_DocumentControlTypeId = 50030
UIA_ValuePatternId = 10002
UIA_TextPatternId = 10014
UIA_TextPattern2Id = 10024
UIA_HasKeyboardFocusPropertyId = 30008
UIA_ControlTypePropertyId = 30003
UIA_ValueIsReadOnlyPropertyId = 30046
TreeScope_Subtree = 7
TEXT_TYPES = {UIA_EditControlTypeId, UIA_DocumentControlTypeId, UIA_ComboBoxControlTypeId}

class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


_local = threading.local()
_lock = threading.Lock()


def _automation():
    """An IUIAutomation instance for the calling thread (COM objects stay in their apartment)."""
    if getattr(_local, "uia", None) is None:
        import comtypes
        import comtypes.client
        try:
            comtypes.CoInitializeEx()
        except OSError:
            pass   # already initialised in another mode on this thread
        with _lock:
            comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA
        _local.uia = (comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation), UIA)
    return _local.uia


# control types that are a single thing to click
CLICKABLE_TYPES = {50000, 50002, 50003, 50005, 50007, 50011, 50013, 50019, 50024, 50031}
# button, check box, combo box, hyperlink, list item, menu item, radio, tab, tree item, split button


def clickable_at(x, y, max_w=340, max_h=120):
    """Screen rect of the small button-like control under screen point (x, y), or None.
    The text inside a button is not the button; look a few levels up for it."""
    try:
        uia, UIA = _automation()
        from ctypes import wintypes
        el = uia.ElementFromPoint(wintypes.POINT(int(x), int(y)))
        if el is None or el.CurrentProcessId == os.getpid():
            return None
        walker = uia.ControlViewWalker
        for _ in range(3):
            if el is None:
                return None
            if el.CurrentControlType in CLICKABLE_TYPES:
                rect = _rect_of(el)
                if rect and rect[2] <= max_w and rect[3] <= max_h:
                    return rect
                return None
            el = walker.GetParentElement(el)
    except Exception:
        return None
    return None


def foreground_window():
    """(hwnd, pid) of the window in front."""
    u = ctypes.windll.user32
    hwnd = u.GetForegroundWindow()
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return hwnd, pid.value


def focused_text_field(retry=True):
    """(element, caret) for the focused text field in another app, or None."""
    hwnd, pid = foreground_window()
    if not hwnd or pid == os.getpid():
        return None
    try:
        uia, _ = _automation()
        el = uia.GetFocusedElement()
        if el is None or el.CurrentProcessId == os.getpid():
            return None
    except Exception:
        return None
    editable = _editable_ancestor(el)
    if editable is None and _is_xaml_host(el):
        editable = _focused_text_control(uia, hwnd)
    if editable is None:
        return None
    return editable, _caret_rect(editable)


XAML_HOSTS = {"InputSiteWindowClass", "Windows.UI.Core.CoreWindow",
              "Microsoft.UI.Content.DesktopChildSiteBridge",
              "Windows.UI.Composition.DesktopWindowContentBridge"}


def _is_xaml_host(el):
    try:
        return el.CurrentClassName in XAML_HOSTS
    except Exception:
        return False


def _focused_text_control(uia, foreground):
    """XAML apps (Notepad on Windows 11) report a hosting pane as focused; the text
    control that says it has keyboard focus is somewhere below it."""
    try:
        u = ctypes.windll.user32
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        hwnd = foreground
        if u.GetGUIThreadInfo(u.GetWindowThreadProcessId(foreground, None), ctypes.byref(info)):
            hwnd = info.hwndFocus or foreground
        root = uia.ElementFromHandle(hwnd)
        focused = uia.CreatePropertyCondition(UIA_HasKeyboardFocusPropertyId, True)
        types = None
        for t in TEXT_TYPES:
            c = uia.CreatePropertyCondition(UIA_ControlTypePropertyId, t)
            types = c if types is None else uia.CreateOrCondition(types, c)
        el = root.FindFirst(TreeScope_Subtree, uia.CreateAndCondition(focused, types))
        return el if el is not None and _is_editable(el) else None
    except Exception:
        return None


def _editable_ancestor(el):
    uia, _ = _automation()
    walker = uia.ControlViewWalker
    cur = el
    for _ in range(6):
        if cur is None:
            return None
        if _is_editable(cur):
            return cur
        try:
            cur = walker.GetParentElement(cur)
        except Exception:
            return None
    return None


def _is_editable(el):
    try:
        ctype = el.CurrentControlType
    except Exception:
        return False
    if ctype in TEXT_TYPES:
        if ctype == UIA_DocumentControlTypeId and _read_only(el):
            return False   # a web page or PDF is a document too, but not a place to type
        return True
    # contenteditable regions in browsers are often plain groups with a value pattern
    try:
        return el.GetCurrentPattern(UIA_ValuePatternId) is not None and not _read_only(el)
    except Exception:
        return False


def _read_only(el):
    try:
        return el.GetCurrentPropertyValue(UIA_ValueIsReadOnlyPropertyId) is True
    except Exception:
        return False


def _rect_of(el):
    try:
        r = el.CurrentBoundingRectangle
        x, y, w, h = r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:
        return None
    if w < 1 or h < 1:
        return None
    return (x, y, w, h)


def _caret_rect(el):
    rect = _text_pattern_caret(el) or _system_caret()
    if rect is not None and _on_screen(rect):
        return rect
    frame = _rect_of(el)
    if frame is not None:
        x, y, _w, h = frame
        rect = (x + 8, y, 2, min(h, 24))   # anchor a huge field to its top line
        if _on_screen(rect):
            return rect
    return FIELD_WITHOUT_POSITION


def _text_pattern_caret(el):
    try:
        uia, UIA = _automation()
        pat = el.GetCurrentPattern(UIA_TextPattern2Id)
        if pat is None:
            return None
        tp = pat.QueryInterface(UIA.IUIAutomationTextPattern2)
        _active, rng = tp.GetCaretRange()
        if rng is None:
            return None
        rects = rng.GetBoundingRectangles()
        if not rects or len(rects) < 4:
            rng.ExpandToEnclosingUnit(0)   # TextUnit_Character: an empty range has no box
            rects = rng.GetBoundingRectangles()
        if not rects or len(rects) < 4:
            return None
        x, y, h = rects[0], rects[1], rects[3]
        return (x, y, 2, h) if h > 0 else None
    except Exception:
        return None


def _system_caret():
    """The classic Win32 caret of the foreground window's thread, in screen pixels."""
    u = ctypes.windll.user32
    tid = u.GetWindowThreadProcessId(u.GetForegroundWindow(), None)
    info = _GUITHREADINFO()
    info.cbSize = ctypes.sizeof(info)
    if not u.GetGUIThreadInfo(tid, ctypes.byref(info)) or not info.hwndCaret:
        return None
    r = info.rcCaret
    if r.bottom - r.top < MIN_LINE_HEIGHT:
        return None
    pt = wintypes.POINT(r.left, r.top)
    u.ClientToScreen(info.hwndCaret, ctypes.byref(pt))
    return (pt.x, pt.y, 2, r.bottom - r.top)


def _on_screen(rect):
    x, y, _w, h = rect
    if h < MIN_LINE_HEIGHT:
        return False
    u = ctypes.windll.user32
    vx, vy, vw, vh = (u.GetSystemMetrics(i) for i in (76, 77, 78, 79))   # virtual screen
    return vx <= x < vx + vw and vy <= y < vy + vh


def field_frame(el):
    rect = _rect_of(el)
    if rect and _on_screen((rect[0], rect[1], 2, max(rect[3], MIN_LINE_HEIGHT))):
        return rect
    return None


def document_frame(el):
    """Google Docs draws its caret on the page while focus sits in a hidden field."""
    try:
        uia, _ = _automation()
        walker = uia.ControlViewWalker
        cur = el
        for _ in range(16):
            if cur is None:
                return None
            name = (cur.CurrentName or "").lower()
            if cur.CurrentControlType == UIA_DocumentControlTypeId and "google docs" in name:
                return _rect_of(cur)
            cur = walker.GetParentElement(cur)
    except Exception:
        return None
    return None


def is_text_control(el):
    try:
        return el.CurrentControlType in TEXT_TYPES
    except Exception:
        return False


def text_length(el):
    try:
        uia, UIA = _automation()
        pat = el.GetCurrentPattern(UIA_TextPatternId)
        if pat is not None:
            tp = pat.QueryInterface(UIA.IUIAutomationTextPattern)
            return len(tp.DocumentRange.GetText(-1))
        pat = el.GetCurrentPattern(UIA_ValuePatternId)
        if pat is not None:
            vp = pat.QueryInterface(UIA.IUIAutomationValuePattern)
            return len(vp.CurrentValue or "")
    except Exception:
        pass
    return None


def same_element(a, b):
    """UIA hands out a new wrapper each time; compare by identity, not by object."""
    if a is None or b is None:
        return a is b
    try:
        uia, _ = _automation()
        return bool(uia.CompareElements(a, b))
    except Exception:
        return False
