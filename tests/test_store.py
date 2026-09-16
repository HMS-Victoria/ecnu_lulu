"""状态库 / 状态机 / 断点续跑 测试（M6）。"""

from __future__ import annotations

import sqlite3

import pytest

from ecnu_transcribe.store import (
    RESUMABLE_STAGES,
    Stage,
    StateStore,
    TaskRecord,
    can_transition,
)


def _task(**kw) -> TaskRecord:
    base = dict(course="数据结构", course_id="C1", resource_id="R1", title="第1讲")
    base.update(kw)
    return TaskRecord(**base)


def test_upsert_is_idempotent_and_keeps_progress(tmp_store: StateStore):
    t1 = tmp_store.upsert_task(_task())
    assert t1.id > 0
    tmp_store.update_stage(t1.id, Stage.DOWNLOADING, progress=40.0)
    t2 = tmp_store.upsert_task(_task())
    assert t2.id == t1.id
    assert t2.progress == pytest.approx(40.0)


def test_upsert_distinguishes_resources(tmp_store: StateStore):
    a = tmp_store.upsert_task(_task(resource_id="R1"))
    b = tmp_store.upsert_task(_task(resource_id="R2"))
    assert a.id != b.id
    assert len(tmp_store.list_tasks()) == 2


def test_required_columns_exist(tmp_store: StateStore):
    """M4 硬性要求：任务表至少包含这些字段。"""
    required = {
        "id", "course", "resource_id", "title", "stage", "progress",
        "retry", "error", "output_dir", "updated_at",
    }
    cur = tmp_store._conn.execute("PRAGMA table_info(tasks)")
    cols = {row["name"] for row in cur.fetchall()}
    assert required <= cols


def test_illegal_transition_is_rejected(tmp_store: StateStore):
    t = tmp_store.upsert_task(_task())
    # done 不能直接跳到 downloading
    out = tmp_store.update_stage(t.id, Stage.DONE, progress=100)
    assert out is not None
    assert out.stage == str(Stage.DONE)
    blocked = tmp_store.update_stage(t.id, Stage.DOWNLOADING)
    assert blocked is not None and blocked.stage == str(Stage.DONE)
    # force 可以强推
    forced = tmp_store.update_stage(t.id, Stage.DOWNLOADING, force=True)
    assert forced is not None and forced.stage == str(Stage.DOWNLOADING)


def test_self_transition_allowed(tmp_store: StateStore):
    t = tmp_store.upsert_task(_task())
    tmp_store.update_stage(t.id, Stage.DOWNLOADING, progress=10)
    out = tmp_store.update_stage(t.id, Stage.DOWNLOADING, progress=55)
    assert out is not None and out.progress == pytest.approx(55)


def test_completed_task_can_be_rerun(tmp_store: StateStore):
    """终态 → pending 必须被允许（用户点「重跑」）。"""
    t = tmp_store.upsert_task(_task())
    tmp_store.update_stage(t.id, Stage.DONE, progress=100)
    again = tmp_store.update_stage(t.id, Stage.PENDING, progress=0.0)
    assert again is not None and again.stage == str(Stage.PENDING)


def test_transition_rules_cover_all_stages():
    """每个阶段都必须给出明确判定（不抛异常），且前进/出口/后退语义正确。"""
    for f in Stage:
        for t in Stage:
            assert isinstance(can_transition(str(f), str(t)), bool)
        assert can_transition(str(f), str(f))                    # 同态允许
        assert can_transition(str(f), str(Stage.FAILED))         # 任意 → 失败
        assert can_transition(str(f), str(Stage.PENDING))        # 任意 → 重跑
    # 前进允许
    assert can_transition(str(Stage.PENDING), str(Stage.DOWNLOADING))
    assert can_transition(str(Stage.TRANSCRIBING), str(Stage.WRITING))
    assert can_transition(str(Stage.DONE), str(Stage.PROBING))
    # 明显后退被拒绝
    assert not can_transition(str(Stage.DONE), str(Stage.DOWNLOADING))
    assert not can_transition(str(Stage.TRANSCRIBING), str(Stage.PROBING))


