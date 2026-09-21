"""隔离的真实 GUI → 本地模拟平台 → ffmpeg → 模拟 ASR → 文件与续跑。"""
import copy
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QTest

from test_gui_recovery import env, qapp, dialogs
from test_platform_api import _load_fake
from test_chunked_pipeline import FakeASR


def until(qapp, predicate, seconds=30):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    assert predicate(), "后台操作未在限定时间内完成"


def test_gui_catalog_to_outputs_and_cached_resume(env, qapp, tone_audio, monkeypatch):
    win, store, cfg, cm, _ = env
    fake = _load_fake()
    original_get = fake.Handler.do_GET
    def with_portal(handler):
        if handler.path.startswith("/jy-application-resourcemanage-ui/"):
            handler._send(200, {"page": "课程资源"})
        else:
            original_get(handler)
    monkeypatch.setattr(fake.Handler, "do_GET", with_portal)
    videos = copy.deepcopy(fake.VIDEOS)
    for rows in videos.values():
        for row in rows:
            row["url"] = str(tone_audio)
            row["vodTime"] = 3
    monkeypatch.setattr(fake, "VIDEOS", videos)
    curriculum = copy.deepcopy(fake.CURRICULUM)
    for rows in curriculum.values():
        for row in rows:
            row["courTime"] = 3
    monkeypatch.setattr(fake, "CURRICULUM", curriculum)
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True)
    with fake.FakePlatform() as platform, FakeASR() as asr:
        cfg.portal_url = platform.base + "/jy-application-resourcemanage-ui/#/home"
        cfg.api_base = platform.base
        cfg.asr_base_url = asr.base_url
        cfg.asr_auto_gain = False
        cfg.asr_retries = 1
        win.act_refresh.trigger()
        until(qapp, lambda: win.catalog_worker is not None and not win.catalog_worker.isRunning())
        assert win.catalog and win.catalog.resource_count > 0
        assert store.list_tasks() == []
        win.tree.topLevelItem(0).child(0).setCheckState(0, Qt.Checked)
        QTest.mouseClick(win.btn_transcribe, Qt.LeftButton)
        assert win.pipeline_worker is not None
        win.on_pause()
        assert win.pipeline_worker.paused
        QTest.qWait(100)
        win.on_pause()
        until(qapp, lambda: not win.pipeline_worker.isRunning())
        task = store.list_tasks()[0]
        assert task.stage == "done", task.error
        assert len(task.outputs) == 3
        for output in task.outputs:
            assert Path(output).read_bytes().startswith(b"\xef\xbb\xbf")
        win._select_task(task.id)
        win.output_row.itemAt(0).widget().click()
        assert opened and Path(opened[0]).is_file()
        before = Path(task.audio_path).stat().st_mtime_ns
        count = len(asr.calls)
        win.on_requeue_selected(force=False)
        win.act_start.trigger()
        until(qapp, lambda: not win.pipeline_worker.isRunning())
        assert store.get_task(task.id).stage == "done"
        assert Path(task.audio_path).stat().st_mtime_ns == before
        assert len(asr.calls) == count
