import cv2
from app.detector import HandDetector, ensure_model, draw_hand
import numpy as np
import pyautogui
import time
import speech_recognition as sr
from app.ui.mac_magnifier import ScreenMagnifier
from cursor_stabilization import OneEuroFilter
from cursor_stabilization.cursor_state import CursorState
from cursor_stabilization.adaptive_smoothing import AdaptiveSmoothing

class GestureRecognizer:
    def __init__(self, activeMode=False, maxHands=1, detectionConfidence=0.5, trackingConfidence=0.5):
        self.activeMode = activeMode
        self.maxHands = maxHands
        self.detectionConfidence = detectionConfidence
        self.trackingConfidence = trackingConfidence

        ensure_model()
        self.handProcessor = HandDetector(max_hands=maxHands, confidence=detectionConfidence)
        self.fingerTipIndices = [4, 8, 12, 16, 20]
        self.positions = None

    def detectHands(self, frame, draw=True):
        self.positions = self.handProcessor.detect(frame, int(time.monotonic() * 1000))
        if draw and self.positions:
            draw_hand(frame, self.positions)
        return frame

    def getPositions(self, frame, handIndex=0, draw=True):
        self.landmarkList = ([[i, int(x), int(y)] for i, (x, y) in enumerate(self.positions)]
                             if self.positions else [])
        return self.landmarkList, ()

    def fingersRaised(self):
        fingers = []

        if self.landmarkList[self.fingerTipIndices[0]][1] > self.landmarkList[self.fingerTipIndices[0] - 1][1]:
            fingers.append(1)
        else:
            fingers.append(0)

        for i in range(1, 5):
            if self.landmarkList[self.fingerTipIndices[i]][2] < self.landmarkList[self.fingerTipIndices[i] - 2][2]:
                fingers.append(1)
            else:
                fingers.append(0)

        return fingers