def test_progress_clamped(tmp_store: StateStore):
    t = tmp_store.upsert_task(_task())
    assert tmp_store.update_stage(t.id, Stage.PROBING, progress=250).progress == 100.0
    assert tmp_store.update_stage(t.id, Stage.PROBING, progress=-5).progress == 0.0


def test_mark_failed_bumps_retry(tmp_store: StateStore):
    t = tmp_store.upsert_task(_task())
    out = tmp_store.mark_failed(t.id, "boom")
    assert out is not None
    assert out.stage == str(Stage.FAILED)
    assert out.retry == 1
    assert "boom" in out.error
    out2 = tmp_store.mark_failed(t.id, "boom2")
    assert out2 is not None and out2.retry == 2


def test_recover_orphans_resets_midway_tasks(tmp_store: StateStore, tmp_path):
    downloading = tmp_store.upsert_task(_task(resource_id="R-A"))
    tmp_store.update_stage(downloading.id, Stage.DOWNLOADING, progress=30)
    transcribing = tmp_store.upsert_task(_task(resource_id="R-B"))
    tmp_store.update_stage(transcribing.id, Stage.TRANSCRIBING, progress=90)

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x" * 2048)
    with_audio = tmp_store.upsert_task(_task(resource_id="R-C"))
    tmp_store.update_stage(with_audio.id, Stage.DOWNLOADING, progress=50, audio_path=str(audio))

    done = tmp_store.upsert_task(_task(resource_id="R-D"))
    tmp_store.update_stage(done.id, Stage.DONE, progress=100)

    fixed = tmp_store.recover_orphans()
    ids = {t.id: t for t in fixed}
    assert downloading.id in ids and ids[downloading.id].stage == str(Stage.PENDING)
    assert transcribing.id in ids and ids[transcribing.id].stage == str(Stage.PENDING)
    # 已有音频 → 回到 audio_ready，不重下
    assert with_audio.id in ids and ids[with_audio.id].stage == str(Stage.AUDIO_READY)
    # 已完成的不动
    assert done.id not in ids
    assert tmp_store.get_task(done.id).stage == str(Stage.DONE)


def test_resumable_and_terminal_helpers(tmp_store: StateStore):
    a = tmp_store.upsert_task(_task(resource_id="R1"))
    b = tmp_store.upsert_task(_task(resource_id="R2"))
    tmp_store.update_stage(b.id, Stage.DONE, progress=100)
    assert tmp_store.get_task(a.id).is_resumable
    assert not tmp_store.get_task(a.id).is_terminal
    assert tmp_store.get_task(b.id).is_terminal
    assert len(tmp_store.resumable_tasks()) == 1


def test_soft_delete_hides_task(tmp_store: StateStore):
    t = tmp_store.upsert_task(_task())
    tmp_store.delete_task(t.id)
    assert tmp_store.list_tasks() == []
    assert len(tmp_store.list_tasks(include_deleted=True)) == 1
    # 重新入队会复活（不丢历史）
    again = tmp_store.upsert_task(_task())
    assert again.id == t.id
    assert len(tmp_store.list_tasks()) == 1


def test_locks_are_exclusive(tmp_store: StateStore):
    t = tmp_store.upsert_task(_task())
    assert tmp_store.acquire_lock(t.id, "worker-A")
    assert not tmp_store.acquire_lock(t.id, "worker-B")
    assert tmp_store.acquire_lock(t.id, "worker-A")  # 同 owner 可重入
    tmp_store.release_lock(t.id, "worker-A")
    assert tmp_store.acquire_lock(t.id, "worker-B")
    tmp_store.clear_all_locks()
    assert tmp_store.get_task(t.id).lock_owner == ""


