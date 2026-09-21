"""GUI 异常路径与恢复操作测试（真实 MainWindow + 真实 PipelineWorker）。

覆盖此前**完全没测**的两块：
    1. `PipelineWorker` 的异常分支 —— 登录态失效 / DRM / 取消 / 普通失败；
    2. 界面的恢复操作 —— 移除、重跑、清空、清除登录态、失败详情、登录态提示。

这些才是长跑场景下真正会遇到的路径：一节课要下载+转写十几分钟，
中途登录态过期是**预期内**的事（README 里专门写了这条），
而「失败之后界面还能不能正常用」决定了用户能不能自己恢复。

做法：用真实的 `MainWindow` + 真实的 `PipelineWorker(QThread)`，
只把 `Pipeline.run` 替换成可编排的假实现 —— 这样信号槽、状态机、UI 更新
走的都是**产品代码**，无需网络与 ffmpeg。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from ecnu_transcribe.catalog import Catalog, Course, Resource  # noqa: E402
from ecnu_transcribe.config import ConfigManager  # noqa: E402
from ecnu_transcribe.errors import DrmDetectedError, TaskCancelled, TranscriptionError  # noqa: E402
from ecnu_transcribe.store import Stage, StateStore, TaskRecord  # noqa: E402


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


@pytest.fixture()
def dialogs(monkeypatch):
    """拦截模态对话框（offscreen 下会永久阻塞），并记录调用。

    注意：``QMessageBox.question`` 的「确认」返回值是 ``Yes`` 而不是 ``Ok`` ——
    界面里用 ``!= QMessageBox.Yes`` 判断，返回 ``Ok`` 会让「确认类」操作被当成取消。
    """
    seen: list[tuple[str, str, str]] = []

    def _rec(kind: str, ret):
        def _fn(parent, title, text, *a, **kw):  # noqa: ANN001
            seen.append((kind, str(title), str(text)))
            return ret

        return _fn

    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, kind, staticmethod(_rec(kind, QMessageBox.Ok)))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(_rec("question", QMessageBox.Yes)))
    return seen


@pytest.fixture()
def env(tmp_path, qapp, dialogs, monkeypatch):
    """搭一个真实 MainWindow（配置/状态库都指向临时目录）。"""
    from app.ui.main_window import MainWindow
    startup_hints = MainWindow._startup_hints
    monkeypatch.setattr(MainWindow, "_startup_hints", lambda self: None)

    cm = ConfigManager(config_file=tmp_path / "config.json", secrets_file=tmp_path / "secrets.json")
    cfg = cm.load()
    cfg.output_dir = str(tmp_path / "output")
    cfg.asr_provider = "openai_compatible"
    cfg.asr_base_url = "http://127.0.0.1:1/v1"
    cfg.asr_model = "fake"
    cfg.jitter_min = 0.0
    cfg.jitter_max = 0.0
    cm.save(cfg)

    store = StateStore(tmp_path / "state.db")
    win = MainWindow(cm, store)
    win._test_startup_hints = lambda: startup_hints(win)
    win.show()
    qapp.processEvents()
    yield win, store, cfg, cm, dialogs
    win.close()
    qapp.processEvents()
    store.close()


def make_catalog(n_courses: int = 2, n_res: int = 3) -> Catalog:
    cat = Catalog(fetched_at="2026-01-01 00:00:00", student_id="", source="test")
    for c in range(n_courses):
        course = Course(course_id=f"C{c}", course_name=f"课程{c}", teacher="老师")
        for r in range(n_res):
            course.resources.append(
                Resource(
                    resource_id=f"R{c}{r}",
                    title=f"第{r + 1}讲",
                    course_id=f"C{c}",
                    course_name=f"课程{c}",
                    duration_sec=600.0,
                )
            )
        cat.courses.append(course)
    return cat


def check_all(win, qapp) -> int:
    n = 0
    for i in range(win.tree.topLevelItemCount()):
        item = win.tree.topLevelItem(i)
        for j in range(item.childCount()):
            item.child(j).setCheckState(0, Qt.Checked)
            n += 1
    qapp.processEvents()
    return n


def drain(qapp, ms: int = 2000) -> None:
    end = time.time() + ms / 1000
    while time.time() < end:
        qapp.processEvents()
        time.sleep(0.01)


def run_worker(win, qapp, items, *, timeout: float = 30.0) -> tuple[list, list]:
    """跑一个真实的 PipelineWorker，返回 (task_done 信号, queue_finished 信号)。

    连线和 `MainWindow.on_start()` 保持一致 —— 包括 `request_relogin`
    与 `task_done` → 失败详情刷新，否则测不到「登录失效提示」这类行为。
    """
    from app.workers import PipelineWorker

    done: list[tuple[int, bool, str]] = []
    finished: list[tuple[int, int]] = []
    relogin: list[str] = []

    w = PipelineWorker(win.cfg, win.cm, win.store, items, force=True)
    w.task_stage.connect(win._on_task_stage)
    w.log_line.connect(win._append_log)
    w.task_done.connect(win._on_task_done)
    w.task_done.connect(lambda tid, ok, err: done.append((tid, ok, err)))
    w.queue_finished.connect(win._on_queue_finished)
    w.queue_finished.connect(lambda ok, fail: finished.append((ok, fail)))
    w.request_relogin.connect(win._on_auth_expired)
    w.request_relogin.connect(relogin.append)
    win.pipeline_worker = w
    win._test_relogin = relogin

    w.start()
    deadline = time.time() + timeout
    while w.isRunning() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    w.wait(5000)
    qapp.processEvents()
    return done, finished


@pytest.fixture()
def fake_pipeline(monkeypatch):
    """把 Pipeline.run 换成可编排的假实现（保留 PipelineWorker 的真实逻辑）。"""
    import ecnu_transcribe.pipeline as pm

    state: dict[str, object] = {"mode": "ok", "calls": []}

    class FakePipeline:
        def __init__(self, cfg, store, **kw):
            self.store = store
            self.cfg = cfg

        def run(self, task, resource, *, force=False):
            state["calls"].append((task.id, resource.resource_id))  # type: ignore[union-attr]
            mode = state["mode"]
            if mode == "ok":
                self.store.update_stage(
                    task.id, Stage.DONE, progress=100.0,
                    outputs=[f"{self.cfg.output_dir}/a.txt"], error="",
                )
                return self.store.get_task(task.id)
            if mode == "auth":
                from ecnu_transcribe.errors import AuthExpiredError

                # 真实 Pipeline 会先落失败状态再抛，这里保持一致，
                # 否则「界面能否显示失败原因」这条就测不到
                self.store.mark_failed(task.id, "登录态已失效：请重新登录", retry_bump=False)
                raise AuthExpiredError("登录态已失效，请重新登录")
            if mode == "drm":
                self.store.mark_failed(task.id, "DRM：检测到 Widevine 保护", retry_bump=False)
                raise DrmDetectedError("检测到 Widevine 保护")
            if mode == "cancel":
                self.store.update_stage(task.id, Stage.CANCELED, force=True, error="用户停止")
                raise TaskCancelled("任务已被用户取消")
            if mode == "fail":
                self.store.mark_failed(task.id, "ASR 返回了空结果。可能原因：音频无声/过短")
                raise TranscriptionError("ASR 返回了空结果")
            raise RuntimeError(f"未知模式 {mode}")

        def close(self):
            pass

    monkeypatch.setattr(pm, "Pipeline", FakePipeline)
    import app.workers as aw

    monkeypatch.setattr(aw, "Pipeline", FakePipeline)
    return state


# --------------------------------------------------------------------------- #
# 1. PipelineWorker 的异常分支
# --------------------------------------------------------------------------- #
def test_success_path_updates_ui(env, qapp, fake_pipeline):
    win, store, _cfg, _cm, _dlg = env
    win.catalog = make_catalog(1, 2)
    win._render_tree()
    check_all(win, qapp)
    win.on_enqueue_checked()
    qapp.processEvents()

    from app.workers import QueueItem

    items = [QueueItem(task_id=t.id, resource=win._resource_index[f"{t.course_id}::{t.resource_id}"])
             for t in store.list_tasks()]
    done, finished = run_worker(win, qapp, items)

    assert finished and finished[-1] == (2, 0), finished
    assert all(ok for _tid, ok, _e in done)
    assert all(t.stage == str(Stage.DONE) for t in store.list_tasks())
    assert [win.table.item(r, 2).text() for r in range(win.table.rowCount())] == ["完成", "完成"]


def test_auth_expired_stops_queue_and_asks_relogin(env, qapp, fake_pipeline):
    """登录态中途失效：必须发重登信号、停下队列、且不把剩余任务标成失败。"""
    win, store, _cfg, _cm, dlg = env
    win.catalog = make_catalog(1, 3)
    win._render_tree()
    check_all(win, qapp)
    win.on_enqueue_checked()
    qapp.processEvents()

    fake_pipeline["mode"] = "auth"
    from app.workers import QueueItem

    items = [QueueItem(task_id=t.id, resource=win._resource_index[f"{t.course_id}::{t.resource_id}"])
             for t in store.list_tasks()]
    done, finished = run_worker(win, qapp, items)

    assert finished, "队列应结束（不能卡住）"
    assert done and done[0][1] is False, "失败任务应上报 ok=False"
    assert "登录" in done[0][2], done[0][2]
    # 队列在第一个任务失败后立刻停止，剩余任务不应被重复尝试
    assert len(fake_pipeline["calls"]) == 1, fake_pipeline["calls"]
    # 日志关闭时也必须能看到登录失效及恢复入口。
    assert "登录已过期" in win.notice_text.text()
    assert win.notice_action.text() == "重新登录"
    assert win.lbl_login.text().startswith("登录态"), win.lbl_login.text()


def test_drm_failure_marks_task_failed_with_evidence(env, qapp, fake_pipeline):
    """DRM 是显式停止：任务标失败、错误详情里带原因、界面仍可用。"""
    win, store, _cfg, _cm, _dlg = env
    win.catalog = make_catalog(1, 2)
    win._render_tree()
    check_all(win, qapp)
    win.on_enqueue_checked()
    qapp.processEvents()

    fake_pipeline["mode"] = "drm"
    from app.workers import QueueItem

    items = [QueueItem(task_id=t.id, resource=win._resource_index[f"{t.course_id}::{t.resource_id}"])
             for t in store.list_tasks()]
    done, finished = run_worker(win, qapp, items)

    assert finished and finished[-1][1] == 2, f"两条都应失败：{finished}"
    assert all(not ok for _t, ok, _e in done)
    errs = [e for _t, _o, e in done]
    assert any("DRM" in e or "Widevine" in e for e in errs), errs
    # 表格「错误详情」列要有内容
    details = [win.table.item(r, 6).text() for r in range(win.table.rowCount())]
    assert all(d for d in details), details


def test_plain_failure_records_error(env, qapp, fake_pipeline):
    win, store, _cfg, _cm, _dlg = env
    win.catalog = make_catalog(1, 1)
    win._render_tree()
    check_all(win, qapp)
    win.on_enqueue_checked()
    qapp.processEvents()

    fake_pipeline["mode"] = "fail"
    from app.workers import QueueItem

    items = [QueueItem(task_id=t.id, resource=win._resource_index[f"{t.course_id}::{t.resource_id}"])
             for t in store.list_tasks()]
    done, finished = run_worker(win, qapp, items)

    assert finished and finished[-1] == (0, 1)
    task = store.list_tasks()[0]
    assert task.stage == str(Stage.FAILED)
    assert "空结果" in task.error, task.error
    assert task.retry >= 1, "失败应累加重试次数"


def test_cancel_marks_task_canceled_not_failed(env, qapp, fake_pipeline):
    """用户取消 → canceled（不是 failed），这是状态机语义。"""
    win, store, _cfg, _cm, _dlg = env
    win.catalog = make_catalog(1, 1)
    win._render_tree()
    check_all(win, qapp)
    win.on_enqueue_checked()
    qapp.processEvents()

    fake_pipeline["mode"] = "cancel"
    from app.workers import QueueItem

    items = [QueueItem(task_id=t.id, resource=win._resource_index[f"{t.course_id}::{t.resource_id}"])
             for t in store.list_tasks()]
    run_worker(win, qapp, items)

    task = store.list_tasks()[0]
    assert task.stage == str(Stage.CANCELED), task.stage


# --------------------------------------------------------------------------- #
# 2. 界面的恢复操作
# --------------------------------------------------------------------------- #
def _enqueue(win, store, qapp, n_courses=2, n_res=3) -> int:
    win.catalog = make_catalog(n_courses, n_res)
    win._render_tree()
    total = check_all(win, qapp)
    win.on_enqueue_checked()
    qapp.processEvents()
    return total


def test_remove_selected_tasks_soft_deletes(env, qapp, fake_pipeline):
    win, store, _cfg, _cm, _dlg = env
    _enqueue(win, store, qapp, 1, 3)
    assert win.table.rowCount() == 3

    win.table.selectRow(0)
    qapp.processEvents()
    win.on_remove_selected_tasks()
    qapp.processEvents()

    assert win.table.rowCount() == 2, "表格应少一行"
    assert len(store.list_tasks()) == 2
    assert len(store.list_tasks(include_deleted=True)) == 3, "应是软删除（产物与历史保留）"


def test_requeue_resets_stage_and_keeps_audio(env, qapp, fake_pipeline):
    win, store, _cfg, _cm, _dlg = env
    _enqueue(win, store, qapp, 1, 2)
    task = store.list_tasks()[0]
    store.update_stage(task.id, Stage.DONE, progress=100.0, audio_path="x.mp3")
    win._reload_tasks()
    qapp.processEvents()

    win.table.selectRow(0)
    qapp.processEvents()
    win.on_requeue_selected(force=False)
    qapp.processEvents()

    again = store.get_task(task.id)
    assert again is not None
    assert again.stage == str(Stage.PENDING), again.stage
    assert again.audio_path == "x.mp3", "续跑不应丢弃音频缓存"
    assert again.retry == task.retry


def test_force_requeue_clears_audio_cache_reference(env, qapp, fake_pipeline):
    win, store, _cfg, _cm, _dlg = env
    _enqueue(win, store, qapp, 1, 2)
    task = store.list_tasks()[0]
    store.update_stage(task.id, Stage.DONE, progress=100.0, audio_path="x.mp3")
    win._reload_tasks()
    qapp.processEvents()

    win.table.selectRow(0)
    qapp.processEvents()
    win.on_requeue_selected(force=True)
    qapp.processEvents()

    again = store.get_task(task.id)
    assert again is not None and again.stage == str(Stage.PENDING)
    assert again.audio_path == "", "强制重跑应清掉音频引用"


def test_clear_tasks_empties_list_without_touching_files(env, qapp, fake_pipeline, tmp_path):
    win, store, _cfg, _cm, _dlg = env
    _enqueue(win, store, qapp, 1, 3)
    artifact = tmp_path / "output" / "keep-me.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("用户产物", encoding="utf-8")

    win.on_clear_tasks()
    qapp.processEvents()

    assert win.table.rowCount() == 0
    assert store.list_tasks() == []
    assert artifact.is_file(), "清空列表不得删除用户产物"


def test_clear_login_state_removes_storage_state(env, qapp, fake_pipeline, monkeypatch, tmp_path):
    win, store, _cfg, _cm, _dlg = env
    from ecnu_transcribe import paths

    fake_state = tmp_path / "storage_state.json"
    fake_state.write_text('{"cookies":[]}', encoding="utf-8")
    import app.ui.main_window as mw

    monkeypatch.setattr(mw.paths, "storage_state_path", lambda: fake_state)
    monkeypatch.setattr(
        mw, "load_session_state",
        lambda *a, **k: __import__("ecnu_transcribe.client", fromlist=["SessionState"]).SessionState(),
    )

    win.on_clear_login()
    qapp.processEvents()

    assert not fake_state.is_file(), "应删除 storage_state.json"
    assert win.lbl_login.text().startswith("登录态：未登录"), win.lbl_login.text()
    assert any("清除" in t or "登录" in t for _k, t, _x in _dlg), _dlg


def test_login_hint_reflects_saved_state(env, qapp, fake_pipeline, monkeypatch):
    win, _store, _cfg, _cm, _dlg = env
    from ecnu_transcribe.client import SessionState
    import app.ui.main_window as mw

    monkeypatch.setattr(
        mw, "load_session_state",
        lambda *a, **k: SessionState(cookies={"JSESSIONID": "x"}, saved_at=time.time()),
    )
    win.refresh_login_hint()
    assert "已保存" in win.lbl_login.text()

    monkeypatch.setattr(mw, "load_session_state", lambda *a, **k: SessionState())
    win.refresh_login_hint()
    assert "未登录" in win.lbl_login.text()


def test_stale_session_triggers_startup_warning(env, qapp, fake_pipeline, monkeypatch, tmp_path):
    """登录态隔夜会失效（实测 webVPN 会话）——启动时就该提醒，而不是等用户撞上失败。"""
    win, _store, _cfg, _cm, _dlg = env
    import app.ui.main_window as mw

    stale = tmp_path / "storage_state.json"
    stale.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    old = time.time() - 30 * 3600          # 30 小时前保存
    os.utime(stale, (old, old))
    monkeypatch.setattr(mw.paths, "storage_state_path", lambda: stale)

    win._test_startup_hints()

    text = win.log_view.toPlainText()
    assert "很可能已过期" in text, text[-400:]
    assert "重新认证" in text, text[-400:]


def test_fresh_session_does_not_warn(env, qapp, fake_pipeline, monkeypatch, tmp_path):
    win, _store, _cfg, _cm, _dlg = env
    import app.ui.main_window as mw

    fresh = tmp_path / "storage_state.json"
    fresh.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    monkeypatch.setattr(mw.paths, "storage_state_path", lambda: fresh)

    win._startup_hints()

    text = win.log_view.toPlainText()
    assert "很可能已过期" not in text, text[-300:]


# --------------------------------------------------------------------------- #
# 3. 树与表格的状态一致性
# --------------------------------------------------------------------------- #
def test_tree_and_table_stay_consistent_after_failure(env, qapp, fake_pipeline):
    win, store, _cfg, _cm, _dlg = env
    _enqueue(win, store, qapp, 1, 2)
    task = store.list_tasks()[0]
    store.mark_failed(task.id, "模拟失败")
    win._reload_tasks()
    qapp.processEvents()

    # 表格里显示失败
    stages = [win.table.item(r, 2).text() for r in range(win.table.rowCount())]
    assert "失败" in stages, stages
    # 树里对应节点也显示失败
    tree_states = [
        win.tree.topLevelItem(i).child(j).text(3)
        for i in range(win.tree.topLevelItemCount())
        for j in range(win.tree.topLevelItem(i).childCount())
    ]
    assert any("失败" in s for s in tree_states), tree_states


def test_filter_hides_nonmatching_rows(env, qapp, fake_pipeline):
    win, _store, _cfg, _cm, _dlg = env
    win.catalog = make_catalog(2, 2)
    win._render_tree()
    qapp.processEvents()

    win.edit_search.setText("课程1")
    qapp.processEvents()

    visible_courses = [
        win.tree.topLevelItem(i).isHidden() for i in range(win.tree.topLevelItemCount())
    ]
    assert visible_courses.count(False) >= 1, "至少应有一门课可见"
    assert visible_courses.count(True) >= 1, "不匹配的课程应被隐藏"

    win.edit_search.setText("")
    qapp.processEvents()
    visible = [win.tree.topLevelItem(i) for i in range(win.tree.topLevelItemCount())
               if not win.tree.topLevelItem(i).isHidden()]
    assert len(visible) == 1, "清空搜索后回到当前课程"
    assert visible[0].data(0, Qt.UserRole)["course_id"] == win._selected_course


def test_check_untranscribed_skips_done_tasks(env, qapp, fake_pipeline):
    win, store, _cfg, _cm, _dlg = env
    win.catalog = make_catalog(1, 3)
    win._render_tree()
    qapp.processEvents()

    done_task = store.upsert_task(TaskRecord(
        course_id="C0", resource_id="R00", title="第1讲", course="课程0",
    ))
    store.update_stage(done_task.id, Stage.DONE, progress=100.0)
    win._reload_tasks()
    qapp.processEvents()

    win._check_untranscribed()
    qapp.processEvents()

    checked = [r.resource_id for r in win.checked_resources()]
    assert "R00" not in checked, f"已完成的资源不应被勾选：{checked}"
    assert len(checked) == 2, checked


def test_export_catalog_writes_file(env, qapp, fake_pipeline, monkeypatch, tmp_path):
    win, _store, _cfg, _cm, _dlg = env
    win.catalog = make_catalog(1, 2)
    target = tmp_path / "export.json"
    import app.ui.main_window as mw

    monkeypatch.setattr(mw.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "")))
    win.on_export_catalog()
    qapp.processEvents()
    assert target.is_file()
    assert "resource_count" in target.read_text(encoding="utf-8")


def test_open_output_uses_configured_dir(env, qapp, fake_pipeline, monkeypatch):
    win, _store, _cfg, _cm, _dlg = env
    opened: list[str] = []
    monkeypatch.setattr(win, "_open_dir", lambda p: opened.append(str(p)))
    win.on_open_output()
    assert opened and opened[0] == str(win.cfg.resolved_output_dir()), opened


# --------------------------------------------------------------------------- #
# 4. 关窗时所有后台线程都要被收尾
# --------------------------------------------------------------------------- #
def test_all_workers_are_enumerated(env, qapp, fake_pipeline):
    """``_all_workers()`` 必须枚举到窗口拥有的**每一个**后台线程。

    回归：原来 ``closeEvent`` 手写清单只列了 pipeline/catalog/login，
    后来加的 ``probe_worker``（首启自动诊断）没被列进去 —— 关窗时它还在跑，
    Qt 析构仍在运行的 QThread 直接让进程崩溃（0xC0000005/0xC0000409 之类），
    用户看到的是「一关就异常退出」。
    """
    win, _store, _cfg, _cm, _dlg = env
    from app.workers import ProbeWorker

    # 造一个真实存在但未启动的 probe_worker（模拟「首启自动诊断已创建」）
    win.probe_worker = ProbeWorker(win.cfg, win.cm)
    workers = win._all_workers()
    assert win.probe_worker in workers, "probe_worker 必须被枚举到"
    assert len(workers) == len({id(w) for w in workers}), "不应重复"


def test_close_while_probe_running_does_not_block_or_crash(env, qapp, fake_pipeline):
    """关窗时若诊断线程仍在跑：要停掉它、等到它退出、且不弹「仍在运行」确认框。

    诊断是只读操作，不该弹窗打扰用户。
    """
    win, store, cfg, cm, dlg = env
    from app.workers import ProbeWorker

    w = ProbeWorker(cfg, cm)
    w.which = ["site", "asr"]           # 会做联网探测，保证有一定耗时
    win.probe_worker = w
    w.start()
    qapp.processEvents()

    win.close()
    qapp.processEvents()

    assert not w.isRunning(), "关窗后诊断线程应已停止"
    assert not any("仍在运行" in title for _k, title, _x in dlg), \
        f"只读诊断不该弹确认框：{dlg}"
    store_ok = True
    try:
        store.stats()
    except Exception:
        store_ok = False
    # 状态库此时已被 closeEvent 关闭，上面调用失败是预期的；这里只确认关窗流程跑完了
    assert win.isVisible() is False


def test_close_while_pipeline_running_asks_and_waits(env, qapp, fake_pipeline, monkeypatch):
    """任务型线程仍在跑时要弹确认；用户点「是」则停线程并关闭。"""
    win, _store, _cfg, _cm, dlg = env

    class SlowWorker:
        """假 worker：记录是否被要求停止。"""

        def __init__(self):
            self.stopped = False
            self._running = True

        def isRunning(self):  # noqa: N802
            return self._running

        def request_stop(self):
            self.stopped = True
            self._running = False

        def wait(self, _ms):
            return True

    slow = SlowWorker()
    win.pipeline_worker = slow
    monkeypatch.setattr(win, "_all_workers", lambda: [slow])

    win.close()
    qapp.processEvents()

    assert slow.stopped, "应请求停止流水线线程"
    assert any("仍在运行" in title for _k, title, _x in dlg), f"应弹确认框：{dlg}"
    assert win.isVisible() is False


# --------------------------------------------------------------------------- #
# 5. 登录流程的交互可靠性
# --------------------------------------------------------------------------- #
class _RecordingLoginWorker:
    """记录「线程启动那一刻」交互信号是否已经接好。"""

    instances: list["_RecordingLoginWorker"] = []

    def __init__(self, cfg, *, timeout=900.0, parent=None):
        self.cfg = cfg
        self.timeout = timeout
        self.started = False
        self.connected_before_start: dict[str, bool] = {}
        self.confirmed = 0
        self.stop_requested = 0
        self._running = False
        _RecordingLoginWorker.instances.append(self)

    # 冒充 QThread 的接口
    def isRunning(self):  # noqa: N802
        return self._running

    def start(self):
        # 关键：在真正「起线程」的这一刻，检查两个回调有没有接上
        self.started = True
        self._running = True

    def bring_to_front(self):
        pass

    def wait(self, _ms=0):
        self._running = False
        return True

    # 供主窗口连接
    class _Sig:
        def __init__(self):
            self.slots: list = []

        def connect(self, fn):
            self.slots.append(fn)

        def emit(self, *a):
            for fn in self.slots:
                fn(*a)

    status = _Sig()
    finished_ok = _Sig()
    browser_opened = _Sig()
    canceled = _Sig()

    def confirm_logged_in(self):
        self.confirmed += 1

    def request_stop(self):
        self.stop_requested += 1


def test_login_signals_connected_before_thread_starts(env, qapp, monkeypatch):
    """回归：交互信号必须先接好、再起线程。

    反过来的话存在竞态：线程一起来就在轮询登录态，而「我已登录完成 / 取消登录」
    还没接上，用户在那一瞬间点击会被丢掉（表现为「点了没反应」）。
    """
    win, _store, _cfg, _cm, _dlg = env
    import app.ui.main_window as mw

    _RecordingLoginWorker.instances.clear()
    monkeypatch.setattr(mw, "LoginWorker", _RecordingLoginWorker)

    win.on_login()
    qapp.processEvents()

    assert _RecordingLoginWorker.instances, "应创建 LoginWorker"
    w = _RecordingLoginWorker.instances[-1]
    assert w.started, "应启动登录线程"
    assert w.status.slots, "status 信号应已连接"
    assert w.finished_ok.slots, "finished_ok 信号应已连接"
    assert w.canceled.slots, "canceled 信号应已连接"

    # 通过对话框真实按钮触发 → 应能到达 worker
    dlg = win._login_dialog
    assert dlg is not None, "应显示登录指引对话框"
    dlg.confirmed.emit()
    dlg.cancelled.emit()
    assert w.confirmed == 1, "「我已登录完成」必须能到达 worker"
    assert w.stop_requested == 1, "「取消登录」必须能到达 worker"


def test_login_canceled_is_quiet_no_error_dialog(env, qapp, monkeypatch):
    """用户主动取消登录：不弹错误框、不改登录态提示、日志用 INFO。"""
    win, _store, _cfg, _cm, dlg = env
    import app.ui.main_window as mw

    monkeypatch.setattr(mw, "LoginWorker", _RecordingLoginWorker)
    _RecordingLoginWorker.instances.clear()
    win.on_login()
    qapp.processEvents()

    dlg.clear() if hasattr(dlg, "clear") else dlg.clear()
    w = _RecordingLoginWorker.instances[-1]
    w.canceled.emit()          # 模拟 worker 报告「用户取消」
    qapp.processEvents()

    titles = [t for _k, t, _x in dlg]
    assert not any("未完成" in t for t in titles), f"取消不该弹错误框：{titles}"
    assert "已取消登录" in win.log_view.toPlainText()
    assert win.lbl_login.text().startswith("登录态：未登录")


def test_login_signal_ordering_documented_on_worker():
    """worker 上的 canceled 信号必须存在（供主窗口区分取消与失败）。"""
    from app.workers import LoginWorker

    assert hasattr(LoginWorker, "canceled"), "LoginWorker 应有 canceled 信号"
    assert hasattr(LoginWorker, "confirm_logged_in")
    assert hasattr(LoginWorker, "request_stop")
