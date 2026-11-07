"""Find the focused text field and its text cursor through the macOS Accessibility API.

Returns None when nothing editable has focus, or off macOS. Needs the
Accessibility permission (already required for pyautogui to click and type).
"""

import os
import sys
import time

TEXT_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
MIN_LINE_HEIGHT = 6   # Chrome sometimes reports a 1 pt line at a bogus spot


def focused_text_caret():
    """Screen rect (x, y, w, h), top-left origin in points, of the text cursor,
    falling back to the text field's own frame. FIELD_WITHOUT_POSITION if a
    text field has focus but reports no usable position. None if no text field
    has focus."""
    field = focused_text_field()
    return None if field is None else field[1]


def focused_text_field(retry=True):
    """(element, caret) for the focused text field in another app, or None.
    `caret` is as described in focused_text_caret."""
    if sys.platform != "darwin":
        return None
    try:
        import ApplicationServices as AS
        from AppKit import NSWorkspace
    except Exception:
        return None

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None or app.processIdentifier() == os.getpid():
        return None
    app_el = AS.AXUIElementCreateApplication(app.processIdentifier())
    # Chrome and Electron apps only expose their text fields once asked to,
    # and build that tree a moment later.
    AS.AXUIElementSetAttributeValue(app_el, "AXManualAccessibility", True)
    el = _attr(AS, app_el, "AXFocusedUIElement")
    if el is None and retry:
        time.sleep(0.15)
        el = _attr(AS, app_el, "AXFocusedUIElement")
    if el is None:
        return None
    # Google Docs keeps keyboard focus on a hidden field and draws the caret on
    # the page. The page is the surface the click actually landed in.
    editable = _editable_ancestor(AS, el)
    if editable is not None:
        return editable, _caret_rect(AS, editable)
    host = _document_host(AS, el)
    if host is not None:
        return host, FIELD_WITHOUT_POSITION
    return None


def document_frame(el):
    """On-screen rect of the Google Docs page that contains `el`, or None.

    Docs (and the same editor on other browsers) does not put the visible caret
    on the focused control. The control is a hidden field; the page under the
    click is the document.
    """
    if el is None or sys.platform != "darwin":
        return None
    try:
        import ApplicationServices as AS
    except Exception:
        return None
    host = _document_host(AS, el)
    if host is None:
        return None
    frame = _frame(AS, host)
    if frame is None:
        return None
    # A browser window includes the tab strip and address bar. The document
    # starts below them; a click up there is not the Docs caret.
    if _attr(AS, host, "AXRole") == "AXWindow":
        x, y, w, h = frame
        chrome = 100
        if h <= chrome + 80:
            return None
        return (x, y + chrome, w, h - chrome)
    return frame


def field_frame(el):
    """Screen rect (x, y, w, h) of a text field, or None if it will not say.

    Same top-left point space as the caret rect. Used to tell a click in the
    field from a click somewhere else while the field still has focus.
    """
    if el is None or sys.platform != "darwin":
        return None
    try:
        import ApplicationServices as AS
    except Exception:
        return None
    pos, size = _attr(AS, el, "AXPosition"), _attr(AS, el, "AXSize")
    if pos is None or size is None:
        return None
    ok_p, (x, y) = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
    ok_s, (w, h) = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
    if not (ok_p and ok_s) or w < 1 or h < 1:
        return None
    rect = (x, y, w, h)
    return rect if _on_screen((x, y, 2, max(h, MIN_LINE_HEIGHT))) else None


def text_length(el):
    """Number of characters in a text field, or None if it will not say."""
    try:
        import ApplicationServices as AS
    except Exception:
        return None
    n = _attr(AS, el, "AXNumberOfCharacters")
    if n is None:
        value = _attr(AS, el, "AXValue")
        n = len(value) if isinstance(value, str) else None
    return n


def _caret_rect(AS, el):
    rng = _attr(AS, el, "AXSelectedTextRange")
    rect = _bounds_for_range(AS, el, rng) if rng is not None else None
    if rect is not None and _on_screen(rect):
        return rect
    pos, size = _attr(AS, el, "AXPosition"), _attr(AS, el, "AXSize")
    if pos is not None and size is not None:
        ok_p, (px, py) = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
        ok_s, (_sw, sh) = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
        # The field can be huge (a whole document); anchor to its top line.
        rect = (px + 8, py, 2, min(sh, 24))
        if ok_p and ok_s and _on_screen(rect):
            return rect
    return FIELD_WITHOUT_POSITION


