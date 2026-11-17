import ctypes
from ctypes import wintypes
import json
import math
import os
import threading
import time
import tkinter as tk
from tkinter import ttk

import cv2
import mss
import numpy as np


# ---------------------------------------------------------------------------
# Windows 11 x64 DPI Awareness & Native Win32 Helpers (No Admin Required)
# ---------------------------------------------------------------------------
def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


user32 = ctypes.windll.user32

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
VK_F8 = 0x77
SW_RESTORE = 9

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION),
    ]


def send_left_click(screen_x: int, screen_y: int) -> None:
    """Moves cursor to exact screen coordinates and dispatches a hardware-style click."""
    user32.SetCursorPos(int(screen_x), int(screen_y))
    time.sleep(0.025)

    nudge_a = INPUT(type=0, union=_INPUT_UNION(mi=MOUSEINPUT(dx=1, dy=0, dwFlags=MOUSEEVENTF_MOVE)))
    nudge_b = INPUT(type=0, union=_INPUT_UNION(mi=MOUSEINPUT(dx=-1, dy=0, dwFlags=MOUSEEVENTF_MOVE)))
    down = INPUT(type=0, union=_INPUT_UNION(mi=MOUSEINPUT(dwFlags=MOUSEEVENTF_LEFTDOWN)))
    up = INPUT(type=0, union=_INPUT_UNION(mi=MOUSEINPUT(dwFlags=MOUSEEVENTF_LEFTUP)))

    user32.SendInput(1, ctypes.byref(nudge_a), ctypes.sizeof(INPUT))
    time.sleep(0.01)
    user32.SendInput(1, ctypes.byref(nudge_b), ctypes.sizeof(INPUT))
    time.sleep(0.015)
    user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    time.sleep(0.045)
    user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))


def find_roblox_window() -> tuple[int | None, tuple[int, int, int, int] | None]:
    """Finds the visible Roblox client window and returns (hwnd, (left, top, width, height))."""
    found: list[tuple[int, tuple[int, int, int, int]]] = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        title = buff.value.strip()
        if title == "Roblox" or "Roblox" in title:
            rect = wintypes.RECT()
            if user32.GetClientRect(hwnd, ctypes.byref(rect)):
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w >= 400 and h >= 300:
                    pt = wintypes.POINT(0, 0)
                    user32.ClientToScreen(hwnd, ctypes.byref(pt))
                    found.append((hwnd, (pt.x, pt.y, w, h)))
                    return False
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    if not found:
        return None, None
    return found[0]


def is_window_foreground(hwnd: int) -> bool:
    return user32.GetForegroundWindow() == hwnd


def focus_window(hwnd: int) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


# ---------------------------------------------------------------------------
# Portable Configuration Manager (%APPDATA%\BloxburgAutoFish\config.json)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "bite_sensitivity": 65,      # 10 (less sensitive) to 100 (very sensitive)
    "cast_delay": 1.2,           # Seconds before casting after ready state
    "reel_cooldown": 1.6,        # Seconds to wait after reeling before next cycle
    "max_wait_timeout": 28.0,    # Failsafe timeout if no bite is detected
    "auto_focus": True,          # Automatically focus Roblox when starting
    "always_on_top": True,       # Keep mini controller above game window
}


def get_config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    cfg_dir = os.path.join(base, "BloxburgAutoFish")
    os.makedirs(cfg_dir, exist_ok=True)
    return os.path.join(cfg_dir, "config.json")


