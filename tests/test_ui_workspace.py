"""新版工作区行为：稳定选择、显式排队、运行隔离与可恢复操作。"""
from pathlib import Path

from PySide6.QtCore import Qt

from test_gui_recovery import env, qapp, dialogs, make_catalog, fake_pipeline, run_worker
from app.workers import PipelineWorker, QueueItem
from ecnu_transcribe.store import Stage


def populate(win, courses=2, resources=2):
    win.catalog = make_catalog(courses, resources)
    win._render_tree()
    return win.tree.topLevelItem(0).child(0)


def test_refresh_never_enqueues_unselected_recordings(env):
    win, store, *_ = env
    win._on_catalog_finished(make_catalog(), "")
    assert store.list_tasks() == []
    assert win.course_list.count() == 2


def test_selection_survives_course_switch_search_and_refresh(env):
    win, store, *_ = env
    first = populate(win)
    first.setCheckState(0, Qt.Checked)
    win.course_list.setCurrentRow(1)
    win.tree.topLevelItem(1).child(1).setCheckState(0, Qt.Checked)
    selected = {r.unique_key for r in win.checked_resources()}
    win.edit_search.setText("不存在")
    assert "筛选外 2" in win.lbl_checked.text()
    win._render_tree()
    assert {r.unique_key for r in win.checked_resources()} == selected
    win.on_enqueue_checked()
    assert {f"{t.course_id}::{t.resource_id}" for t in store.list_tasks()} == selected
    win._clear_selection()
    assert win.checked_resources() == []


def test_select_current_results_respects_parent_and_same_resource_ids(env):
    win, store, *_ = env
    cat = make_catalog(2, 1)
    for course in cat.courses:
        course.course_name = "同名课程"
        course.resources[0].resource_id = "same-id"
    win.catalog = cat
    win._render_tree()
    win._set_all_checked(True)
    assert [r.course_id for r in win.checked_resources()] == ["C0"]
    win.on_enqueue_checked()
    task = store.list_tasks()[0]
    store.update_stage(task.id, Stage.DONE)
    win._clear_selection()
    win.course_list.setCurrentRow(1)
    win._check_untranscribed()
    assert [r.course_id for r in win.checked_resources()] == ["C1"]


def test_filter_actions_use_task_id_and_never_target_hidden_selection(env):
    win, store, *_ = env
    populate(win)
    win._enqueue_resources(win.catalog.resources)
    tasks = store.list_tasks()
    store.update_stage(tasks[0].id, Stage.DONE)
    store.mark_failed(tasks[1].id, "测试失败")
    win._reload_tasks()
    win.table.selectRow(win._task_row_of(tasks[0].id))
    win.task_filter.setCurrentIndex(4)
    assert win._selected_task_ids() == []
    win.table.selectRow(win._task_row_of(tasks[1].id))
    win.on_requeue_selected(force=False)
    assert store.get_task(tasks[1].id).stage == "pending"
    assert store.get_task(tasks[0].id).stage == "done"


def test_readding_done_recording_preserves_result(env):
    win, store, *_ = env
    child = populate(win, 1, 1)
    child.setCheckState(0, Qt.Checked)
    win.on_enqueue_checked()
    task = store.list_tasks()[0]
    store.update_stage(task.id, Stage.DONE, outputs=["saved.txt"])
    win.on_enqueue_checked()
    assert store.get_task(task.id).outputs == ["saved.txt"]
    assert win._pending_tasks() == []


def test_restored_audio_ready_is_startable(env):
    win, store, *_ = env
    populate(win, 1, 1).setCheckState(0, Qt.Checked)
    win.on_enqueue_checked()
    task = store.list_tasks()[0]
    store.update_stage(task.id, Stage.AUDIO_READY)
    assert [t.id for t in win._pending_tasks()] == [task.id]


def test_new_batch_is_snapshot_and_new_items_stay_pending(env):
    win, store, cfg, cm, _ = env
    populate(win, 1, 2).setCheckState(0, Qt.Checked)
    win.on_enqueue_checked()
    first = store.list_tasks()[0]
    items = [QueueItem(first.id, win.catalog.resources[0])]
    cm.set_secret("asr_api_key", "first-secret")
    worker = PipelineWorker(cfg, cm, store, items)
    old_model = worker.cfg.asr_model
    cfg.asr_model = "changed-model"
    cm.set_secret("asr_api_key", "new-secret")
    items.clear()
    win._batch_active = True
    win.tree.topLevelItem(0).child(1).setCheckState(0, Qt.Checked)
    win.on_transcribe_selected()
    assert len(worker.items) == 1
    assert worker.cfg.asr_model == old_model
    assert worker.cm.secret("asr_api_key") == "first-secret"
    assert len(store.list_tasks()) == 2
    assert win.pipeline_worker is None
    win._batch_active = False


