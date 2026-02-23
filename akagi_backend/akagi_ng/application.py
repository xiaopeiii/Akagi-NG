from __future__ import annotations

import queue
import signal
import threading
import time
from dataclasses import dataclass

from akagi_ng.core import AppContext, NotificationHandler, get_app_context, set_app_context
from akagi_ng.core.constants import ServerConstants
from akagi_ng.core.logging import configure_logging, logger
from akagi_ng.dataserver import DataServer
from akagi_ng.dataserver.adapter import build_dataserver_payload
from akagi_ng.mitm_client import MitmClient
from akagi_ng.mjai_bot import Controller, StateTrackerBot
from akagi_ng.settings import local_settings as loaded_settings

logger = logger.bind(module="akagi")


@dataclass
class _PendingAutoplay:
    steps: list[dict[str, object]]
    activity_seq: int
    retry_at: float
    retried: bool = False


class AkagiApp:
    def __init__(self):
        self._stop_event = threading.Event()
        self.ds: DataServer | None = None
        self.frontend_url = ""
        self.message_queue: queue.Queue[dict] = queue.Queue(maxsize=ServerConstants.MESSAGE_QUEUE_MAXSIZE)
        self._autoplay_seq = 0
        self._game_activity_seq = 0
        self._pending_autoplay: _PendingAutoplay | None = None

    def initialize(self):
        import importlib

        from akagi_ng import AKAGI_VERSION
        from akagi_ng.electron_client import create_electron_client

        logger.info(f"Starting Akagi-NG {AKAGI_VERSION}...")

        settings = loaded_settings
        configure_logging(settings.log_level)

        host, port = settings.server.host, settings.server.port
        self.ds = DataServer(host=host, external_port=port)

        target_host = "127.0.0.1" if host == "0.0.0.0" else host
        self.frontend_url = f"http://{target_host}:{port}/"

        mjai_bot, mjai_controller = None, None
        try:
            importlib.import_module("akagi_ng.core.lib_loader")
            from akagi_ng.mjai_bot import Controller, StateTrackerBot

            mjai_bot, mjai_controller = StateTrackerBot(), Controller()
            logger.info("Bot components loaded successfully.")
        except ImportError as e:
            logger.error(f"Failed to load bot components or native library: {e}")

        autoplay_service = None
        try:
            if getattr(settings, "autoplay", None) and settings.autoplay.enabled:
                from akagi_ng.autoplay import AutoPlayService
                from akagi_ng.autoplay.service import AutoPlayRuntimeConfig

                proxy = None
                if settings.autoplay.auto_launch_browser:
                    if settings.mitm.enabled:
                        proxy = f"http://{settings.mitm.host}:{settings.mitm.port}"
                    else:
                        logger.warning(
                            "[autoplay] auto_launch_browser=true requires mitm.enabled=true (for message interception)."
                        )

                autoplay_service = AutoPlayService(
                    config=AutoPlayRuntimeConfig(
                        enabled=settings.autoplay.enabled,
                        mode=settings.autoplay.mode,
                        auto_launch_browser=settings.autoplay.auto_launch_browser,
                        viewport_width=settings.autoplay.viewport_width,
                        viewport_height=settings.autoplay.viewport_height,
                        think_delay_ms=settings.autoplay.think_delay_ms,
                        real_mouse_speed_pps=settings.autoplay.real_mouse_speed_pps,
                        real_mouse_jitter_px=settings.autoplay.real_mouse_jitter_px,
                    ),
                    game_url=settings.game_url,
                    platform=settings.platform,
                    proxy_server=proxy,
                )
        except Exception as e:
            logger.error(f"[autoplay] Failed to initialize autoplay service: {e}")
            autoplay_service = None

        app_context = AppContext(
            settings=settings,
            controller=mjai_controller,
            bot=mjai_bot,
            mitm_client=MitmClient(shared_queue=self.message_queue),
            electron_client=create_electron_client(settings.platform, shared_queue=self.message_queue),
            autoplay_service=autoplay_service,
            shared_queue=self.message_queue,
        )

        set_app_context(app_context)

    def start(self):
        self.ds.start()
        logger.info(f"DataServer started at {self.frontend_url}")

        app = get_app_context()
        if app.settings.mitm.enabled and app.mitm_client:
            app.mitm_client.start()

        if app.autoplay_service:
            try:
                app.autoplay_service.start()
            except Exception as e:
                logger.error(f"[autoplay] Failed to start autoplay service: {e}")

        if app.electron_client:
            app.electron_client.start()

        self._setup_signals()
        logger.info("Akagi backend loop started.")

    def _setup_signals(self):
        """设置信号处理器以关闭程序"""

        def signal_handler(signum: int, _frame: object) -> None:
            sig_name = signal.Signals(signum).name
            logger.info(f"Received signal {sig_name} ({signum}), initiating shutdown...")
            self.stop()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    def stop(self):
        self._stop_event.set()

    def _handle_system_shutdown(self, msg: dict) -> bool:
        """处理系统关闭消息

        Returns:
            True 如果是关闭消息并已处理 否则False
        """
        if msg.get("type") == "system_shutdown":
            logger.info(f"Received shutdown signal from {msg.get('source', 'unknown')}.")
            self.stop()
            return True
        return False

    def _handle_system_event(self, msg: dict, batch_notifications: list[dict]) -> bool:
        """处理系统事件消息

        Returns:
            True 如果是系统事件且应跳过后续处理 否则False
        """
        if n := NotificationHandler.from_message(msg):
            batch_notifications.append(n)
            if msg.get("type") == "system_event":
                return True
        return False

    def _collect_controller_response(
        self, msg: dict, controller: Controller | None, mjai_responses: list[dict], batch_notifications: list[dict]
    ) -> None:
        """收集 Controller 的响应和通知"""
        if not controller:
            return

        if resp := controller.react(msg):
            mjai_responses.append(resp)
            # 立即采集 Controller 产生的标志 (如模型加载)
            flags = getattr(controller, "notification_flags", {})
            if flags:
                batch_notifications.extend(NotificationHandler.from_flags(flags))

    def _update_bot_state(self, msg: dict, bot: StateTrackerBot | None, batch_notifications: list[dict]) -> None:
        """更新 Bot 状态并收集通知"""
        if not bot:
            return

        bot.react(msg)
        # 立即采集 Bot 产生的标志
        flags = getattr(bot, "notification_flags", {})
        if flags:
            batch_notifications.extend(NotificationHandler.from_flags(flags))

    def _process_message_batch(
        self,
        mjai_msgs: list[dict],
        bot: StateTrackerBot | None,
        controller: Controller | None,
    ) -> tuple[list[dict], list[dict]]:
        """
        处理一批 MJAI 消息

        注意: Controller 必须在 Bot 更新状态之前响应
        Controller 基于当前状态做决策，如果 Bot 先更新状态
        Controller 将基于"未来"状态而非当前事件做出响应
        """
        mjai_responses: list[dict] = []
        batch_notifications: list[dict] = []

        for msg in mjai_msgs:
            try:
                # 0. 处理系统关闭消息
                if self._handle_system_shutdown(msg):
                    continue

                # 1. 从消息本身提取通知 (例如 system_event)
                if self._handle_system_event(msg, batch_notifications):
                    continue

                # Any non-system MJAI message indicates the game state progressed; clear pending autoplay retry.
                self._game_activity_seq += 1
                self._pending_autoplay = None

                # 2. Controller response (decision)
                resp = None
                if controller:
                    resp = controller.react(msg)
                    if resp:
                        mjai_responses.append(resp)
                        flags = getattr(controller, "notification_flags", {})
                        if flags:
                            batch_notifications.extend(NotificationHandler.from_flags(flags))

                # 3. Update state tracker bot
                self._update_bot_state(msg, bot, batch_notifications)

            except (ValueError, KeyError, TypeError) as e:
                logger.error(f"Invalid MJAI message format: {msg}, error: {e}")
            except Exception:
                logger.exception(f"Unexpected error processing MJAI message: {msg}")

        return mjai_responses, batch_notifications

    def _get_next_message(self, timeout: float = ServerConstants.MAIN_LOOP_POLL_TIMEOUT_SECONDS) -> dict | None:
        """
        从事件队列获取下一条消息(阻塞、100ms超时)
        如果超时或队列为空则返回 None

        这是事件驱动的 INPUT 阶段
        """
        try:
            return self.message_queue.get(block=True, timeout=timeout)
        except queue.Empty:
            return None

    def _process_events(
        self, mjai_msgs: list[dict], bot: StateTrackerBot | None, controller: Controller | None
    ) -> dict:
        """
        处理 MJAI 消息批次
        这是 Reactor 模式的 PROCESS 阶段

        Returns:
            mjai_responses: Controller 的响应列表
            notifications: 要发送的通知列表
        """
        mjai_responses, batch_notifications = self._process_message_batch(mjai_msgs, bot, controller)

        return {
            "mjai_responses": mjai_responses,
            "batch_notifications": batch_notifications,
            "is_sync": any(msg.get("sync", False) for msg in mjai_msgs),
        }

    def _estimate_autoplay_steps_duration_seconds(self, steps: list[dict[str, object]]) -> float:
        delay_ms = 0
        click_n = 0
        for s in steps:
            if not isinstance(s, dict):
                continue
            op = s.get("op")
            if op == "delay":
                try:
                    delay_ms += int(s.get("ms") or 0)
                except Exception:
                    pass
            elif op == "click":
                click_n += 1

        # Add headroom for SSE delivery + IPC + CDP click press/release timing.
        est = (delay_ms / 1000.0) + (click_n * 0.25) + 0.2
        return max(0.0, min(15.0, float(est)))

    def _check_autoplay_retry(self) -> None:
        pending = self._pending_autoplay
        if not pending or pending.retried:
            return
        if time.monotonic() < pending.retry_at:
            return
        if self._game_activity_seq != pending.activity_seq:
            self._pending_autoplay = None
            return
        if not self.ds:
            self._pending_autoplay = None
            return

        try:
            self._autoplay_seq += 1
            self.ds.broadcast_event("autoplay", {"seq": self._autoplay_seq, "steps": pending.steps})
            logger.info("[autoplay] No state change detected after action, retrying UI steps once.")
        except Exception as e:
            logger.debug(f"[autoplay] Failed to retry autoplay steps: {e}")
        finally:
            self._pending_autoplay = None

    def _emit_outputs(self, result: dict, bot: StateTrackerBot | None):
        """
        将处理结果发送到 DataServer
        这是 Reactor 模式的 OUTPUT 阶段
        """
        mjai_responses = result["mjai_responses"]
        batch_notifications = result["batch_notifications"]
        is_sync = result.get("is_sync", False)

        # 1. Payload：使用最后一个有效响应
        last_response = mjai_responses[-1] if mjai_responses else {}
        payload = build_dataserver_payload(last_response, bot)

        # 2. Notifications: 从各种来源收集通知
        all_notifications = batch_notifications.copy()

        # 3. 检查响应中的错误
        if error_notification := NotificationHandler.from_error_response(last_response):
            all_notifications.append(error_notification)

        if all_notifications:
            self.ds.send_notifications(all_notifications)

        # 同步期间屏蔽推荐输出，仅保留通知
        if payload and not is_sync:
            self.ds.send_recommendations(payload)

        # Auto-play output: either execute in Playwright (auto_launch_browser) or send UI steps to Electron via SSE.
        if not is_sync and bot:
            try:
                app = get_app_context()
                autoplay_service = getattr(app, "autoplay_service", None)
                if autoplay_service and isinstance(last_response, dict) and not last_response.get("error"):
                    if autoplay_service.config.auto_launch_browser:
                        autoplay_service.handle_action(last_response, bot)
                    else:
                        steps = autoplay_service.plan_steps(last_response, bot)
                        if steps:
                            self._autoplay_seq += 1
                            self.ds.broadcast_event("autoplay", {"seq": self._autoplay_seq, "steps": steps})
                            self._pending_autoplay = _PendingAutoplay(
                                steps=steps,
                                activity_seq=self._game_activity_seq,
                                retry_at=time.monotonic() + self._estimate_autoplay_steps_duration_seconds(steps) + 3.0,
                            )
            except Exception as e:
                logger.debug(f"[autoplay] Failed to emit autoplay steps: {e}")

    def run(self) -> int:
        """
        使用 Reactor 模式的主应用循环。

        循环分三个阶段：
        1. _poll_inputs()   - 从事件源收集消息
        2. _process_events() - 处理消息并生成响应
        3. _emit_outputs()   - 发送结果到 DataServer
        """
        # 启动主循环
        logger.info("Starting main loop...")
        # 捕获引用以减少全局上下文访问
        app = get_app_context()
        bot = app.bot
        controller = app.controller

        try:
            while not self._stop_event.is_set():
                # 阶段 1：INPUT - 从事件队列获取消息（阻塞模式，替代轮询）
                msg = self._get_next_message(timeout=ServerConstants.MAIN_LOOP_POLL_TIMEOUT_SECONDS)
                if not msg:
                    # Timeout, check stop event and continue
                    self._check_autoplay_retry()
                    continue

                # 将单个消息包装为列表以兼容现有处理逻辑
                mjai_msgs = [msg]

                try:
                    # 阶段 2：PROCESS - 处理事件
                    result = self._process_events(mjai_msgs, bot, controller)

                    # 阶段 3：OUTPUT - 分发结果
                    self._emit_outputs(result, bot)

                except Exception as e:
                    logger.exception(f"Critical error in main loop dispatch: {e}")
                    self._stop_event.wait(1.0)

        finally:
            self.cleanup()

        return 0

    def cleanup(self):
        """清理资源并记录详细的关闭日志"""
        logger.info("Stopping Akagi-NG...")
        app = get_app_context()

        # 停止 AutoPlay (Playwright browser)
        if getattr(app, "autoplay_service", None):
            try:
                logger.info("Stopping AutoPlay service...")
                app.autoplay_service.stop()
            except Exception as e:
                logger.error(f"Error stopping AutoPlay service: {e}")

        # 停止 MITM 客户端
        if app.mitm_client:
            try:
                logger.info("Stopping MITM client...")
                app.mitm_client.stop()
            except Exception as e:
                logger.error(f"Error stopping MITM client: {e}")

        # 停止 Electron 客户端
        if app.electron_client:
            try:
                logger.info("Stopping Electron client...")
                app.electron_client.stop()
            except Exception as e:
                logger.error(f"Error stopping Electron client: {e}")

        # 停止 DataServer
        if self.ds:
            try:
                logger.info("Stopping DataServer...")
                self.ds.stop()
            except Exception as e:
                logger.error(f"Error stopping DataServer: {e}")

        logger.info("Akagi-NG stopped successfully.")