def load_config() -> dict:
    path = get_config_path()
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg.update({k: loaded[k] for k in DEFAULT_CONFIG if k in loaded})
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(get_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Computer Vision Fishing Interface Analyzer
# ---------------------------------------------------------------------------
class BloxburgVisionAnalyzer:
    """
    Resolution-adaptive OpenCV detector for Bloxburg's fishing UI:
      1. Detects the bottom-center circular blue action button ('Cast' / 'Pull').
      2. Classifies button state using normalized label geometry & state tracking.
      3. Tracks the floating bobber and detects bite splashes via temporal diffing.
    """

    def __init__(self) -> None:
        self.bobber_pos: tuple[int, int] | None = None
        self.prev_bobber_patch: np.ndarray | None = None
        self.baseline_splash_area: float = 0.0
        self.calm_frames: int = 0

    def reset_bite_tracker(self) -> None:
        self.bobber_pos = None
        self.prev_bobber_patch = None
        self.baseline_splash_area = 0.0
        self.calm_frames = 0

    def analyze_action_button(
        self, frame_bgr: np.ndarray
    ) -> tuple[str, tuple[int, int, int] | None, float]:
        """
        Returns (button_state, (cx, cy, radius), confidence_pct).
        button_state is 'CAST', 'PULL', or 'NONE'.
        """
        h, w = frame_bgr.shape[:2]
        x1 = int(w * 0.38)
        x2 = int(w * 0.62)
        y1 = int(h * 0.78)
        y2 = int(h * 0.99)

        roi = frame_bgr[y1:y2, x1:x2]
        if roi.size == 0:
            return "NONE", None, 0.0

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        lower_blue = np.array([92, 115, 135], dtype=np.uint8)
        upper_blue = np.array([115, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_blue, upper_blue)

        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return "NONE", None, 0.0

        best_score = 0.0
        best_circle: tuple[int, int, int] | None = None

        min_radius = max(14, int(h * 0.022))
        max_radius = max(30, int(h * 0.075))

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < math.pi * (min_radius ** 2) * 0.55:
                continue
            perimeter = cv2.arcLength(cnt, True)
            if perimeter <= 0:
                continue

            circularity = min(1.0, (4.0 * math.pi * area) / (perimeter ** 2))
            if circularity < 0.58:
                continue

            (cx_local, cy_local), radius = cv2.minEnclosingCircle(cnt)
            if not (min_radius <= radius <= max_radius):
                continue

            full_cx = int(x1 + cx_local)
            full_cy = int(y1 + cy_local)

            center_offset = abs(full_cx - (w * 0.5)) / (w * 0.12)
            center_score = max(0.0, 1.0 - center_offset)
            expected_r = h * 0.042
            size_score = max(0.0, 1.0 - abs(radius - expected_r) / expected_r)

            confidence = (circularity * 0.50 + center_score * 0.30 + size_score * 0.20) * 100.0
            if confidence > best_score:
                best_score = confidence
                best_circle = (full_cx, full_cy, int(radius))

        if best_circle is None or best_score < 48.0:
            return "NONE", None, round(best_score, 1)

        cx, cy, r = best_circle

        tx1 = max(0, int(cx - r * 0.65))
        tx2 = min(w, int(cx + r * 0.65))
        ty1 = max(0, int(cy + r * 0.18))
        ty2 = min(h, int(cy + r * 0.85))
        text_roi = frame_bgr[ty1:ty2, tx1:tx2]

        if text_roi.size == 0:
            return "NONE", best_circle, round(best_score, 1)

        text_hsv = cv2.cvtColor(text_roi, cv2.COLOR_BGR2HSV)
        white_mask = cv2.inRange(
            text_hsv,
            np.array([0, 0, 195], dtype=np.uint8),
            np.array([180, 65, 255], dtype=np.uint8),
        )

        ys, xs = np.where(white_mask > 0)
        if len(xs) < max(8, int(r * 0.35)):
            return "NONE", best_circle, round(best_score * 0.7, 1)

        text_span_ratio = (xs.max() - xs.min()) / float(max(1, r))
        state = "CAST" if text_span_ratio >= 0.62 else "PULL"
        return state, best_circle, round(min(99.9, best_score), 1)

    def detect_bite(self, frame_bgr: np.ndarray, sensitivity: int) -> tuple[bool, float]:
        """
        Monitors the water region for the floating bobber and detects sudden splash/dip
        activity indicative of a fish bite. Returns (bite_detected, splash_confidence).
        """
        h, w = frame_bgr.shape[:2]
        wx1 = int(w * 0.24)
        wx2 = int(w * 0.76)
        wy1 = int(h * 0.44)
        wy2 = int(h * 0.84)

        water_roi = frame_bgr[wy1:wy2, wx1:wx2]
        if water_roi.size == 0:
            return False, 0.0

        hsv = cv2.cvtColor(water_roi, cv2.COLOR_BGR2HSV)
        white_mask = cv2.inRange(
            hsv,
            np.array([0, 0, 212], dtype=np.uint8),
            np.array([180, 52, 255], dtype=np.uint8),
        )

        if self.bobber_pos is None:
            contours, _ = cv2.findContours(white_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            best_bobber = None
            best_dist = float("inf")
            target_x = (wx2 - wx1) * 0.48
            target_y = (wy2 - wy1) * 0.45

            for cnt in contours:
                area = cv2.contourArea(cnt)
                if 10 <= area <= max(280, int((h / 720.0) ** 2 * 220)):
                    m = cv2.moments(cnt)
                    if m["m00"] > 0:
                        bx = int(m["m10"] / m["m00"])
                        by = int(m["m01"] / m["m00"])
                        dist = math.hypot(bx - target_x, by - target_y)
                        if dist < best_dist:
                            best_dist = dist
                            best_bobber = (bx, by)

            if best_bobber is not None:
                self.bobber_pos = best_bobber
            else:
                self.bobber_pos = (int(target_x), int(target_y))

        bx, by = self.bobber_pos
        patch_r = max(42, int(68 * (h / 720.0)))
        px1 = max(0, bx - patch_r)
        px2 = min(water_roi.shape[1], bx + patch_r)
        py1 = max(0, by - patch_r)
        py2 = min(water_roi.shape[0], by + patch_r)

        patch_gray = cv2.cvtColor(water_roi[py1:py2, px1:px2], cv2.COLOR_BGR2GRAY)
        patch_white = white_mask[py1:py2, px1:px2]
        white_area = float(cv2.countNonZero(patch_white))

        if self.prev_bobber_patch is None or self.prev_bobber_patch.shape != patch_gray.shape:
            self.prev_bobber_patch = patch_gray
            self.baseline_splash_area = max(12.0, white_area)
            return False, 0.0

        if self.calm_frames < 6:
            self.calm_frames += 1
            self.prev_bobber_patch = patch_gray
            self.baseline_splash_area = 0.7 * self.baseline_splash_area + 0.3 * max(12.0, white_area)
            return False, 0.0

        diff = cv2.absdiff(patch_gray, self.prev_bobber_patch)
        _, diff_thresh = cv2.threshold(diff, 28, 255, cv2.THRESH_BINARY)
        motion_ratio = cv2.countNonZero(diff_thresh) / float(max(1, patch_gray.size))
        splash_expansion = white_area / max(15.0, self.baseline_splash_area)

        self.prev_bobber_patch = patch_gray

        norm_sens = max(0.1, min(1.0, sensitivity / 100.0))
        motion_threshold = 0.14 - (norm_sens * 0.095)
        expansion_threshold = 3.2 - (norm_sens * 1.45)

        bite_detected = (motion_ratio >= motion_threshold) or (
            splash_expansion >= expansion_threshold and white_area >= 35.0
        )
        confidence = min(
            99.0,
            max(motion_ratio / motion_threshold, splash_expansion / expansion_threshold) * 75.0,
        )
        return bite_detected, round(confidence, 1)


# ---------------------------------------------------------------------------
# Background Automation Worker Thread
# ---------------------------------------------------------------------------
class FishingWorker(threading.Thread):
    def __init__(self, app_ref: "BloxburgAutoFishApp") -> None:
        super().__init__(daemon=True)
        self.app = app_ref
        self.stop_event = threading.Event()
        self.analyzer = BloxburgVisionAnalyzer()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        missed_frames = 0
        waiting_for_bite = False
        cast_timestamp = 0.0

        with mss.mss() as sct:
            while not self.stop_event.is_set():
                if user32.GetAsyncKeyState(VK_F8) & 0x8000:
                    self.app.trigger_emergency_stop()
                    break

                cfg = self.app.get_current_settings()
                hwnd, rect = find_roblox_window()

                if hwnd is None or rect is None:
                    missed_frames += 1
                    self.app.update_telemetry(
                        status="SEARCHING WINDOW",
                        confidence=0.0,
                        badge_color="#d97706",
                    )
                    if self.stop_event.wait(0.35):
                        break
                    continue

                if cfg["auto_focus"] and not is_window_foreground(hwnd):
                    focus_window(hwnd)
                    if self.stop_event.wait(0.25):
                        break

                left, top, width, height = rect
                monitor = {"left": left, "top": top, "width": width, "height": height}

                try:
                    raw = sct.grab(monitor)
                    frame = np.array(raw, dtype=np.uint8)[:, :, :3]
                except Exception:
                    missed_frames += 1
                    self.app.update_telemetry(
                        status="RECOVERING CAPTURE",
                        confidence=0.0,
                        badge_color="#d97706",
                    )
                    if self.stop_event.wait(0.25):
                        break
                    continue

                ui_state, btn_circle, confidence = self.analyzer.analyze_action_button(frame)

                if ui_state == "NONE" or btn_circle is None:
                    missed_frames += 1
                    if missed_frames <= 12:
                        self.app.update_telemetry(
                            status="RECOVERING UI",
                            confidence=confidence,
                            badge_color="#d97706",
                        )
                    else:
                        self.app.update_telemetry(
                            status="WAITING FOR FISHING UI",
                            confidence=confidence,
                            badge_color="#64748b",
                        )
                    if self.stop_event.wait(0.15):
                        break
                    continue

                missed_frames = 0
                btn_screen_x = left + btn_circle[0]
                btn_screen_y = top + btn_circle[1]

                if ui_state == "CAST":
                    if waiting_for_bite:
                        self.app.increment_catches()
                        waiting_for_bite = False
                        self.analyzer.reset_bite_tracker()
                        self.app.update_telemetry(
                            status="CAUGHT! COOLDOWN",
                            confidence=confidence,
                            badge_color="#16a34a",
                        )
                        if self.stop_event.wait(cfg["reel_cooldown"]):
                            break
                        continue

                    self.app.update_telemetry(
                        status="CASTING LINE",
                        confidence=confidence,
                        badge_color="#2563eb",
                    )
                    if self.stop_event.wait(cfg["cast_delay"]):
                        break
                    if self.stop_event.is_set():
                        break

                    send_left_click(btn_screen_x, btn_screen_y)
                    waiting_for_bite = True
                    cast_timestamp = time.time()
                    self.analyzer.reset_bite_tracker()
                    if self.stop_event.wait(0.45):
                        break

                elif ui_state == "PULL":
                    if not waiting_for_bite:
                        waiting_for_bite = True
                        cast_timestamp = time.time()
                        self.analyzer.reset_bite_tracker()

                    elapsed = time.time() - cast_timestamp
                    bite_detected, splash_conf = self.analyzer.detect_bite(
                        frame, cfg["bite_sensitivity"]
                    )
                    combined_conf = round(max(confidence, splash_conf), 1)

                    if bite_detected:
                        self.app.update_telemetry(
                            status="BITE! REELING IN",
                            confidence=combined_conf,
                            badge_color="#16a34a",
                        )
                        send_left_click(btn_screen_x, btn_screen_y)
                        self.app.increment_catches()
                        waiting_for_bite = False
                        self.analyzer.reset_bite_tracker()
                        if self.stop_event.wait(cfg["reel_cooldown"]):
                            break
                    elif elapsed >= cfg["max_wait_timeout"]:
                        self.app.update_telemetry(
                            status="TIMEOUT RESETTING",
                            confidence=confidence,
                            badge_color="#d97706",
                        )
                        send_left_click(btn_screen_x, btn_screen_y)
                        waiting_for_bite = False
                        self.analyzer.reset_bite_tracker()
                        if self.stop_event.wait(cfg["reel_cooldown"]):
                            break
                    else:
                        self.app.update_telemetry(
                            status=f"WAITING FOR BITE ({elapsed:.1f}s)",
                            confidence=confidence,
                            badge_color="#0284c7",
                        )
                        if self.stop_event.wait(0.06):
                            break


# ---------------------------------------------------------------------------
# Desktop GUI Application
# ---------------------------------------------------------------------------
class BloxburgAutoFishApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.config = load_config()
        self.worker: FishingWorker | None = None
        self.catches_count = 0
        self.session_start_time: float | None = None

        self.root.title("Bloxburg Auto Fish")
        self.root.geometry("410x540")
        self.root.resizable(False, False)
        self.root.configure(bg="#0f172a")
        self.root.attributes("-topmost", self.config["always_on_top"])

        self._setup_styles()
        self._build_ui()
        self._bind_hotkeys()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _setup_styles(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#0f172a")
        style.configure("Card.TFrame", background="#1e293b")
        style.configure(
            "TLabel",
            background="#0f172a",
            foreground="#f8fafc",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Card.TLabel",
            background="#1e293b",
            foreground="#f8fafc",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Muted.TLabel",
            background="#1e293b",
            foreground="#94a3b8",
            font=("Segoe UI", 9),
        )
        style.configure(
            "Header.TLabel",
            background="#0f172a",
            foreground="#38bdf8",
            font=("Segoe UI", 14, "bold"),
        )
        style.configure(
            "TCheckbutton",
            background="#1e293b",
            foreground="#f8fafc",
            font=("Segoe UI", 9),
        )
        style.map("TCheckbutton", background=[("active", "#1e293b")])
        style.configure(
            "Confidence.Horizontal.TProgressbar",
            troughcolor="#0f172a",
            background="#38bdf8",
            bordercolor="#1e293b",
            lightcolor="#38bdf8",
            darkcolor="#38bdf8",
        )

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill="both", expand=True)

        header_frame = ttk.Frame(container)
        header_frame.pack(fill="x", pady=(0, 12))
        ttk.Label(header_frame, text="Bloxburg Auto Fish", style="Header.TLabel").pack(side="left")
        ttk.Label(
            header_frame,
            text="Emergency Stop: F8",
            foreground="#f87171",
            background="#0f172a",
            font=("Segoe UI", 9, "bold"),
        ).pack(side="right", pady=(4, 0))

        status_card = ttk.Frame(container, style="Card.TFrame", padding=14)
        status_card.pack(fill="x", pady=(0, 12))

        status_row = ttk.Frame(status_card, style="Card.TFrame")
        status_row.pack(fill="x", pady=(0, 10))
        ttk.Label(status_row, text="Fishing Status:", style="Muted.TLabel").pack(side="left")
        self.status_badge = tk.Label(
            status_row,
            text="IDLE",
            bg="#475569",
            fg="#ffffff",
            font=("Segoe UI", 9, "bold"),
            padx=10,
            pady=3,
        )
        self.status_badge.pack(side="right")

        catch_row = ttk.Frame(status_card, style="Card.TFrame")
        catch_row.pack(fill="x", pady=(0, 10))
        ttk.Label(catch_row, text="Successful Catches:", style="Muted.TLabel").pack(side="left")
        self.catches_var = tk.StringVar(value="0")
        ttk.Label(
            catch_row,
            textvariable=self.catches_var,
            background="#1e293b",
            foreground="#4ade80",
            font=("Segoe UI", 13, "bold"),
        ).pack(side="right")

        conf_row = ttk.Frame(status_card, style="Card.TFrame")
        conf_row.pack(fill="x", pady=(0, 4))
        ttk.Label(conf_row, text="Detection Confidence:", style="Muted.TLabel").pack(side="left")
        self.conf_var = tk.StringVar(value="0.0%")
        ttk.Label(conf_row, textvariable=self.conf_var, style="Card.TLabel").pack(side="right")

        self.conf_bar = ttk.Progressbar(
            status_card,
            style="Confidence.Horizontal.TProgressbar",
            orient="horizontal",
            mode="determinate",
            maximum=100,
            value=0,
        )
        self.conf_bar.pack(fill="x")

        btn_frame = ttk.Frame(container)
        btn_frame.pack(fill="x", pady=(0, 12))

        self.start_btn = tk.Button(
            btn_frame,
            text="START",
            command=self.start_fishing,
            bg="#16a34a",
            fg="#ffffff",
            activebackground="#15803d",
            activeforeground="#ffffff",
            font=("Segoe UI", 11, "bold"),
            relief="flat",
            cursor="hand2",
            height=2,
        )
        self.start_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.stop_btn = tk.Button(
            btn_frame,
            text="STOP (F8)",
            command=self.stop_fishing,
            bg="#dc2626",
            fg="#ffffff",
            activebackground="#b91c1c",
            activeforeground="#ffffff",
            font=("Segoe UI", 11, "bold"),
            relief="flat",
            cursor="hand2",
            height=2,
            state="disabled",
        )
        self.stop_btn.pack(side="right", fill="x", expand=True, padx=(6, 0))

        settings_card = ttk.Frame(container, style="Card.TFrame", padding=14)
        settings_card.pack(fill="both", expand=True)

        ttk.Label(
            settings_card,
            text="Settings Panel",
            background="#1e293b",
            foreground="#38bdf8",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        sens_header = ttk.Frame(settings_card, style="Card.TFrame")
        sens_header.pack(fill="x")
        ttk.Label(sens_header, text="Bite Splash Sensitivity:", style="Muted.TLabel").pack(side="left")
        self.sens_var = tk.IntVar(value=int(self.config["bite_sensitivity"]))
        self.sens_label_var = tk.StringVar(value=f"{self.sens_var.get()}%")
        ttk.Label(sens_header, textvariable=self.sens_label_var, style="Card.TLabel").pack(side="right")

        sens_slider = tk.Scale(
            settings_card,
            from_=15,
            to=100,
            orient="horizontal",
            variable=self.sens_var,
            command=self._on_settings_change,
            bg="#1e293b",
            fg="#f8fafc",
            troughcolor="#0f172a",
            highlightthickness=0,
            showvalue=False,
        )
        sens_slider.pack(fill="x", pady=(2, 8))

        grid_frame = ttk.Frame(settings_card, style="Card.TFrame")
        grid_frame.pack(fill="x", pady=(0, 8))
        grid_frame.columnconfigure(1, weight=1)

        ttk.Label(grid_frame, text="Cast Delay (s):", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", pady=3
        )
        self.cast_delay_var = tk.StringVar(value=str(self.config["cast_delay"]))
        cast_spin = tk.Spinbox(
            grid_frame,
            from_=0.3,
            to=10.0,
            increment=0.1,
            textvariable=self.cast_delay_var,
            command=self._on_settings_change,
            width=8,
            bg="#0f172a",
            fg="#f8fafc",
            buttonbackground="#334155",
            relief="flat",
        )
        cast_spin.grid(row=0, column=1, sticky="e", pady=3)

        ttk.Label(grid_frame, text="Reel Cooldown (s):", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=3
        )
        self.reel_cd_var = tk.StringVar(value=str(self.config["reel_cooldown"]))
        reel_spin = tk.Spinbox(
            grid_frame,
            from_=0.5,
            to=10.0,
            increment=0.1,
            textvariable=self.reel_cd_var,
            command=self._on_settings_change,
            width=8,
            bg="#0f172a",
            fg="#f8fafc",
            buttonbackground="#334155",
            relief="flat",
        )
        reel_spin.grid(row=1, column=1, sticky="e", pady=3)

        ttk.Label(grid_frame, text="Max Bite Timeout (s):", style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", pady=3
        )
        self.timeout_var = tk.StringVar(value=str(self.config["max_wait_timeout"]))
        timeout_spin = tk.Spinbox(
            grid_frame,
            from_=10.0,
            to=60.0,
            increment=1.0,
            textvariable=self.timeout_var,
            command=self._on_settings_change,
            width=8,
            bg="#0f172a",
            fg="#f8fafc",
            buttonbackground="#334155",
            relief="flat",
        )
        timeout_spin.grid(row=2, column=1, sticky="e", pady=3)

        self.auto_focus_var = tk.BooleanVar(value=bool(self.config["auto_focus"]))
        ttk.Checkbutton(
            settings_card,
            text="Auto-focus Roblox window when active",
            variable=self.auto_focus_var,
            command=self._on_settings_change,
        ).pack(anchor="w", pady=(2, 2))

        self.topmost_var = tk.BooleanVar(value=bool(self.config["always_on_top"]))
        ttk.Checkbutton(
            settings_card,
            text="Keep controller window always on top",
            variable=self.topmost_var,
            command=self._on_settings_change,
        ).pack(anchor="w", pady=(2, 0))

    def _bind_hotkeys(self) -> None:
        self.root.bind("<F8>", lambda _e: self.trigger_emergency_stop())

    def _on_settings_change(self, *_args) -> None:
        self.sens_label_var.set(f"{self.sens_var.get()}%")
        self.root.attributes("-topmost", self.topmost_var.get())
        save_config(self.get_current_settings())

    def get_current_settings(self) -> dict:
        def safe_float(val: str, default: float, low: float, high: float) -> float:
            try:
                parsed = float(val)
                return max(low, min(high, parsed))
            except ValueError:
                return default

        return {
            "bite_sensitivity": int(self.sens_var.get()),
            "cast_delay": safe_float(self.cast_delay_var.get(), 1.2, 0.2, 15.0),
            "reel_cooldown": safe_float(self.reel_cd_var.get(), 1.6, 0.3, 15.0),
            "max_wait_timeout": safe_float(self.timeout_var.get(), 28.0, 8.0, 90.0),
            "auto_focus": bool(self.auto_focus_var.get()),
            "always_on_top": bool(self.topmost_var.get()),
        }

    def start_fishing(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        save_config(self.get_current_settings())
        self.session_start_time = time.time()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.update_telemetry("STARTING...", 0.0, "#2563eb")

        self.worker = FishingWorker(self)
        self.worker.start()

    def stop_fishing(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.update_telemetry("STOPPED", 0.0, "#475569")

    def trigger_emergency_stop(self) -> None:
        self.root.after(0, self._apply_emergency_stop)

    def _apply_emergency_stop(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_badge.configure(text="EMERGENCY STOPPED", bg="#dc2626")
        self.conf_var.set("0.0%")
        self.conf_bar["value"] = 0

    def increment_catches(self) -> None:
        self.catches_count += 1
        self.root.after(0, lambda: self.catches_var.set(str(self.catches_count)))

    def update_telemetry(self, status: str, confidence: float, badge_color: str) -> None:
        def _apply() -> None:
            if self.worker is None and status not in ("STOPPED", "EMERGENCY STOPPED"):
                return
            self.status_badge.configure(text=status, bg=badge_color)
            self.conf_var.set(f"{confidence:.1f}%")
            self.conf_bar["value"] = confidence

        self.root.after(0, _apply)

    def on_close(self) -> None:
        if self.worker is not None:
            self.worker.stop()
        save_config(self.get_current_settings())
        self.root.destroy()


def main() -> None:
    enable_dpi_awareness()
    root = tk.Tk()
    BloxburgAutoFishApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
