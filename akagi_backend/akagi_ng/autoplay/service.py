from __future__ import annotations

import random
import time
from dataclasses import dataclass
from functools import cmp_to_key

from akagi_ng.autoplay.browser import PlaywrightBrowser, Viewport
from akagi_ng.autoplay.positions import (
    Positions,
    candidate_kan_pos_index,
    candidate_pos_index,
)
from akagi_ng.bridge.majsoul.tile_mapping import compare_pai
from akagi_ng.core.constants import Platform
from akagi_ng.core.logging import logger


@dataclass(frozen=True)
class AutoPlayRuntimeConfig:
    enabled: bool
    mode: str  # "playwright" | "real_mouse"
    auto_launch_browser: bool
    viewport_width: int
    viewport_height: int
    think_delay_ms: int
    real_mouse_speed_pps: float
    real_mouse_jitter_px: float


_ACTION_PRIORITY: dict[str, int] = {
    # smaller = higher priority (more left / more top)
    "zimo": 1,
    "rong": 1,
    "hora": 1,  # resolved to zimo/rong
    "reach": 2,
    "daiminkan": 2,
    "ryukyoku": 5,
    "chi": 4,
    "pon": 3,
    "kan": 3,  # unified button for ankan/kakan
    "ankan": 3,
    "kakan": 3,
    "nukidora": 4,
    "none": 99,
}

# Timing tuning (ms) for Majsoul UI reliability. These defaults are intentionally
# conservative to reduce mis-click / ignored-click issues when driving the UI.
_DISCARD_PRE_DELAY_MS = 1000
_DISCARD_DOUBLE_CLICK_GAP_MS = 150
_BUTTON_PRE_DELAY_MS = 450
_BUTTON_POST_DELAY_MS = 350