def test_running_batch_blocks_destructive_task_actions(env):
    win, store, *_ = env
    populate(win, 1, 1).setCheckState(0, Qt.Checked)
    win.on_enqueue_checked()
    win.table.selectRow(0)
    win._batch_active = True
    win.on_remove_selected_tasks()
    win.on_requeue_selected(force=True)
    assert len(store.list_tasks()) == 1
    assert store.list_tasks()[0].stage == "pending"
    assert "先停止" in win.notice_text.text()
    win._batch_active = False


def test_output_open_uses_actual_path_and_missing_file_never_created(env, tmp_path, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    win, *_ = env
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()) or True)
    path = tmp_path / "中文文稿.txt"
    path.write_text("课堂内容", encoding="utf-8")
    win._open_existing(path)
    assert Path(opened[0]) == path
    missing = tmp_path / "不存在" / "文稿.txt"
    win._open_existing(missing)
    assert not missing.parent.exists()
    assert "不存在" in win.notice_text.text()


def test_log_is_collapsed_and_page_switch_does_not_start_work(env):
    win, *_ = env
    win._append_log("INFO", "测试消息")
    assert win.log_panel.isHidden()
    win.pages.setCurrentIndex(1)
    win.btn_logs.setChecked(True)
    assert "测试消息" in win.log_view.toPlainText()
    win.pages.setCurrentIndex(0)
    assert win.pipeline_worker is None


def test_loopback_without_key_allowed_but_lookalike_rejected(env):
    win, _, cfg, *_ = env
    cfg.asr_provider = "openai_compatible"
    cfg.asr_base_url = "http://127.0.0.1:8000/v1"
    assert not win._asr_not_configured()
    cfg.asr_base_url = "https://127.0.0.1.example.com/v1"
    assert win._asr_not_configured()


def test_thousand_recordings_search_and_hidden_selection(env, qapp):
    win, *_ = env
    first = populate(win, 20, 50)
    first.setCheckState(0, Qt.Checked)
    last = win.tree.topLevelItem(19).child(49)
    last.setCheckState(0, Qt.Checked)
    chosen = {r.unique_key for r in win.checked_resources()}
    win.edit_search.setText("没有匹配的录播")
    qapp.processEvents()
    assert len(chosen) == 2
    assert {r.unique_key for r in win.checked_resources()} == chosen
    assert win.catalog_empty.isVisible()
    win.edit_search.clear()
    win._render_tree()
    assert len(win.catalog.resources) == 1000
    assert {r.unique_key for r in win.checked_resources()} == chosen


def test_returned_auth_failure_stops_batch_and_requests_login(env, monkeypatch):
    import app.workers as aw
    win, store, *_ = env
    populate(win, 1, 2)
    win._set_all_checked(True)
    win.on_enqueue_checked()
    called, relogin = [], []
    def fail(self, task, resource, *, force=False):
        called.append(task.id)
        store.mark_failed(task.id, "登录态失效：请重新登录")
        return store.get_task(task.id)
    monkeypatch.setattr(aw.Pipeline, "run", fail)
    worker = PipelineWorker(win.cfg, win.cm, store, [
        QueueItem(t.id, win._resource_index[f"{t.course_id}::{t.resource_id}"])
        for t in store.list_tasks()
    ])
    worker.request_relogin.connect(relogin.append)
    worker.run()
    assert len(called) == 1
    assert relogin and "登录态失效" in relogin[0]


def test_force_retry_reaches_pipeline_and_clears_flag_after_success(env, qapp, fake_pipeline, monkeypatch):
    import app.workers as aw
    win, store, *_ = env
    populate(win, 1, 1).setCheckState(0, Qt.Checked)
    win.on_enqueue_checked()
    win.table.selectRow(0)
    win.on_requeue_selected(force=True)
    task = store.list_tasks()[0]
    assert task.meta["force_retranscribe"]
    seen = []
    original = aw.Pipeline.run
    def capture(self, task, resource, *, force=False):
        seen.append(force)
        return original(self, task, resource, force=force)
    monkeypatch.setattr(aw.Pipeline, "run", capture)
    worker = PipelineWorker(win.cfg, win.cm, store, [QueueItem(task.id, win.catalog.resources[0])])
    worker.run()
    assert seen == [True]
    assert "force_retranscribe" not in store.get_task(task.id).meta
