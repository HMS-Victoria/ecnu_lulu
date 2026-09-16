"""后台工作线程：绝不在这里碰 UI 对象，一切通过 Qt 信号回传。

三个 worker：
    * :class:`LoginWorker`    —— 打开可见浏览器等用户手动登录（M0）
    * :class:`CatalogWorker`  —— 拉取全量课程/资源清单（M1）
    * :class:`PipelineWorker` —— 依次执行队列里的「下载 + 转写 + 产物」（M2/M3）

设计要点
--------
* 每个 worker 都是 ``QThread`` 子类，``run()`` 里跑阻塞 IO；
* 进度/日志/完成/失败统一用 Signal 发回主线程；
* ``cancel`` 是 ``threading.Event``，供核心库轮询（可优雅中断）；
* UI 关闭时先 ``stop()`` 再 ``wait()``，避免线程悬空导致崩溃。
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from ecnu_transcribe.catalog import Catalog, Resource
from ecnu_transcribe.client import EcnuClient, load_session_state
from ecnu_transcribe.config import AppConfig, ConfigManager
from ecnu_transcribe.errors import (
    AuthExpiredError,
    DrmDetectedError,
    SiteUnreachableError,
    TaskCancelled,
)
from ecnu_transcribe.logbus import get_logger
from ecnu_transcribe.login import LoginSession
from ecnu_transcribe.pausegate import PauseGate
from ecnu_transcribe.pipeline import Pipeline, PipelineHooks, resource_from_task
from ecnu_transcribe.store import Stage, StateStore, TaskRecord

log = get_logger("app.workers")


# --------------------------------------------------------------------------- #
class LoginWorker(QThread):
    """可见浏览器登录（人工完成统一身份认证）。"""

    status = Signal(str)
    finished_ok = Signal(bool, str)
    browser_opened = Signal()
    #: 用户主动取消（与「失败」区分：取消不该弹错误框吓人）
    canceled = Signal()

    def __init__(self, cfg: AppConfig, *, timeout: float = 900.0, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.timeout = timeout
        self._stop = threading.Event()
        self.session: LoginSession | None = None
        self._confirm = threading.Event()

    # -- 供 UI 调用的交互 ------------------------------------------------ #
    def confirm_logged_in(self) -> None:
        """用户在 UI 上点「我已登录完成」。"""
        self._confirm.set()

    def request_stop(self) -> None:
        self._stop.set()
        self._confirm.set()
        if self.session is not None:
            self.session.request_stop()

    def bring_to_front(self) -> None:
        if self.session is not None:
            self.session.bring_to_front()

    # ------------------------------------------------------------------ #
    def run(self) -> None:  # noqa: D102
        self.session = LoginSession(self.cfg, on_status=self.status.emit, headless=False)
        try:
            self.session.open()
            self.browser_opened.emit()
            deadline = time.time() + self.timeout
            saved = False
            while time.time() < deadline and not self._stop.is_set():
                if self.session._detect_logged_in():
                    self.session.save_storage_state()
                    saved = True
                    self.status.emit("✅ 检测到登录成功，已保存登录态")
                    break
                if self._confirm.is_set():
                    saved = self.session.confirm_logged_in()
                    break
                time.sleep(1.0)

            if self._stop.is_set():
                # 用户主动取消：明确告知，不当成失败
                self.status.emit("已取消登录。")
                self.canceled.emit()
                return
            if not saved:
                saved = self.session.confirm_logged_in()
            self.finished_ok.emit(saved, "" if saved else "登录未完成或超时")
        except SiteUnreachableError as exc:
            self.finished_ok.emit(False, f"无法访问站点：{exc}")
        except Exception as exc:  # noqa: BLE001
            log.error("登录线程异常：%s", traceback.format_exc())
            self.finished_ok.emit(False, f"登录过程出错：{exc}")
        finally:
            try:
                self.session.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
class CatalogWorker(QThread):
    """拉取全量清单。"""

    status = Signal(str)
    finished_ok = Signal(object, str)  # (Catalog|None, error)

    def __init__(self, cfg: AppConfig, cm: ConfigManager, *, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.cm = cm

    def run(self) -> None:  # noqa: D102
        client = EcnuClient(self.cfg, config_manager=self.cm, session=load_session_state())
        try:
            self.status.emit("检查站点可达性…")
            diag = client.diagnose_access()
            self.status.emit(diag.to_text())
            if not diag.reachable:
                self.finished_ok.emit(None, diag.detail or "站点不可达")
                return
            catalog = client.fetch_catalog(on_progress=self.status.emit)
            self.finished_ok.emit(catalog, "")
        except AuthExpiredError as exc:
            self.finished_ok.emit(None, f"AUTH_EXPIRED::{exc}")
        except Exception as exc:  # noqa: BLE001
            log.error("清单线程异常：%s", traceback.format_exc())
            self.finished_ok.emit(None, str(exc))
        finally:
            client.close()


# --------------------------------------------------------------------------- #
@dataclass
class QueueItem:
    task_id: int
    resource: Resource


class PipelineWorker(QThread):
    """执行队列：下载 + 转写 + 写出产物。"""

    task_stage = Signal(int, str, float, str)  # task_id, stage, progress, message
    log_line = Signal(str, str)  # level, message
    task_done = Signal(int, bool, str)  # task_id, ok, error
    queue_finished = Signal(int, int)  # ok_count, fail_count
    request_relogin = Signal(str)

    def __init__(
        self,
        cfg: AppConfig,
        cm: ConfigManager,
        store: StateStore,
        items: list[QueueItem],
        *,
        force: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.cm = cm
        self.store = store
        self.items = items
        self.force = force
        self._cancel = threading.Event()
        self._pause = threading.Event()
        #: 真正作用于**任务内部**的暂停闸门（阶段之间 / 每个音频分段 / 每个 LLM 块 / ffmpeg 拉流）
        self._gate = PauseGate()
        self._gate.bind_cancel_event(self._cancel)
        self._current_task_id: int | None = None

    # -- 控制 ------------------------------------------------------------ #
    def request_pause(self) -> None:
        """暂停。

        当前正在跑的那一小段（一个分段 / 一次 ASR 请求）不会被打断，
        而是在下一个**天然断点**停下：流水线各阶段之间、每个音频分段转写前、
        每个 LLM 处理块前；ffmpeg 拉流则通过阻塞输出读取就地冻结（恢复后继续，不重下）。
        """
        self._pause.set()
        self._gate.pause()

    def resume(self) -> None:
        self._pause.clear()
        self._gate.resume()

    @property
    def paused(self) -> bool:
        return self._gate.is_paused

    def paused_seconds(self) -> float:
        return self._gate.paused_seconds()

    def request_stop(self) -> None:
        self._cancel.set()
        self._pause.clear()
        self._gate.cancel()  # 放行所有在闸门上等待的线程，让取消检查接管

    # ------------------------------------------------------------------ #
    def run(self) -> None:  # noqa: D102
        hooks = PipelineHooks(
            on_stage=lambda tid, stage, pct, msg: self.task_stage.emit(tid, stage, float(pct), msg),
            on_log=lambda level, msg: self.log_line.emit(level, msg),
            cancel=self._cancel,
            gate=self._gate,
        )
        client = EcnuClient(self.cfg, config_manager=self.cm, session=load_session_state())
        pipeline = Pipeline(self.cfg, self.store, cm=self.cm, hooks=hooks, client=client)
        ok = fail = 0
        try:
            for item in self.items:
                if self._cancel.is_set():
                    break
                # 暂停：任务之间也要停（任务内部的暂停由 PauseGate 在断点处接管）
                while self._pause.is_set() and not self._cancel.is_set():
                    time.sleep(0.3)
                if self._cancel.is_set():
                    break

                task = self.store.get_task(item.task_id)
                if task is None:
                    continue
                self._current_task_id = task.id
                # 用队列里的最新 resource（可能被用户在 UI 上改过标题）
                resource = item.resource
                try:
                    self.store.update_stage(
                        task.id, Stage.PENDING, progress=0.0, force=True,
                        error="", message="pipeline start",
                    )
                    final = pipeline.run(task, resource, force=self.force)
                    success = bool(final and final.stage == str(Stage.DONE))
                    if success:
                        ok += 1
                    else:
                        fail += 1
                    self.task_done.emit(
                        task.id, success, (final.error if final and not success else "")
                    )
                except TaskCancelled:
                    self.store.update_stage(task.id, Stage.CANCELED, force=True, error="用户停止")
                    self.task_done.emit(task.id, False, "已停止")
                except AuthExpiredError as exc:
                    self.request_relogin.emit(str(exc))
                    self.task_done.emit(task.id, False, str(exc))
                    fail += 1
                    break
                except DrmDetectedError as exc:
                    self.task_done.emit(task.id, False, f"DRM：{exc}")
                    fail += 1
                except Exception as exc:  # noqa: BLE001
                    log.error("任务 %s 异常：%s", task.id, traceback.format_exc())
                    self.store.mark_failed(task.id, str(exc))
                    self.task_done.emit(task.id, False, str(exc))
                    fail += 1
                self._current_task_id = None
        finally:
            pipeline.close()
            client.close()
            self.queue_finished.emit(ok, fail)


# --------------------------------------------------------------------------- #
class ProbeWorker(QThread):
    """首启一键诊断 / 设置页连通性自检。

    ``which`` 为空时跑**完整首启诊断**（输出目录 / ffmpeg / Chromium / 站点可达性 /
    登录态 / ASR 端点），一次性把「能不能开始用」查清楚。
    """

    result = Signal(str, bool, str)  # name, ok, message
    report = Signal(str)             # 完整报告文本
    finished_ready = Signal(bool)    # 是否全部就绪

    def __init__(self, cfg: AppConfig, cm: ConfigManager, *, parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.cm = cm
        self.which: list[str] = ["ffmpeg", "site", "asr", "llm"]
        self.full_readiness = False

    def run(self) -> None:  # noqa: D102
        if self.full_readiness:
            self._run_readiness()
            return
        self._run_probes()

    def _run_readiness(self) -> None:
        from ecnu_transcribe.login import run_readiness_check

        try:
            report = run_readiness_check(self.cfg, cm=self.cm, deep=True)
        except Exception as exc:  # noqa: BLE001
            log.error("首启诊断异常：%s", traceback.format_exc())
            self.result.emit("首启诊断", False, str(exc))
            self.report.emit(f"首启诊断失败：{exc}")
            self.finished_ready.emit(False)
            return
        for item in report.items:
            self.result.emit(item.name, item.ok, f"{item.icon} {item.detail or ('正常' if item.ok else '未通过')}")
        self.report.emit(report.to_text())
        self.finished_ready.emit(report.ready)

    def _run_probes(self) -> None:
        from ecnu_transcribe import media
        from ecnu_transcribe.llm import probe_llm
        from ecnu_transcribe.transcriber import probe_asr_endpoint

        if "ffmpeg" in self.which:
            try:
                exe = media.find_ffmpeg(self.cfg.ffmpeg_path)
                ver = media.ffmpeg_version(exe)
                self.result.emit("ffmpeg", True, f"{exe}\n{ver}")
            except Exception as exc:  # noqa: BLE001
                self.result.emit("ffmpeg", False, str(exc))

        if "site" in self.which:
            try:
                client = EcnuClient(self.cfg, config_manager=self.cm, session=load_session_state())
                diag = client.diagnose_access()
                client.close()
                self.result.emit("站点", diag.reachable, diag.to_text())
            except Exception as exc:  # noqa: BLE001
                self.result.emit("站点", False, str(exc))

        if "asr" in self.which:
            try:
                key = self.cm.secret("asr_api_key")
                ok, msg = probe_asr_endpoint(self.cfg, key)
                self.result.emit("ASR", ok, msg)
            except Exception as exc:  # noqa: BLE001
                self.result.emit("ASR", False, str(exc))

        if "llm" in self.which:
            try:
                key = self.cm.secret("llm_api_key")
                if not key:
                    self.result.emit("DeepSeek", False, "未配置 API Key（LLM 后处理为可选功能）")
                else:
                    ok, msg = probe_llm(self.cfg, key)
                    self.result.emit("DeepSeek", ok, msg)
            except Exception as exc:  # noqa: BLE001
                self.result.emit("DeepSeek", False, str(exc))
        self.finished_ready.emit(True)
