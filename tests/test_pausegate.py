"""暂停闸门（PauseGate）测试。

回归的用户体验问题：一节课的录播可能 1~2 小时，「暂停」如果只在任务之间生效，
用户点了暂停要等当前任务跑完（十几分钟）——很糟。现在暂停作用于**任务内部**的天然断点。

覆盖：
    * 闸门本身：pause / resume / cancel 语义、超时、暂停时长统计、无锁快速路径；
    * 流水线：阶段之间能停下、恢复后继续、暂停期间「停止」能立刻放行；
    * 转写器：分段边界能停下；
    * 取消优先：暂停中取消必须立刻抛出，不能卡住。
"""

from __future__ import annotations

import threading
import time

import pytest

from ecnu_transcribe.errors import TaskCancelled
from ecnu_transcribe.pausegate import PauseGate
from ecnu_transcribe.pipeline import PipelineHooks


# --------------------------------------------------------------------------- #
# 闸门本身
# --------------------------------------------------------------------------- #
def test_gate_not_paused_is_fast_path():
    g = PauseGate()
    t0 = time.time()
    assert g.wait() is True
    assert time.time() - t0 < 0.05, "未暂停时 wait() 应该是无锁快速路径"


def test_gate_pause_blocks_until_resume():
    g = PauseGate(poll_interval=0.01)
    g.pause()
    assert g.is_paused is True

    released: list[float] = []

    def worker() -> None:
        g.wait()
        released.append(time.time())

    t = threading.Thread(target=worker, daemon=True)
    start = time.time()
    t.start()
    time.sleep(0.15)
    assert not released, "暂停期间不应放行"
    g.resume()
    t.join(timeout=2.0)
    assert released, "resume() 后应立刻放行"
    assert released[0] - start >= 0.1


def test_gate_cancel_releases_waiter_and_reports_canceled():
    g = PauseGate(poll_interval=0.01)
    g.pause()
    result: list[bool] = []

    def worker() -> None:
        result.append(g.wait())

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    time.sleep(0.1)
    g.cancel()
    t.join(timeout=2.0)
    assert result == [False], "取消后 wait() 应返回 False"
    assert g.is_canceled is True


def test_gate_wait_timeout_returns_true():
    """带超时的等待到期即放行（用于不希望无限阻塞的场景）。"""
    g = PauseGate(poll_interval=0.01)
    g.pause()
    t0 = time.time()
    assert g.wait(timeout=0.2) is True
    assert 0.15 <= time.time() - t0 < 1.0


def test_gate_paused_seconds_tracks_accumulated_time():
    g = PauseGate(poll_interval=0.01)
    assert g.paused_seconds() == pytest.approx(0.0)
    g.pause()
    time.sleep(0.15)
    mid = g.paused_seconds()
    assert mid >= 0.1
    g.resume()
    after = g.paused_seconds()
    assert after >= mid
    time.sleep(0.1)
    assert g.paused_seconds() == pytest.approx(after, abs=0.05), "恢复后不应继续累计"


def test_gate_external_cancel_event_releases_waiter():
    """绑定外部取消事件后，外部取消也能放行（避免两条取消路径不一致）。"""
    g = PauseGate(poll_interval=0.01)
    ext = threading.Event()
    g.bind_cancel_event(ext)
    g.pause()
    result: list[bool] = []
    t = threading.Thread(target=lambda: result.append(g.wait()), daemon=True)
    t.start()
    time.sleep(0.1)
    ext.set()
    t.join(timeout=2.0)
    assert result == [False]


def test_gate_resume_when_not_paused_is_noop():
    g = PauseGate()
    g.resume()
    assert g.is_paused is False
    assert g.paused_seconds() == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# PipelineHooks.checkpoint
# --------------------------------------------------------------------------- #
def _hooks(**kw) -> PipelineHooks:
    return PipelineHooks(**kw)


def test_checkpoint_returns_immediately_when_running():
    h = _hooks()
    t0 = time.time()
    h.checkpoint(1, "downloading", 50.0)
    assert time.time() - t0 < 0.05


def test_checkpoint_raises_when_canceled():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(TaskCancelled):
        _hooks(cancel=cancel).checkpoint(1, "downloading", 50.0)


def test_checkpoint_blocks_while_paused_then_continues():
    gate = PauseGate(poll_interval=0.01)
    stages: list[tuple[str, float, str]] = []
    h = _hooks(gate=gate, on_stage=lambda tid, st, pct, msg: stages.append((st, pct, msg)))
    gate.pause()

    done = threading.Event()

    def worker() -> None:
        h.checkpoint(7, "transcribing", 90.0, "准备语音识别")
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    time.sleep(0.2)
    assert not done.is_set(), "暂停时应阻塞在断点"
    # 应至少报了一次「已暂停」给 UI（消息里带 ⏸ 标记 + 恢复指引）
    pause_msgs = [msg for _st, _p, msg in stages if "⏸" in msg or "暂停" in msg]
    assert pause_msgs, f"UI 应收到暂停提示，实际：{stages}"
    assert any("继续" in msg for msg in pause_msgs), "暂停提示应告诉用户怎么恢复"
    gate.resume()
    assert done.wait(timeout=2.0), "恢复后应放行"
    assert any("▶" in msg or "恢复" in msg for _st, _p, msg in stages), "UI 应收到恢复提示"


