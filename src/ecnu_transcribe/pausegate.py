"""可暂停闸门（PauseGate）：让「暂停」真正作用于**单个长任务内部**，而不只是任务之间。

背景
----
一节课的录播可能有 1~2 小时，下载 + 转写要跑十几分钟。
如果「暂停」只在任务之间生效，用户点了暂停却要等当前任务跑完 —— 体验很差。

用法
----
核心库在每个**天然断点**调用 :meth:`PauseGate.wait`：

    * 流水线每个阶段之间（probing → downloading → audio_ready → transcribing → …）
    * 每个音频分段（chunk）转写之前
    * 每个 LLM 处理块之前
    * ffmpeg 拉流过程中（downloader 把它转成「等一会儿再继续读输出」）

语义
----
* ``pause()`` 后，所有 ``wait()`` 会阻塞，直到 ``resume()`` 或 ``cancel()``；
* ``cancel()``（用户点「停止」）会立刻放行，让调用方的取消检查接管并抛 ``TaskCancelled``；
* 线程安全；未暂停时 ``wait()`` 是**无锁快速路径**（只做一次 ``Event.is_set()``）。
"""

from __future__ import annotations

import threading
import time


class PauseGate:
    """线程安全的暂停闸门。"""

    def __init__(self, *, poll_interval: float = 0.2) -> None:
        self._paused = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self._cancel = threading.Event()
        self._poll = poll_interval
        self._lock = threading.Lock()
        self._paused_since: float = 0.0
        self.total_paused_sec: float = 0.0

    # ------------------------------------------------------------------ #
    # 控制端
    # ------------------------------------------------------------------ #
    def pause(self) -> None:
        with self._lock:
            if not self._paused.is_set():
                self._paused.set()
                self._resume.clear()
                self._paused_since = time.time()

    def resume(self) -> None:
        with self._lock:
            if self._paused.is_set():
                self.total_paused_sec += time.time() - self._paused_since
                self._paused_since = 0.0
            self._paused.clear()
            self._resume.set()

    def cancel(self) -> None:
        """取消会立刻放行所有等待者（调用方随后自行检查取消标志）。"""
        with self._lock:
            if self._paused.is_set():
                self.total_paused_sec += time.time() - self._paused_since
                self._paused_since = 0.0
            self._paused.clear()
            self._resume.set()
            self._cancel.set()

    def bind_cancel_event(self, event: threading.Event) -> None:
        """把外部的取消事件绑进来：取消时也能放行暂停中的线程。"""
        self._external_cancel = event

    # ------------------------------------------------------------------ #
    # 状态查询
    # ------------------------------------------------------------------ #
    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    @property
    def is_canceled(self) -> bool:
        return self._cancel.is_set()

    def paused_seconds(self) -> float:
        with self._lock:
            extra = (time.time() - self._paused_since) if self._paused_since else 0.0
            return self.total_paused_sec + extra

    # ------------------------------------------------------------------ #
    # 工作线程端
    # ------------------------------------------------------------------ #
    def wait(self, *, timeout: float | None = None) -> bool:
        """如果处于暂停状态就阻塞。

        返回 ``True`` 表示「可以继续」，``False`` 表示在暂停期间被取消。
        ``timeout`` 到了也返回 ``True``（调用方正常继续，用于不希望无限等待的场景）。
        """
        if not self._paused.is_set():
            return not self._cancel.is_set()
        end = (time.time() + timeout) if timeout else None
        while True:
            if self._cancel.is_set():
                return False
            if self._resume.wait(self._poll):
                return not self._cancel.is_set()
            if end is not None and time.time() >= end:
                return True
            ext = getattr(self, "_external_cancel", None)
            if ext is not None and ext.is_set():
                return False


#: 全局默认闸门（未显式传入时的兜底，保证核心库任何路径都不会因缺少闸门而报错）
NULL_GATE = PauseGate()