FIELD_WITHOUT_POSITION = ()   # a text field has focus but its position is unknown


def _on_screen(rect):
    from AppKit import NSScreen
    x, y, _w, h = rect
    if h < MIN_LINE_HEIGHT:
        return False
    screens = NSScreen.screens()
    top = screens[0].frame().size.height
    for s in screens:
        (sx, sy), (sw, sh) = s.frame()
        if sx <= x < sx + sw and top - sy - sh <= y < top - sy:
            return True
    return False


def _attr(AS, el, name):
    err, value = AS.AXUIElementCopyAttributeValue(el, name, None)
    return value if err == 0 else None


def _is_editable(AS, el):
    if _attr(AS, el, "AXRole") in TEXT_ROLES:
        return True
    if _attr(AS, el, "AXEditable"):
        return True
    err, settable = AS.AXUIElementIsAttributeSettable(el, "AXValue", None)
    return err == 0 and bool(settable) and _attr(AS, el, "AXSelectedTextRange") is not None


def _editable_ancestor(AS, el):
    cur = el
    for _ in range(10):
        if cur is None:
            return None
        if _attr(AS, cur, "AXRole") == "AXWebArea":
            return None
        if _is_editable(AS, cur):
            return cur
        cur = _attr(AS, cur, "AXParent")
    return None


def is_text_control(el):
    """True for a real text field, not a web page that merely contains one."""
    if el is None or sys.platform != "darwin":
        return False
    try:
        import ApplicationServices as AS
    except Exception:
        return False
    return _attr(AS, el, "AXRole") in TEXT_ROLES


def _document_host(AS, el):
    """The web page element when `el` lives in a Google Docs document.

    The parent chain from Docs' hidden text field sometimes never reaches the
    page, so the window title is checked too.
    """
    cur, web, window, is_doc, other_page = el, None, None, False, False
    for _ in range(16):
        if cur is None:
            break
        role = _attr(AS, cur, "AXRole")
        url = _text(AS, cur, "AXURL").lower()
        classes = (_text(AS, cur, "AXDOMClassList") + _text(AS, cur, "AXDOMIdentifier")).lower()
        if "docs.google.com" in url or "kix-" in classes:
            is_doc = True
        if role == "AXWebArea":
            web = cur
            if url and "docs.google.com" not in url:
                other_page = True
        if role == "AXWindow":
            window = cur
            if not other_page and "google docs" in _text(AS, cur, "AXTitle").lower():
                is_doc = True
        cur = _attr(AS, cur, "AXParent")
    if not is_doc:
        # Focus can sit in an iframe whose parents stop before the window.
        top = _attr(AS, el, "AXWindow") or _attr(AS, el, "AXTopLevelUIElement")
        if top is not None and "google docs" in _text(AS, top, "AXTitle").lower():
            return top
        return None
    return web or window


def _text(AS, el, name):
    value = _attr(AS, el, name)
    return "" if value is None else str(value)


def _frame(AS, el):
    pos, size = _attr(AS, el, "AXPosition"), _attr(AS, el, "AXSize")
    if pos is None or size is None:
        return None
    ok_p, (x, y) = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
    ok_s, (w, h) = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
    if not (ok_p and ok_s) or w < 1 or h < 1:
        return None
    if not _on_screen((x, y, 2, max(h, MIN_LINE_HEIGHT))):
        return None
    return (x, y, w, h)


def _bounds_for_range(AS, el, rng):
    ok, r = AS.AXValueGetValue(rng, AS.kAXValueCFRangeType, None)
    if not ok:
        return None
    start, count = (r.location, r.length) if hasattr(r, "location") else r
    # An empty selection often has no bounds; measure the character before it instead.
    tries = [(start, count)] if count else []
    tries += [(start - 1, 1)] if start > 0 else []
    tries += [(start, 1)]
    for loc, length in tries:
        probe = AS.AXValueCreate(AS.kAXValueCFRangeType, (loc, length))
        err, value = AS.AXUIElementCopyParameterizedAttributeValue(el, "AXBoundsForRange", probe, None)
        if err != 0 or value is None:
            continue
        ok, rect = AS.AXValueGetValue(value, AS.kAXValueCGRectType, None)
        if not ok:
            continue
        (x, y), (w, h) = rect
        if h > 0:
            if loc == start - 1 and length == 1:
                x += w
            return (x, y, 2, h)
    return None