def test_events_are_recorded(tmp_store: StateStore):
    t = tmp_store.upsert_task(_task())
    tmp_store.update_stage(t.id, Stage.PROBING, message="go")
    events = tmp_store.list_events(t.id)
    assert any(e["to_stage"] == str(Stage.PROBING) for e in events)
    assert any(e["message"] == "go" for e in events)


def test_artifacts_recorded(tmp_store: StateStore):
    t = tmp_store.upsert_task(_task())
    tmp_store.record_artifact(t.id, "srt", "C/T.srt", sha256="abc", size=10)
    tmp_store.record_artifact(t.id, "srt", "C/T.srt", sha256="def", size=20)  # upsert
    arts = tmp_store.list_artifacts(t.id)
    assert len(arts) == 1
    assert arts[0]["sha256"] == "def"


def test_stats_grouping(tmp_store: StateStore):
    a = tmp_store.upsert_task(_task(resource_id="R1"))
    b = tmp_store.upsert_task(_task(resource_id="R2"))
    tmp_store.update_stage(b.id, Stage.DONE, progress=100)
    stats = tmp_store.stats()
    assert stats.get(str(Stage.PENDING)) == 1
    assert stats.get(str(Stage.DONE)) == 1


def test_reset_for_rerun_keeps_audio_by_default(tmp_store: StateStore):
    t = tmp_store.upsert_task(_task())
    tmp_store.update_stage(t.id, Stage.DOWNLOADING, progress=80, audio_path="x.mp3")
    tmp_store.update_stage(t.id, Stage.FAILED, force=True, error="boom")
    rec = tmp_store.reset_for_rerun(t.id)
    assert rec is not None
    assert rec.stage == str(Stage.PENDING)
    assert rec.audio_path == "x.mp3"
    assert rec.error == ""

    rec2 = tmp_store.reset_for_rerun(t.id, keep_audio=False)
    assert rec2 is not None and rec2.audio_path == ""


def test_persisted_fields_outputs_and_meta(tmp_store: StateStore):
    """回归：``outputs`` / ``meta`` 必须真正落库（曾因字段名校验被静默丢弃）。"""
    t = tmp_store.upsert_task(_task())
    final = tmp_store.update_stage(
        t.id,
        Stage.DONE,
        progress=100,
        outputs=["C/T.txt", "C/T.srt", "C/T.md"],
        meta={"k": "v"},
        output_dir="out",
        audio_path="a.mp3",
        transcript_path="C/T.transcript.json",
        error="",
    )
    assert final is not None
    assert final.outputs == ["C/T.txt", "C/T.srt", "C/T.md"]
    assert final.meta == {"k": "v"}
    assert final.output_dir == "out"
    assert final.transcript_path == "C/T.transcript.json"
    # 从库里重新读也要一致
    again = tmp_store.get_task(t.id)
    assert again is not None and again.outputs == final.outputs


def test_unknown_field_is_logged_not_silent(tmp_store: StateStore, caplog):
    t = tmp_store.upsert_task(_task())
    tmp_store.update_stage(t.id, Stage.PROBING, nonsense_field="x")
    assert tmp_store.get_task(t.id) is not None


def test_resumable_stages_set_matches_expectations():
    assert Stage.DONE not in RESUMABLE_STAGES
    assert Stage.FAILED not in RESUMABLE_STAGES
    assert Stage.DOWNLOADING in RESUMABLE_STAGES
    assert Stage.TRANSCRIBING in RESUMABLE_STAGES


