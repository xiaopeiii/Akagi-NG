from __future__ import annotations

import ctypes
import math
import os
import queue
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from akagi_ng.core.paths import ensure_dir, get_settings_dir
from akagi_ng.electron_client.logger import logger


try:
    from playwright.sync_api import BrowserContext, Page, sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None  # type: ignore[assignment]
    BrowserContext = object  # type: ignore[misc,assignment]
    Page = object  # type: ignore[misc,assignment]


@dataclass(frozen=True)
class Viewport:
    width: int
    height: int


class PlaywrightBrowser:
    """
    Owns a Chromium instance (Playwright sync API) in a dedicated thread.

    We keep all Playwright interactions in the browser thread and expose a small
    action queue API for mouse actions + JS evaluation.
    """

    def __init__(self, viewport: Viewport, profile_dir: Path | None = None):
        self.viewport = viewport
        self.profile_dir = profile_dir or self._default_profile_dir()

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._q: queue.Queue[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any], queue.Queue[Any] | None]] = (
            queue.Queue()
        )

        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._ready = threading.Event()

        # Cache for real-mouse hwnd resolution.
        self._real_mouse_hwnd: int | None = None

    def _default_profile_dir(self) -> Path:
        base = ensure_dir(get_settings_dir() / "playwright_data")
        pid = os.getpid()
        d = base / f"instance_{pid}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, url: str, proxy_server: str | None = None) -> None:
        if sync_playwright is None:
            raise RuntimeError("playwright is not available. Install it in akagi_backend.")
        if self.is_running():
            return
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(url, proxy_server),
            name="AutoPlayBrowser",
            daemon=True,
        )
        self._thread.start()
        # Best-effort: wait a bit for browser readiness.
        self._ready.wait(timeout=20.0)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            try:
                self._enqueue(lambda: None, block=False)
            except Exception:
                pass
            self._thread.join(timeout=10.0)
        self._thread = None

    def _run(self, url: str, proxy_server: str | None) -> None:
        try:
            proxy = {"server": proxy_server} if proxy_server else None
            with sync_playwright() as p:
                chromium = p.chromium
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                self._ctx = chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    headless=False,
                    proxy=proxy,
                    viewport={"width": int(self.viewport.width), "height": int(self.viewport.height)},
                    args=["--noerrdialogs", "--no-sandbox"],
                )
                pages = self._ctx.pages
                self._page = pages[0] if pages else self._ctx.new_page()
                self._page.goto(url, wait_until="domcontentloaded")
                self._ready.set()

                while not self._stop.is_set():
                    try:
                        fn, args, kwargs, retq = self._q.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    try:
                        out = fn(*args, **kwargs)
                        if retq is not None:
                            retq.put(out)
                    except Exception as e:
                        if retq is not None:
                            retq.put(e)
        except Exception as e:
            logger.error(f"[autoplay] Browser thread crashed: {e}")
        finally:
            try:
                if self._ctx:
                    self._ctx.close()
            except Exception:
                pass
            self._ctx = None
            self._page = None

    def _enqueue(self, fn: Callable[..., Any], *args: Any, block: bool = True, **kwargs: Any) -> Any:
        if not self.is_running():
            raise RuntimeError("Browser is not running")
        retq: queue.Queue[Any] | None = queue.Queue(maxsize=1) if block else None
        self._q.put((fn, args, kwargs, retq))
        if not block:
            return None
        out = retq.get(timeout=10.0)  # type: ignore[union-attr]
        if isinstance(out, Exception):
            raise out
        return out

    # ---------------------------
    # Playwright mouse primitives

    def mouse_move(self, x: float, y: float, steps: int = 5) -> None:
        def _do(px: float, py: float, st: int) -> None:
            assert self._page is not None
            self._page.mouse.move(px, py, steps=st)

        self._enqueue(_do, float(x), float(y), int(steps), block=True)

    def mouse_down(self) -> None:
        def _do() -> None:
            assert self._page is not None
            self._page.mouse.down()

        self._enqueue(_do, block=True)

    def mouse_up(self) -> None:
        def _do() -> None:
            assert self._page is not None
            self._page.mouse.up()

        self._enqueue(_do, block=True)

    def mouse_wheel(self, dx: float, dy: float) -> None:
        def _do(pdx: float, pdy: float) -> None:
            assert self._page is not None
            self._page.mouse.wheel(delta_x=pdx, delta_y=pdy)

        self._enqueue(_do, float(dx), float(dy), block=True)

    def eval_js(self, expression: str) -> Any:
        def _do(expr: str) -> Any:
            assert self._page is not None
            return self._page.evaluate(expr)

        return self._enqueue(_do, str(expression), block=True)

    # ---------------------------
    # Real mouse support (Windows)

    def _read_window_geometry(self) -> dict[str, Any] | None:
        try:
            return self.eval_js(
                "(() => ({"
                "screenX: window.screenX, screenY: window.screenY,"
                "outerWidth: window.outerWidth, outerHeight: window.outerHeight,"
                "innerWidth: window.innerWidth, innerHeight: window.innerHeight,"
                "dpr: window.devicePixelRatio || 1"
                "}))()"
            )
        except Exception:
            return None

    def _get_hwnd_class_name(self, hwnd: int) -> str:
        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd):
            return ""
        cls_buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls_buf, 64)
        return cls_buf.value

    def _is_chrome_hwnd(self, hwnd: int) -> bool:
        return self._get_hwnd_class_name(hwnd).startswith("Chrome_WidgetWin")

    def _get_hwnd_metrics(self, hwnd: int) -> dict[str, float] | None:
        if os.name != "nt" or not hwnd:
            return None

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return None

        win_rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(win_rect)):
            return None

        client_rect = RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(client_rect)):
            return None
        client_w = client_rect.right - client_rect.left
        client_h = client_rect.bottom - client_rect.top
        if client_w <= 0 or client_h <= 0:
            return None

        client_origin = POINT(0, 0)
        if not user32.ClientToScreen(hwnd, ctypes.byref(client_origin)):
            return None

        return {
            "win_left": float(win_rect.left),
            "win_top": float(win_rect.top),
            "win_w": float(win_rect.right - win_rect.left),
            "win_h": float(win_rect.bottom - win_rect.top),
            "client_left": float(client_origin.x),
            "client_top": float(client_origin.y),
            "client_w": float(client_w),
            "client_h": float(client_h),
        }

    def _hwnd_match_score(self, hwnd: int, js_info: dict[str, Any]) -> float | None:
        metrics = self._get_hwnd_metrics(hwnd)
        if metrics is None:
            return None
        try:
            screen_x = float(js_info.get("screenX", 0) or 0)
            screen_y = float(js_info.get("screenY", 0) or 0)
            outer_w = float(js_info.get("outerWidth", 0) or 0)
            outer_h = float(js_info.get("outerHeight", 0) or 0)
            inner_w = float(js_info.get("innerWidth", 0) or 0)
            inner_h = float(js_info.get("innerHeight", 0) or 0)
            dpr = float(js_info.get("dpr", 1.0) or 1.0)
        except Exception:
            return None
        if outer_w <= 0 or outer_h <= 0 or inner_w <= 0 or inner_h <= 0:
            return None

        scales = [1.0]
        if abs(dpr - 1.0) > 0.01:
            scales.append(dpr)
        scales.append(max(0.5, min(4.0, metrics["win_w"] / outer_w)))
        scales.append(max(0.5, min(4.0, metrics["client_w"] / inner_w)))

        best: float | None = None
        for scale in scales:
            score = (
                abs(metrics["win_left"] - screen_x * scale)
                + abs(metrics["win_top"] - screen_y * scale)
                + abs(metrics["win_w"] - outer_w * scale)
                + abs(metrics["win_h"] - outer_h * scale)
                + abs(metrics["client_w"] - inner_w * scale)
                + abs(metrics["client_h"] - inner_h * scale)
            )
            if best is None or score < best:
                best = score
        return best

    def _find_matching_chrome_hwnd(self, js_info: dict[str, Any]) -> int | None:
        if os.name != "nt":
            return None
        user32 = ctypes.windll.user32
        hwnd_scores: list[tuple[float, int]] = []
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _enum_cb(hwnd, _lparam):
            hwnd = int(hwnd)
            if not self._is_chrome_hwnd(hwnd):
                return True
            score = self._hwnd_match_score(hwnd, js_info)
            if score is not None:
                hwnd_scores.append((float(score), hwnd))
            return True

        cb = enum_proc(_enum_cb)
        user32.EnumWindows(cb, 0)
        if not hwnd_scores:
            return None
        hwnd_scores.sort(key=lambda item: item[0])
        return hwnd_scores[0][1]

    def _get_client_origin_and_scale(self, hwnd: int, js_info: dict[str, Any]) -> tuple[float, float, float, float] | None:
        if os.name != "nt":
            return None

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        user32 = ctypes.windll.user32
        h = ctypes.c_void_p(hwnd)

        rect = RECT()
        if not user32.GetClientRect(h, ctypes.byref(rect)):
            return None
        client_w = rect.right - rect.left
        client_h = rect.bottom - rect.top
        if client_w <= 0 or client_h <= 0:
            return None

        pt = POINT(0, 0)
        if not user32.ClientToScreen(h, ctypes.byref(pt)):
            return None

        inner_w = float(js_info.get("innerWidth", 0) or 0)
        inner_h = float(js_info.get("innerHeight", 0) or 0)
        dpr = float(js_info.get("dpr", 1.0) or 1.0)
        scale_x = (client_w / inner_w) if inner_w > 0 else dpr
        scale_y = (client_h / inner_h) if inner_h > 0 else dpr
        return (float(pt.x), float(pt.y), float(scale_x), float(scale_y))

    def _get_viewport_geometry(self) -> tuple[float, float, float, float] | None:
        info = self._read_window_geometry()
        if info is None:
            return None

        hwnd = None
        if self._real_mouse_hwnd:
            score = self._hwnd_match_score(self._real_mouse_hwnd, info)
            if score is not None and score < 2200:
                hwnd = self._real_mouse_hwnd

        if hwnd is None:
            hwnd = self._find_matching_chrome_hwnd(info)
            self._real_mouse_hwnd = hwnd

        if hwnd is not None:
            mapped = self._get_client_origin_and_scale(hwnd, info)
            if mapped is not None:
                return mapped

        # Fallback estimate based on JS metrics.
        border_x = (float(info["outerWidth"]) - float(info["innerWidth"])) / 2.0
        title_bar_y = float(info["outerHeight"]) - float(info["innerHeight"]) - border_x
        origin_x_css = float(info["screenX"]) + border_x
        origin_y_css = float(info["screenY"]) + title_bar_y
        dpr = float(info.get("dpr", 1.0) or 1.0)
        return (origin_x_css * dpr, origin_y_css * dpr, dpr, dpr)

    def viewport_to_screen(self, viewport_x: float, viewport_y: float) -> tuple[int, int]:
        geom = self._get_viewport_geometry()
        if geom is None:
            return (int(viewport_x), int(viewport_y))
        origin_x, origin_y, scale_x, scale_y = geom
        sx = round(origin_x + viewport_x * scale_x)
        sy = round(origin_y + viewport_y * scale_y)
        return (int(sx), int(sy))

    def _get_cursor_pos(self) -> tuple[int, int]:
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return int(pt.x), int(pt.y)

    def real_mouse_move(self, screen_x: int, screen_y: int, speed_pps: float = 2200.0, jitter_px: float = 2.0) -> None:
        user32 = ctypes.windll.user32
        start_x, start_y = self._get_cursor_pos()
        dx = int(screen_x) - start_x
        dy = int(screen_y) - start_y
        dist = math.hypot(dx, dy)
        speed_pps = max(300.0, float(speed_pps))
        jitter_px = max(0.0, float(jitter_px))
        if dist < 1.0:
            user32.SetCursorPos(int(screen_x), int(screen_y))
            return

        move_sec = max(0.06, min(0.45, dist / speed_pps + random.uniform(0.0, 0.03)))
        nx = dx / dist
        ny = dy / dist
        px = -ny
        py = nx
        bend = min(22.0, max(4.0, dist * 0.06)) * random.choice((-1.0, 1.0))
        mid_t = random.uniform(0.35, 0.65)
        cx = start_x + dx * mid_t + px * bend
        cy = start_y + dy * mid_t + py * bend

        steps = int(max(14, min(80, move_sec / 0.008)))
        start_ts = time.perf_counter()
        for i in range(1, steps + 1):
            t = i / steps
            eased = 3 * t * t - 2 * t * t * t
            omt = 1.0 - eased
            bx = (omt * omt * start_x) + (2 * omt * eased * cx) + (eased * eased * int(screen_x))
            by = (omt * omt * start_y) + (2 * omt * eased * cy) + (eased * eased * int(screen_y))

            if i < steps:
                jitter = jitter_px * (1.0 - eased)
                bx += random.uniform(-jitter, jitter)
                by += random.uniform(-jitter, jitter)

            user32.SetCursorPos(int(round(bx)), int(round(by)))
            expected = start_ts + move_sec * t
            sleep_sec = expected - time.perf_counter()
            if sleep_sec > 0:
                time.sleep(sleep_sec)

        user32.SetCursorPos(int(screen_x), int(screen_y))

    def real_mouse_down(self) -> None:
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN

    def real_mouse_up(self) -> None:
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP

    def real_mouse_click(self, delay_sec: float = 0.08) -> None:
        self.real_mouse_down()
        time.sleep(max(0.01, float(delay_sec)))
        self.real_mouse_up()

