import objc
import pyautogui
from AppKit import (
    NSApplication,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSCompositingOperationSourceOver,
    NSGraphicsContext,
    NSImage,
    NSImageInterpolationNone,
    NSInsetRect,
    NSMakePoint,
    NSMakeRect,
    NSMidX,
    NSMidY,
    NSScreen,
    NSStatusWindowLevel,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowSharingNone,
    NSWindowStyleMaskBorderless,
    NSZeroRect,
    NSBitmapImageRep,
)
from Quartz import (
    CGRectMake,
    CGWindowListCreateImage,
    kCGWindowImageBestResolution,
    kCGWindowImageBoundsIgnoreFraming,
    kCGWindowListOptionOnScreenBelowWindow,
)


class MagnifierView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(MagnifierView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._image = None
        return self

    def isOpaque(self):
        return True

    def drawRect_(self, dirtyRect):
        bounds = self.bounds()
        NSColor.blackColor().set()
        NSBezierPath.fillRect_(bounds)

        if self._image is not None:
            NSGraphicsContext.currentContext().setImageInterpolation_(NSImageInterpolationNone)
            self._image.drawInRect_fromRect_operation_fraction_(
                bounds,
                NSZeroRect,
                NSCompositingOperationSourceOver,
                1.0,
            )

        NSColor.whiteColor().set()
        border = NSBezierPath.bezierPathWithRect_(NSInsetRect(bounds, 1.5, 1.5))
        border.setLineWidth_(3.0)
        border.stroke()

        mid_x = NSMidX(bounds)
        mid_y = NSMidY(bounds)
        arm = 8.0
        NSColor.yellowColor().set()
        hair = NSBezierPath.bezierPath()
        hair.moveToPoint_(NSMakePoint(mid_x - arm, mid_y))
        hair.lineToPoint_(NSMakePoint(mid_x + arm, mid_y))
        hair.moveToPoint_(NSMakePoint(mid_x, mid_y - arm))
        hair.lineToPoint_(NSMakePoint(mid_x, mid_y + arm))
        hair.setLineWidth_(2.0)
        hair.stroke()


class ScreenMagnifier:
    def __init__(self, box_w=320, box_h=240, zoom=2.0):
        self.box_w = float(box_w)
        self.box_h = float(box_h)
        self.zoom = float(zoom)
        self.src_w = self.box_w / self.zoom
        self.src_h = self.box_h / self.zoom
        self._closed = False

        NSApplication.sharedApplication()
        frame = NSMakeRect(0, 0, self.box_w, self.box_h)
        self.view = MagnifierView.alloc().initWithFrame_(frame)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setContentView_(self.view)
        self.window.setLevel_(NSStatusWindowLevel)
        self.window.setIgnoresMouseEvents_(True)
        self.window.setSharingType_(NSWindowSharingNone)
        self.window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self.window.setHidesOnDeactivate_(False)
        self.window.setOpaque_(True)
        self.window.setHasShadow_(False)
        self.window.setMovable_(False)
        self.window.setBackgroundColor_(NSColor.blackColor())
        self.window.orderFrontRegardless()

    def update(self):
        if self._closed:
            return

        x, y = pyautogui.position()
        # CGWindowListCreateImage is top-left, y downward — the same space as pyautogui.
        cgimage = CGWindowListCreateImage(
            CGRectMake(x - self.src_w / 2.0, y - self.src_h / 2.0, self.src_w, self.src_h),
            kCGWindowListOptionOnScreenBelowWindow,
            self.window.windowNumber(),
            kCGWindowImageBoundsIgnoreFraming | kCGWindowImageBestResolution,
        )
        self.view._image = self._image_from_capture(cgimage)
        self.view.setNeedsDisplay_(True)

        origin_x, origin_y = self._window_origin(x, y)
        self.window.setFrame_display_(
            NSMakeRect(origin_x, origin_y, self.box_w, self.box_h),
            True,
        )

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.window.orderOut_(None)
        self.window.close()

    def _window_origin(self, x, y):
        # AppKit window frames are bottom-left of the primary display.
        screens = NSScreen.screens()
        primary_height = screens[0].frame().size.height if screens else self.box_h
        origin_x = x - self.box_w / 2.0
        origin_y = primary_height - y - self.box_h / 2.0
        return origin_x, origin_y

    def _image_from_capture(self, cgimage):
        if cgimage is None:
            return None
        rep = NSBitmapImageRep.alloc().initWithCGImage_(cgimage)
        if rep is None:
            return None
        image = NSImage.alloc().initWithSize_(rep.size())
        image.addRepresentation_(rep)
        return image