def main(recorder=None):
    w = 640
    h = 480
    frameR = 100
    cursor_state = CursorState()
    cursor_filter_x = OneEuroFilter()
    cursor_filter_y = OneEuroFilter()
    adaptive = AdaptiveSmoothing(cursor_filter_x, cursor_filter_y, enabled=recorder is None)
    curr_x, curr_y = 0, 0
    prev_time = 0
    scroll_down_speed = -60
    scroll_up_speed = 60
    interval = 0.01 # interval (in seconds) between keystrokes when typing.
    timeout = 5 # duration (in seconds) that the speech recognizer waits for the speaker to start talking before it times out.
    phrase_time_limit = 10 # duration (in seconds) for capturing speech after it has started, regardless of any pauses or breaks in speaking.

    r = sr.Recognizer()
    m = sr.Microphone()

    def speech_to_text():
        with m as source:
            r.adjust_for_ambient_noise(source)
            try:
                audio = r.listen(source, timeout=timeout, phrase_time_limit=phrase_time_limit)
                return r.recognize_google(audio)
            except (sr.UnknownValueError, sr.RequestError, sr.WaitTimeoutError):
                return ''

    cap = cv2.VideoCapture(0)
    cap.set(3, w)
    cap.set(4, h)
    cap.set(cv2.CAP_PROP_FPS, 60)

    detector = GestureRecognizer()
    scr_w, scr_h = pyautogui.size()

    hold = False
    magnifier = None

    try:
        magnifier = ScreenMagnifier()
        while True:
            ok, img = cap.read()
            if not ok:
                break
            observed_at = time.monotonic()
            state_text = "NO HAND"
            img = detector.detectHands(img)
            lmList, _ = detector.getPositions(img)

            if len(lmList) != 0:
                x1, y1 = lmList[8][1:]
                fingers = detector.fingersRaised()
                pointing = fingers[1] == 1 and fingers[2] == 0
                dragging = fingers[0] == 0 and all(f == 1 for f in fingers[1:])
                mode = 'DRAG' if dragging else 'POINT' if pointing else 'OTHER'
                # Classify raw input before smoothing to avoid feedback from the filter.
                raw_x = 1.0 - float(np.interp(x1, (frameR, w - frameR), (0, 1)))
                raw_y = float(np.interp(y1, (frameR, h - frameR), (0, 1)))
                if recorder is not None:
                    recorder.write(observed_at, raw_x, raw_y, mode)
                if pointing or dragging:
                    state, score = cursor_state.update(observed_at, raw_x, raw_y)
                    state_text = f'{state}  score {score:.2f}'
                    adaptive.update(state, observed_at)
                else:
                    cursor_state.reset()
                    state_text = 'INACTIVE'
                    adaptive.update('INACTIVE', observed_at)

                cv2.rectangle(img, (frameR, frameR), (w - frameR, h - frameR), (255, 0, 255), 2)

                if fingers[1] == 1 and fingers[2] == 0:
                    x3 = np.interp(x1, (frameR, w - frameR), (0, scr_w))
                    y3 = np.interp(y1, (frameR, h - frameR), (0, scr_h))

                    timestamp = time.monotonic()
                    curr_x = cursor_filter_x.update(x3, timestamp)
                    curr_y = cursor_filter_y.update(y3, timestamp)

                    pyautogui.moveTo(min(scr_w - 1, max(0, scr_w - curr_x)), min(scr_h - 1, max(0, curr_y)), _pause=False)
                    cv2.circle(img, (x1, y1), 7, (255, 0, 255), cv2.FILLED)

                # Double left click
                if fingers[1] == 1 and fingers[4] == 1 and all(f == 0 for f in [fingers[0], fingers[2], fingers[3]]):
                    pyautogui.click(_pause=False)
                    pyautogui.click(_pause=False)

                # Hold left click and move
                if fingers[0] == 0 and all(f == 1 for f in fingers[1:]):
                    if not hold:
                        pyautogui.mouseDown(_pause=False)
                        hold = True

                    x3 = np.interp(x1, (frameR, w - frameR), (0, scr_w))
                    y3 = np.interp(y1, (frameR, h - frameR), (0, scr_h))
                    timestamp = time.monotonic()
                    curr_x = cursor_filter_x.update(x3, timestamp)
                    curr_y = cursor_filter_y.update(y3, timestamp)
                    pyautogui.moveTo(min(scr_w - 1, max(0, scr_w - curr_x)), min(scr_h - 1, max(0, curr_y)), _pause=False)
                    cv2.circle(img, (x1, y1), 7, (255, 0, 255), cv2.FILLED)
                else:
                    if hold:
                        pyautogui.mouseUp(_pause=False)
                        hold = False

                # Scroll down
                if all(f == 0 for f in fingers):
                    pyautogui.scroll(scroll_down_speed)

                # Scroll up
                if all(f == 1 for f in fingers):
                    pyautogui.scroll(scroll_up_speed)

                # Speech to text
                if fingers[2] == 1 and all(f == 0 for f in [fingers[0], fingers[1], fingers[3], fingers[4]]):
                    text = speech_to_text()
                    if text:
                        pyautogui.write(text, interval=interval)

            if not lmList:
                cursor_state.reset()
                adaptive.update('NO HAND', observed_at)
                if recorder is not None:
                    recorder.write(observed_at, '', '', 'NO HAND')
            cv2.putText(img, state_text, (20, 85), cv2.FONT_HERSHEY_SIMPLEX,
                        0.65, (0, 255, 255), 2)
            cv2.putText(img, 'Adaptive ON (a: toggle)' if adaptive.enabled else 'Adaptive OFF (a: toggle)',
                        (20, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
            if recorder is not None:
                label = recorder.label or 'PAUSED'
                cv2.putText(img, recorder.prompt or f'REC {label}: 1=still 2=fine 3=move 0=pause',
                            (10, h - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
            curr_time = time.time()
            fps = 1 / (curr_time - prev_time)
            prev_time = curr_time
            cv2.putText(img, str(int(fps)), (20, 50), cv2.FONT_HERSHEY_PLAIN, 3, (0, 0, 0), 3)
            cv2.imshow("Gesture Recognition", img)
            magnifier.update()

            key = cv2.waitKey(1) & 0xFF
            if recorder is not None:
                recorder.select(key, time.monotonic())
            elif key == ord('a'):
                adaptive.enabled = not adaptive.enabled
                adaptive.update('UNCERTAIN', time.monotonic())
            if key == ord('q') or (recorder is not None and recorder.done):
                break
    finally:
        detector.handProcessor.close()
        cap.release()
        cv2.destroyAllWindows()
        if magnifier is not None:
            magnifier.close()

if __name__ == "__main__":
    main()