def test_checkpoint_releases_on_cancel_while_paused():
    """暂停中用户点「停止」：必须立刻抛 TaskCancelled，不能卡死。"""
    gate = PauseGate(poll_interval=0.01)
    cancel = threading.Event()
    h = _hooks(gate=gate, cancel=cancel)
    gate.pause()

    outcome: list[str] = []

    def worker() -> None:
        try:
            h.checkpoint(9, "downloading", 40.0)
            outcome.append("completed")
        except TaskCancelled:
            outcome.append("cancelled")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    time.sleep(0.2)
    cancel.set()
    gate.cancel()
    t.join(timeout=3.0)
    assert outcome == ["cancelled"], f"暂停中取消应立刻抛出，实际：{outcome}"


def test_checkpoint_reports_pause_progress_periodically():
    """长时间暂停时，UI 应周期性收到「已暂停 Ns」，而不是一片沉默。"""
    gate = PauseGate(poll_interval=0.01)
    seen: list[str] = []
    h = PipelineHooks(gate=gate, on_stage=lambda tid, st, pct, msg: seen.append(msg))
    gate.pause()
    t = threading.Thread(target=lambda: h.checkpoint(1, "downloading", 10.0), daemon=True)
    t.start()
    time.sleep(0.3)
    gate.resume()
    t.join(timeout=2.0)
    assert seen, "应至少上报一次状态"


# --------------------------------------------------------------------------- #
# 转写器：分段边界暂停
# --------------------------------------------------------------------------- #
def test_transcriber_wait_gate_blocks_and_resumes():
    from ecnu_transcribe.config import AppConfig
    from ecnu_transcribe.transcriber import OpenAICompatibleTranscriber

    gate = PauseGate(poll_interval=0.01)
    cfg = AppConfig()
    cfg.asr_base_url = "http://127.0.0.1:9/v1"  # 本机 → 不要求 Key；本测试不会真的发请求
    cfg.asr_model = "m"
    tr = OpenAICompatibleTranscriber(cfg, "", gate=gate)

    gate.pause()
    done = threading.Event()
    t = threading.Thread(target=lambda: (tr._wait_gate(), done.set()), daemon=True)
    t.start()
    time.sleep(0.2)
    assert not done.is_set(), "暂停时 _wait_gate 应阻塞"
    gate.resume()
    assert done.wait(timeout=2.0), "恢复后应放行"


def test_transcriber_wait_gate_noop_without_gate():
    from ecnu_transcribe.config import AppConfig
    from ecnu_transcribe.transcriber import OpenAICompatibleTranscriber

    cfg = AppConfig()
    cfg.asr_base_url = "http://127.0.0.1:9/v1"
    tr = OpenAICompatibleTranscriber(cfg, "", gate=None)
    t0 = time.time()
    tr._wait_gate()
    assert time.time() - t0 < 0.05


def test_create_transcriber_accepts_gate():
    from ecnu_transcribe.config import AppConfig
    from ecnu_transcribe.config import ConfigManager
    from ecnu_transcribe.transcriber import create_transcriber

    cfg = AppConfig()
    cfg.asr_provider = "openai_compatible"
    cfg.asr_base_url = "http://127.0.0.1:9/v1"
    gate = PauseGate()
    tr = create_transcriber(cfg, cm=None, gate=gate)
    assert tr.gate is gate


# --------------------------------------------------------------------------- #
# 下载器：拉流就地冻结
# --------------------------------------------------------------------------- #
def test_downloader_check_cancel_blocks_while_paused(tmp_path):
    from ecnu_transcribe.config import AppConfig
    from ecnu_transcribe.downloader import AudioDownloader

    gate = PauseGate(poll_interval=0.01)
    dl = AudioDownloader(AppConfig(), gate=gate)
    gate.pause()
    done = threading.Event()
    t = threading.Thread(target=lambda: (dl._check_cancel(), done.set()), daemon=True)
    t.start()
    time.sleep(0.2)
    assert not done.is_set()
    gate.resume()
    assert done.wait(timeout=2.0)


def test_downloader_check_cancel_raises_on_cancel():
    from ecnu_transcribe.config import AppConfig
    from ecnu_transcribe.downloader import AudioDownloader

    cancel = threading.Event()
    cancel.set()
    dl = AudioDownloader(AppConfig(), cancel=cancel)
    with pytest.raises(TaskCancelled):
        dl._check_cancel()


def test_downloader_cancel_wins_over_pause():
    """暂停中取消：_check_cancel 必须抛 TaskCancelled 而不是继续等。"""
    from ecnu_transcribe.config import AppConfig
    from ecnu_transcribe.downloader import AudioDownloader

    gate = PauseGate(poll_interval=0.01)
    cancel = threading.Event()
    dl = AudioDownloader(AppConfig(), cancel=cancel, gate=gate)
    gate.pause()
    outcome: list[str] = []

    def worker() -> None:
        try:
            dl._check_cancel()
            outcome.append("completed")
        except TaskCancelled:
            outcome.append("cancelled")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    time.sleep(0.2)
    cancel.set()
    gate.cancel()
    t.join(timeout=3.0)
    assert outcome == ["cancelled"]