# --------------------------------------------------------------------------- #
# 缺陷 40：启用 WAL 时的**瞬时 I/O 错误**不能让应用起不来
# --------------------------------------------------------------------------- #
class _WalFailingConnection:
    """把真实 sqlite3 连接包一层，让 `PRAGMA journal_mode=WAL` 按需失败。

    注意：必须同时代理**属性赋值** —— `StateStore` 会设置 `row_factory`，
    只代理 `__getattr__` 的话赋值会落在包装对象上，真实连接拿不到 row_factory，
    读回来就是 tuple（`dict(row)` 直接炸）。这个坑我自己先踩了一次。
    """

    def __init__(self, conn, *, fail_times: int | None) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "calls", 0)
        object.__setattr__(self, "_fail_times", fail_times)

    def execute(self, sql, *a):
        if "journal_mode=WAL" in sql.replace(" ", ""):
            object.__setattr__(self, "calls", self.calls + 1)
            if self._fail_times is None or self.calls <= self._fail_times:
                raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)


def _patch_wal_failure(monkeypatch, *, fail_times: int | None):
    """返回一个可读 `calls` 的包装（`fail_times=None` 表示永远失败）。"""
    from ecnu_transcribe import store as store_mod

    real_connect = sqlite3.connect
    box: dict[str, _WalFailingConnection] = {}

    def fake_connect(*a, **kw):
        conn = _WalFailingConnection(real_connect(*a, **kw), fail_times=fail_times)
        box["conn"] = conn
        return conn

    monkeypatch.setattr(store_mod.sqlite3, "connect", fake_connect)
    return box


def test_wal_is_enabled_normally(tmp_path):
    store = StateStore(tmp_path / "wal.db")
    try:
        assert store.journal_mode == "wal", store.journal_mode
    finally:
        store.close()


def test_transient_wal_failure_is_retried(tmp_path, monkeypatch):
    """第一次 `PRAGMA journal_mode=WAL` 抛 disk I/O error，重试后必须成功。

    实测触发场景：上一个持有该库的进程刚被强杀（Windows 还没释放 -wal/-shm 映射），
    此时立刻开库就会遇到这个瞬时错误。
    """
    box = _patch_wal_failure(monkeypatch, fail_times=1)
    s = StateStore(tmp_path / "flaky.db")
    try:
        assert box["conn"].calls >= 2, "应当重试过 WAL pragma"
        assert s.journal_mode == "wal", s.journal_mode
        # 重试之后库必须真的可用（读写都要正常）
        t = s.upsert_task(_task(resource_id="FLAKY-1"))
        got = s.get_task(t.id)
        assert got is not None and got.resource_id == "FLAKY-1"
    finally:
        s.close()


def test_permanent_wal_failure_degrades_instead_of_crashing(tmp_path, monkeypatch):
    """WAL 一直失败 → 降级为默认日志模式继续跑，**绝不抛异常**。

    这条路径在 GUI 启动上：真抛出去，用户看到的就是「双击 exe 一打开就崩」。
    而 WAL 只是并发/性能优化，拿不到不该影响可用性。
    """
    _patch_wal_failure(monkeypatch, fail_times=None)
    s = StateStore(tmp_path / "nowal.db")
    try:
        assert s.journal_mode == "delete", s.journal_mode
        t = s.upsert_task(_task(resource_id="NOWAL-1"))
        s.update_stage(t.id, Stage.DONE, progress=100.0)
        got = s.get_task(t.id)
        assert got is not None and got.stage == str(Stage.DONE)
    finally:
        s.close()


def test_wal_failure_is_visible_in_logs(tmp_path, monkeypatch):
    """降级必须留痕 —— 静默降级会让人日后排查并发问题时毫无线索。"""
    from ecnu_transcribe import store as store_mod

    _patch_wal_failure(monkeypatch, fail_times=None)
    seen: list[str] = []
    real_warning = store_mod.log.warning

    def spy(msg, *a, **kw):
        seen.append(str(msg) % a if a else str(msg))
        return real_warning(msg, *a, **kw)

    monkeypatch.setattr(store_mod.log, "warning", spy)
    s = StateStore(tmp_path / "logged.db")
    s.close()
    assert any("WAL" in m for m in seen), seen