class AutoPlayService:
    def __init__(self, config: AutoPlayRuntimeConfig, game_url: str, platform: Platform, proxy_server: str | None):
        self.config = config
        self.game_url = game_url
        self.platform = platform
        self.proxy_server = proxy_server

        self._browser = PlaywrightBrowser(viewport=Viewport(config.viewport_width, config.viewport_height))

    def start(self) -> None:
        if not self.config.enabled:
            return
        if self.platform not in (Platform.MAJSOUL, Platform.AUTO):
            logger.warning(f"[autoplay] platform={self.platform} not supported, disabling autoplay.")
            return
        if self.config.auto_launch_browser:
            logger.info("[autoplay] starting Playwright browser...")
            self._browser.start(self.game_url, proxy_server=self.proxy_server)

    def stop(self) -> None:
        try:
            self._browser.stop()
        except Exception:
            pass

    def _should_click_pass(self, bot) -> bool:
        """Return True when a visible Pass/None button is expected on screen."""
        return len(self._available_button_actions(bot)) > 1

    def plan_steps(self, action: dict, bot) -> list[dict[str, object]]:
        """
        Convert a MJAI action + current bot state into a list of UI steps in 16x9 normalized coordinates.

        The Electron client will map (x16,y9) to actual viewport pixels and dispatch input events.
        """
        if not self.config.enabled:
            return []
        if self.platform not in (Platform.MAJSOUL, Platform.AUTO):
            return []
        if not action or not isinstance(action, dict):
            return []

        a_type = action.get("type")
        if a_type is None:
            return []

        a_type = str(a_type)
        if a_type == "none" and not self._should_click_pass(bot):
            # Most events produce {"type":"none"} while no UI buttons are present. Don't spam clicks.
            return []

        steps: list[dict[str, object]] = []
        if self.config.think_delay_ms > 0:
            steps.append({"op": "delay", "ms": int(self.config.think_delay_ms)})

        def _delay(ms: int) -> None:
            steps.append({"op": "delay", "ms": int(ms)})

        def _click(x16: float, y9: float) -> None:
            steps.append({"op": "click", "x16": float(x16), "y9": float(y9)})

        def _move(x16: float, y9: float) -> None:
            steps.append({"op": "move", "x16": float(x16), "y9": float(y9)})

        def _double_click(x16: float, y9: float) -> None:
            _click(x16, y9)
            _delay(_DISCARD_DOUBLE_CLICK_GAP_MS)
            _click(x16, y9)

        def _center() -> None:
            cx16, cy9 = Positions.CENTER
            _move(cx16, cy9)

        # 1) Discard
        if a_type == "dahai":
            pai = str(action.get("pai"))
            tsumogiri = action.get("tsumogiri")
            x16, y9 = self._discard_pos(bot, pai, tsumogiri if isinstance(tsumogiri, bool) else None)
            _delay(_DISCARD_PRE_DELAY_MS)
            _double_click(x16, y9)
            _center()
            return steps

        # 2) Reach: click reach then discard
        if a_type == "reach":
            _delay(_BUTTON_PRE_DELAY_MS)
            slot = self._button_slot_for_action(bot, "reach")
            x16, y9 = Positions.BUTTONS[slot]
            _click(x16, y9)
            _center()
            return steps

        # 3) Agari
        if a_type == "hora":
            _delay(_BUTTON_PRE_DELAY_MS)
            resolved = self._resolve_agari_action(bot, "hora")
            slot = self._button_slot_for_action(bot, resolved)
            x16, y9 = Positions.BUTTONS[slot]
            _click(x16, y9)
            _center()
            return steps

        # 4) Pass/None (only when UI buttons exist)
        if a_type == "none":
            _delay(_BUTTON_PRE_DELAY_MS)
            slot = 0
            x16, y9 = Positions.BUTTONS[slot]
            _click(x16, y9)
            _center()
            return steps

        # 5) Button actions (chi/pon/kan/...)
        _delay(_BUTTON_PRE_DELAY_MS)
        slot = self._button_slot_for_action(bot, a_type)
        bx16, by9 = Positions.BUTTONS[slot]
        _click(bx16, by9)

        if a_type in ("chi", "pon", "daiminkan"):
            _delay(_BUTTON_POST_DELAY_MS)
            find_fn = getattr(bot, f"find_{a_type}_candidates", None)
            candidates = find_fn() if callable(find_fn) else []
            idx = self._select_candidate_index(candidates, action)
            n = max(1, len(candidates))
            pos_idx = candidate_pos_index(n, idx)
            pos_idx = max(0, min(pos_idx, len(Positions.CANDIDATES) - 1))
            cx16, cy9 = Positions.CANDIDATES[pos_idx]
            _click(cx16, cy9)
        elif a_type in ("ankan", "kakan"):
            _delay(_BUTTON_POST_DELAY_MS)
            cand_fn = getattr(bot, f"find_{a_type}_candidates", None)
            candidates = cand_fn() if callable(cand_fn) else []
            idx = self._select_candidate_index(candidates, action)
            n = max(1, len(candidates))
            pos_idx = candidate_kan_pos_index(n, idx)
            pos_idx = max(0, min(pos_idx, len(Positions.CANDIDATES_KAN) - 1))
            cx16, cy9 = Positions.CANDIDATES_KAN[pos_idx]
            _click(cx16, cy9)

        _center()
        return steps

    def _to_viewport_px(self, x16: float, y9: float) -> tuple[float, float]:
        x = (x16 / 16.0) * float(self.config.viewport_width)
        y = (y9 / 9.0) * float(self.config.viewport_height)
        return x, y

    def _move_click(self, x16: float, y9: float) -> None:
        # light jitter to avoid robotic clicks
        jitter16 = 0.02
        x16 = float(x16) + random.uniform(-jitter16, jitter16)
        y9 = float(y9) + random.uniform(-jitter16, jitter16)
        x, y = self._to_viewport_px(x16, y9)

        if self.config.mode == "real_mouse":
            sx, sy = self._browser.viewport_to_screen(x, y)
            self._browser.real_mouse_move(
                sx,
                sy,
                speed_pps=self.config.real_mouse_speed_pps,
                jitter_px=self.config.real_mouse_jitter_px,
            )
            self._browser.real_mouse_click()
            # Also sync Playwright mouse hover state so canvas reacts consistently.
            self._browser.mouse_move(x, y, steps=4)
        else:
            self._browser.mouse_move(x, y, steps=6)
            self._browser.mouse_down()
            time.sleep(0.06)
            self._browser.mouse_up()

    def _move_center(self) -> None:
        x16, y9 = Positions.CENTER
        x, y = self._to_viewport_px(x16, y9)
        if self.config.mode == "real_mouse":
            sx, sy = self._browser.viewport_to_screen(x, y)
            self._browser.real_mouse_move(
                sx,
                sy,
                speed_pps=self.config.real_mouse_speed_pps,
                jitter_px=self.config.real_mouse_jitter_px,
            )
            self._browser.mouse_move(x, y, steps=2)
        else:
            self._browser.mouse_move(x, y, steps=4)

    def _resolve_agari_action(self, bot, action_type: str) -> str:
        if action_type != "hora":
            return action_type
        # Prefer explicit flags from mjai.Bot.
        if getattr(bot, "can_tsumo_agari", False):
            return "zimo"
        if getattr(bot, "can_ron_agari", False):
            return "rong"

        # Fallback: When it's self-draw we usually can discard; when it's ron we can't.
        can_discard = bool(getattr(bot, "can_discard", False))
        return "zimo" if can_discard else "rong"

    def _available_button_actions(self, bot) -> list[str]:
        actions: list[str] = ["none"]
        if getattr(bot, "can_chi", False):
            actions.append("chi")
        if getattr(bot, "can_pon", False):
            actions.append("pon")
        if getattr(bot, "can_daiminkan", False):
            actions.append("daiminkan")
        # Majsoul shows ankan/kakan under a unified Kan button.
        if getattr(bot, "can_ankan", False) or getattr(bot, "can_kakan", False):
            actions.append("kan")
        # mjai.Bot uses can_riichi (not can_reach).
        if getattr(bot, "can_riichi", False):
            actions.append("reach")
        if getattr(bot, "can_agari", False) or getattr(bot, "can_tsumo_agari", False) or getattr(bot, "can_ron_agari", False):
            # Majsoul shows either Zimo (tsumo) or Rong (ron). We resolve to the one likely on screen.
            actions.append(self._resolve_agari_action(bot, "hora"))
        if getattr(bot, "can_ryukyoku", False):
            actions.append("ryukyoku")
        if getattr(bot, "can_nukidora", False):
            actions.append("nukidora")
        return actions

    def _button_slot_for_action(self, bot, action_type: str) -> int:
        action_type = self._resolve_agari_action(bot, action_type)
        available = self._available_button_actions(bot)
        # Merge: "ankan"/"kakan" click the unified "kan" button.
        if action_type in ("ankan", "kakan"):
            action_type = "kan"
        # Ensure pass exists.
        if "none" not in available:
            available.append("none")

        # Sort by priority (excluding discard).
        sorted_actions = sorted(
            available,
            key=lambda a: (_ACTION_PRIORITY.get(a, 50), a),
        )
        # Pass must always be slot 0.
        if "none" in sorted_actions:
            sorted_actions.remove("none")
        # Fill slots 1.. with actions.
        slot_map = {"none": 0}
        for i, act in enumerate(sorted_actions, start=1):
            if i >= len(Positions.BUTTONS):
                break
            slot_map[act] = i
        # Resolve final mapping.
        return slot_map.get(action_type, 0)

    def _sorted_hand_wo_tsumo(self, bot) -> tuple[list[str], str | None]:
        tiles = list(getattr(bot, "tehai_mjai", []) or [])
        last_tsumo = getattr(bot, "last_self_tsumo", None)
        if last_tsumo in tiles:
            tiles.remove(last_tsumo)
        tiles.sort(key=cmp_to_key(compare_pai))
        return tiles, last_tsumo

    def _discard_pos(self, bot, pai: str, tsumogiri: bool | None) -> tuple[float, float]:
        tiles, last_tsumo = self._sorted_hand_wo_tsumo(bot)
        is_tsumo = False
        if tsumogiri is True:
            is_tsumo = True
        elif tsumogiri is False:
            is_tsumo = False
        else:
            is_tsumo = (pai == last_tsumo) if last_tsumo else False

        if is_tsumo:
            idx = len([t for t in tiles if t != "?"])
            idx = max(0, min(idx, len(Positions.TEHAI_X) - 1))
            x16 = Positions.TEHAI_X[idx] + Positions.TSUMO_GAP
            y9 = Positions.TEHAI_Y
            return x16, y9

        # tedashi: discard from sorted hand (excluding tsumohai)
        try:
            idx = tiles.index(pai)
        except ValueError:
            # fallback: base tile match (ignore red)
            base = pai.replace("r", "")
            idx = next((i for i, t in enumerate(tiles) if t.replace("r", "") == base), 0)
        idx = max(0, min(idx, len(Positions.TEHAI_X) - 1))
        return Positions.TEHAI_X[idx], Positions.TEHAI_Y

    def _select_candidate_index(self, candidates: list[dict], action: dict) -> int:
        if not candidates:
            return 0
        target_consumed = action.get("consumed") or []
        target_pai = action.get("pai")

        def _norm_tiles(ts: list[str]) -> list[str]:
            return sorted([t.replace("r", "") for t in ts])

        for i, cand in enumerate(candidates):
            ev = cand.get("event") if isinstance(cand, dict) else None
            if not isinstance(ev, dict):
                continue
            if target_pai and ev.get("pai") and ev.get("pai") != target_pai:
                continue
            if target_consumed and ev.get("consumed"):
                if _norm_tiles(list(ev.get("consumed") or [])) == _norm_tiles(list(target_consumed)):
                    return i
        return 0

    def handle_action(self, action: dict, bot) -> None:
        """
        Execute a MJAI action on the Majsoul web UI.

        This is best-effort UI automation. If the UI is out-of-sync (wrong zoom, window resized,
        or you are not in-game), actions may fail.
        """
        if not self.config.enabled:
            return
        if self.platform not in (Platform.MAJSOUL, Platform.AUTO):
            return
        if not action or not isinstance(action, dict):
            return
        if action.get("type") is None:
            return
        if not self._browser.is_running():
            # Autoplay may be enabled but browser not launched (manual start).
            return

        # think delay (avoid instant robotic play)
        if self.config.think_delay_ms > 0:
            time.sleep(self.config.think_delay_ms / 1000.0)

        a_type = str(action.get("type"))

        # 1) Discard
        if a_type == "dahai":
            pai = str(action.get("pai"))
            tsumogiri = action.get("tsumogiri")
            x16, y9 = self._discard_pos(bot, pai, tsumogiri if isinstance(tsumogiri, bool) else None)
            time.sleep(_DISCARD_PRE_DELAY_MS / 1000.0)
            self._move_click(x16, y9)
            time.sleep(_DISCARD_DOUBLE_CLICK_GAP_MS / 1000.0)
            self._move_click(x16, y9)
            self._move_center()
            return

        # 2) Reach: click reach button then discard tile
        if a_type == "reach":
            slot = self._button_slot_for_action(bot, "reach")
            x16, y9 = Positions.BUTTONS[slot]
            time.sleep(_BUTTON_PRE_DELAY_MS / 1000.0)
            self._move_click(x16, y9)
            self._move_center()
            return

        # 3) Agari
        if a_type == "hora":
            resolved = self._resolve_agari_action(bot, "hora")
            slot = self._button_slot_for_action(bot, resolved)
            x16, y9 = Positions.BUTTONS[slot]
            time.sleep(_BUTTON_PRE_DELAY_MS / 1000.0)
            self._move_click(x16, y9)
            self._move_center()
            return

        # 4) Pass (only if UI buttons exist)
        if a_type == "none":
            if not self._should_click_pass(bot):
                return
            slot = 0
            x16, y9 = Positions.BUTTONS[slot]
            time.sleep(_BUTTON_PRE_DELAY_MS / 1000.0)
            self._move_click(x16, y9)
            self._move_center()
            return

        # 5) Button actions (chi/pon/kan/...)
        slot = self._button_slot_for_action(bot, a_type)
        x16, y9 = Positions.BUTTONS[slot]
        time.sleep(_BUTTON_PRE_DELAY_MS / 1000.0)
        self._move_click(x16, y9)

        # candidate selection if needed
        if a_type in ("chi", "pon", "daiminkan"):
            time.sleep(_BUTTON_POST_DELAY_MS / 1000.0)
            find_fn = getattr(bot, f"find_{a_type}_candidates", None)
            candidates = find_fn() if callable(find_fn) else []
            idx = self._select_candidate_index(candidates, action)
            n = max(1, len(candidates))
            pos_idx = candidate_pos_index(n, idx)
            pos_idx = max(0, min(pos_idx, len(Positions.CANDIDATES) - 1))
            cx16, cy9 = Positions.CANDIDATES[pos_idx]
            self._move_click(cx16, cy9)
        elif a_type in ("ankan", "kakan"):
            # unified kan button already clicked; choose candidate from bot helpers
            time.sleep(_BUTTON_POST_DELAY_MS / 1000.0)
            cand_fn = getattr(bot, f"find_{a_type}_candidates", None)
            candidates = cand_fn() if callable(cand_fn) else []
            idx = self._select_candidate_index(candidates, action)
            n = max(1, len(candidates))
            pos_idx = candidate_kan_pos_index(n, idx)
            pos_idx = max(0, min(pos_idx, len(Positions.CANDIDATES_KAN) - 1))
            cx16, cy9 = Positions.CANDIDATES_KAN[pos_idx]
            self._move_click(cx16, cy9)

        self._move_center()
