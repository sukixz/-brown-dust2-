"""Screen-recognition helper for the BrownDust II bargain loop.

The program only captures the visible game client, compares it with local
reference images, and sends ordinary keyboard/mouse input to the game window.
It does not read game memory, inject code, or alter game files.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import cv2
import numpy as np
from PIL import ImageGrab


# Use the Windows user32 API directly. This avoids the pywin32 DLL dependency,
# which can fail when Python and pywin32 have different architectures or when
# the pywin32 post-install registration is incomplete.
USER32 = ctypes.WinDLL("user32", use_last_error=True)
ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

USER32.EnumWindows.argtypes = [ENUM_WINDOWS_PROC, wintypes.LPARAM]
USER32.EnumWindows.restype = wintypes.BOOL
USER32.IsWindowVisible.argtypes = [wintypes.HWND]
USER32.IsWindowVisible.restype = wintypes.BOOL
USER32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
USER32.GetWindowTextLengthW.restype = ctypes.c_int
USER32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
USER32.GetWindowTextW.restype = ctypes.c_int
USER32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
USER32.GetWindowRect.restype = wintypes.BOOL
USER32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
USER32.GetClientRect.restype = wintypes.BOOL
USER32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
USER32.ClientToScreen.restype = wintypes.BOOL
USER32.IsIconic.argtypes = [wintypes.HWND]
USER32.IsIconic.restype = wintypes.BOOL
USER32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
USER32.ShowWindow.restype = wintypes.BOOL
USER32.SetForegroundWindow.argtypes = [wintypes.HWND]
USER32.SetForegroundWindow.restype = wintypes.BOOL
USER32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
USER32.SetCursorPos.restype = wintypes.BOOL
USER32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
USER32.mouse_event.restype = None
USER32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
USER32.keybd_event.restype = None
USER32.GetAsyncKeyState.argtypes = [ctypes.c_int]
USER32.GetAsyncKeyState.restype = ctypes.c_short


WINDOW_TITLE = "BrownDust II"
# Detection templates were made from the original 1334x753 client captures.
# Input coordinates use the user's unified 1920x1080 game resolution.
DETECTION_WIDTH = 1334
DETECTION_HEIGHT = 753
INPUT_REFERENCE_WIDTH = 1920
INPUT_REFERENCE_HEIGHT = 1080
REFERENCE_FULL_HEIGHT = 792
REFERENCE_CLIENT_TOP = 39

SCENE1_CLICK = (190, 910)
SCENE2_CLICK = (1054, 657)
SCENE3_CLICK = (176, 52)
SCENE4_CLICK = (1049, 632)
RETRY_INTERVAL_SECONDS = 2.0

VK_F8 = 0x77
VK_F9 = 0x78

SW_RESTORE = 9
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    source_rect: tuple[int, int, int, int]
    threshold: float
    min_brightness: float = 0.0


# Coordinates are relative to the game client, not the desktop. They are
# taken from the supplied 1334x753 client area and are normalized at runtime.
TEMPLATES = (
    TemplateSpec("scene1", (80, 550, 180, 725), 0.64, 0.0),
    TemplateSpec("scene2", (480, 250, 860, 505), 0.68, 0.0),
    TemplateSpec("scene3", (90, 75, 340, 740), 0.60, 38.0),
    TemplateSpec("scene4", (480, 255, 860, 490), 0.70, 0.0),
)


def set_process_dpi_aware() -> None:
    """Keep client-to-screen coordinates correct on scaled Windows displays."""

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def find_window(title: str) -> int | None:
    matches: list[tuple[int, int]] = []

    def callback(hwnd: int, _extra: int) -> bool:
        if not USER32.IsWindowVisible(hwnd):
            return True
        length = USER32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        USER32.GetWindowTextW(hwnd, buffer, length + 1)
        current_title = buffer.value
        if title.lower() not in current_title.lower():
            return True
        rect = wintypes.RECT()
        if not USER32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        matches.append((area, int(hwnd)))
        return True

    USER32.EnumWindows(ENUM_WINDOWS_PROC(callback), 0)
    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def get_client_box(hwnd: int) -> tuple[int, int, int, int] | None:
    client_rect = wintypes.RECT()
    if not USER32.GetClientRect(hwnd, ctypes.byref(client_rect)):
        return None
    top_left = wintypes.POINT(client_rect.left, client_rect.top)
    bottom_right = wintypes.POINT(client_rect.right, client_rect.bottom)
    if not USER32.ClientToScreen(hwnd, ctypes.byref(top_left)):
        return None
    if not USER32.ClientToScreen(hwnd, ctypes.byref(bottom_right)):
        return None
    if bottom_right.x <= top_left.x or bottom_right.y <= top_left.y:
        return None
    return top_left.x, top_left.y, bottom_right.x, bottom_right.y


def capture_client(hwnd: int) -> np.ndarray | None:
    box = get_client_box(hwnd)
    if box is None:
        return None
    try:
        image = ImageGrab.grab(bbox=box, all_screens=True).convert("RGB")
    except (OSError, ValueError):
        return None
    rgb = np.asarray(image)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def crop(frame: np.ndarray, rect: tuple[int, int, int, int]) -> np.ndarray:
    x1, y1, x2, y2 = rect
    return frame[y1:y2, x1:x2]


def normalized_rect(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = rect
    return (
        round(x1 * DETECTION_WIDTH / DETECTION_WIDTH),
        round(y1 * DETECTION_HEIGHT / DETECTION_HEIGHT),
        round(x2 * DETECTION_WIDTH / DETECTION_WIDTH),
        round(y2 * DETECTION_HEIGHT / DETECTION_HEIGHT),
    )


class Detector:
    def __init__(self, assets_dir: Path) -> None:
        self.assets_dir = assets_dir
        self.templates: dict[str, np.ndarray] = {}
        self._load_templates()

    def _load_templates(self) -> None:
        for spec in TEMPLATES:
            path = self.assets_dir / f"{spec.name}.png"
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                raise FileNotFoundError(f"Template not found: {path}")
            # Clipboard captures can include a few extra border pixels. Normalize
            # the complete capture before removing the title bar so all supplied
            # references share the same client-coordinate system.
            image = cv2.resize(
                image,
                (DETECTION_WIDTH, REFERENCE_FULL_HEIGHT),
                interpolation=cv2.INTER_AREA,
            )
            client = image[REFERENCE_CLIENT_TOP:REFERENCE_FULL_HEIGHT, :DETECTION_WIDTH]
            template = crop(client, normalized_rect(spec.source_rect))
            self.templates[spec.name] = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)

    def score(self, name: str, frame: np.ndarray) -> float:
        spec = next(item for item in TEMPLATES if item.name == name)
        current = cv2.resize(
            frame,
            (DETECTION_WIDTH, DETECTION_HEIGHT),
            interpolation=cv2.INTER_AREA,
        )
        current_crop = crop(current, normalized_rect(spec.source_rect))
        if current_crop.size == 0:
            return -1.0
        gray = cv2.cvtColor(current_crop, cv2.COLOR_BGR2GRAY)
        template = self.templates[name]
        result = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
        score = float(result[0, 0])
        if spec.min_brightness:
            brightness = float(np.mean(gray))
            if brightness < spec.min_brightness:
                return min(score, 0.0)
        return score

    def matched(self, name: str, frame: np.ndarray) -> tuple[bool, float]:
        spec = next(item for item in TEMPLATES if item.name == name)
        score = self.score(name, frame)
        return score >= spec.threshold, score


def focus_window(hwnd: int) -> None:
    if USER32.IsIconic(hwnd):
        USER32.ShowWindow(hwnd, SW_RESTORE)
    USER32.SetForegroundWindow(hwnd)
    time.sleep(0.08)


def click_client_point(hwnd: int, x: int, y: int) -> None:
    box = get_client_box(hwnd)
    if box is None:
        return
    client_left, client_top, _, _ = box
    focus_window(hwnd)
    USER32.SetCursorPos(client_left + x, client_top + y)
    time.sleep(0.04)
    USER32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.035)
    USER32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def reference_point(hwnd: int, x: int, y: int) -> tuple[int, int] | None:
    box = get_client_box(hwnd)
    if box is None:
        return None
    _, _, client_right, client_bottom = box
    width = client_right - box[0]
    height = client_bottom - box[1]
    return round(x * width / INPUT_REFERENCE_WIDTH), round(y * height / INPUT_REFERENCE_HEIGHT)


def click_reference_point(hwnd: int, point: tuple[int, int]) -> None:
    scaled = reference_point(hwnd, *point)
    if scaled is not None:
        logging.info("mouse click ref=%s client=%s", point, scaled)
        click_client_point(hwnd, *scaled)


class Automation:
    def __init__(
        self,
        detector: Detector,
        title: str,
        dry_run: bool = False,
        once: bool = False,
    ) -> None:
        self.detector = detector
        self.title = title
        self.dry_run = dry_run
        self.once = once
        self.state = "scene1"
        self.state_started = time.monotonic()
        self.streak = 0
        self.paused = False
        self.stopped = False
        self.last_hotkey_state: dict[int, bool] = {}
        self.last_action: Callable[[int], None] | None = None
        self.last_action_at = 0.0
        self.action_attempts = 0
        self.scene_timeouts = {
            "scene1": 30.0,
            "scene2": 20.0,
            "scene3": 30.0,
            "scene4": 15.0,
        }

    def log(self, message: str, *args: object) -> None:
        logging.info(message, *args)

    def set_state(self, state: str) -> None:
        self.state = state
        self.state_started = time.monotonic()
        self.streak = 0
        self.log("state -> %s", state)

    def set_next_state(self, state: str, action: Callable[[int], None]) -> None:
        """Record a click transition so it can be retried if the UI stays put."""

        self.set_state(state)
        self.last_action = action
        self.last_action_at = time.monotonic()
        self.action_attempts = 1

    def retry_action_if_needed(self, hwnd: int) -> None:
        if self.dry_run or self.last_action is None:
            return
        if time.monotonic() - self.last_action_at < RETRY_INTERVAL_SECONDS:
            return
        self.action_attempts += 1
        self.log("%s not detected after %.1fs; retry action #%d", self.state, RETRY_INTERVAL_SECONDS, self.action_attempts)
        self.last_action(hwnd)
        self.last_action_at = time.monotonic()

    def hotkey_pressed(self, virtual_key: int) -> bool:
        down = bool(USER32.GetAsyncKeyState(virtual_key) & 0x8000)
        previous = self.last_hotkey_state.get(virtual_key, False)
        self.last_hotkey_state[virtual_key] = down
        return down and not previous

    def poll_controls(self) -> None:
        if self.hotkey_pressed(VK_F8):
            self.paused = not self.paused
            self.log("%s", "paused" if self.paused else "resumed")
        if self.hotkey_pressed(VK_F9):
            self.stopped = True
            self.log("stop requested")

    def confirmed_match(self, name: str, frame: np.ndarray) -> tuple[bool, float]:
        matched, score = self.detector.matched(name, frame)
        if matched:
            self.streak += 1
        else:
            self.streak = 0
        return self.streak >= 2, score

    def execute_scene1(self, hwnd: int) -> None:
        self.log("scene1 detected; click bargain skill at %s", SCENE1_CLICK)
        if not self.dry_run:
            click_reference_point(hwnd, SCENE1_CLICK)
        self.set_next_state("scene2", lambda window: click_reference_point(window, SCENE1_CLICK))

    def execute_scene2(self, hwnd: int) -> None:
        self.log("scene2 detected; click bargain button at %s", SCENE2_CLICK)
        if not self.dry_run:
            click_reference_point(hwnd, SCENE2_CLICK)
        self.set_next_state("scene3", lambda window: click_reference_point(window, SCENE2_CLICK))

    def execute_scene3(self, hwnd: int) -> None:
        self.log("scene3 detected; click back at %s", SCENE3_CLICK)
        if not self.dry_run:
            click_reference_point(hwnd, SCENE3_CLICK)
        self.set_next_state("scene4", lambda window: click_reference_point(window, SCENE3_CLICK))

    def execute_scene4(self, hwnd: int) -> None:
        self.log("scene4 detected; click close shop at %s", SCENE4_CLICK)
        if not self.dry_run:
            click_reference_point(hwnd, SCENE4_CLICK)
        if self.once:
            self.stopped = True
        else:
            self.set_next_state("scene1", lambda window: click_reference_point(window, SCENE4_CLICK))

    def run(self) -> None:
        self.log("waiting for window: %s", self.title)
        while not self.stopped:
            self.poll_controls()
            if self.paused:
                time.sleep(0.12)
                continue

            hwnd = find_window(self.title)
            if hwnd is None:
                time.sleep(0.5)
                continue

            frame = capture_client(hwnd)
            if frame is None:
                time.sleep(0.25)
                continue

            if self.state == "scene1":
                ready, score = self.confirmed_match("scene1", frame)
                if ready:
                    self.log("scene1 score=%.3f", score)
                    self.execute_scene1(hwnd)
            elif self.state == "scene2":
                ready, score = self.confirmed_match("scene2", frame)
                if ready:
                    self.log("scene2 score=%.3f", score)
                    self.execute_scene2(hwnd)
            elif self.state == "scene3":
                ready, score = self.confirmed_match("scene3", frame)
                if ready:
                    self.log("scene3 score=%.3f", score)
                    self.execute_scene3(hwnd)
            elif self.state == "scene4":
                ready, score = self.confirmed_match("scene4", frame)
                if ready:
                    self.log("scene4 score=%.3f", score)
                    self.execute_scene4(hwnd)

            self.retry_action_if_needed(hwnd)
            timeout = self.scene_timeouts.get(self.state)
            if timeout is not None and time.monotonic() - self.state_started > timeout:
                self.log("timeout in %s; restart recognition from scene1", self.state)
                self.set_state("scene1")
                self.last_action = None
                time.sleep(0.8)
            time.sleep(0.18)


def self_test(detector: Detector, logger: logging.Logger) -> int:
    passed = True
    for spec in TEMPLATES:
        path = detector.assets_dir / f"{spec.name}.png"
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            logger.error("missing image: %s", path)
            passed = False
            continue
        image = cv2.resize(image, (DETECTION_WIDTH, REFERENCE_FULL_HEIGHT), interpolation=cv2.INTER_AREA)
        client = image[REFERENCE_CLIENT_TOP:REFERENCE_FULL_HEIGHT, :DETECTION_WIDTH]
        matched, score = detector.matched(spec.name, client)
        logger.info("%s self-test score=%.3f threshold=%.3f", spec.name, score, spec.threshold)
        passed = passed and matched
    return 0 if passed else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BrownDust II bargain loop helper")
    parser.add_argument("--title", default=WINDOW_TITLE, help="game window title substring")
    parser.add_argument("--dry-run", action="store_true", help="recognize scenes without input")
    parser.add_argument("--once", action="store_true", help="stop after one complete loop")
    parser.add_argument("--self-test", action="store_true", help="validate the bundled templates")
    return parser.parse_args()


def main() -> int:
    set_process_dpi_aware()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    assets_dir = Path(__file__).resolve().parent / "assets"
    try:
        detector = Detector(assets_dir)
    except (FileNotFoundError, ValueError) as exc:
        logging.error("%s", exc)
        return 2
    if args.self_test:
        return self_test(detector, logging.getLogger(__name__))
    logging.info("F8 pause/resume, F9 stop")
    if args.dry_run:
        logging.info("dry-run enabled: no keyboard or mouse input will be sent")
    Automation(detector, args.title, args.dry_run, args.once).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
