"""状态库的**并发安全**与**崩溃恢复**测试。

断点续跑完全建立在 `state.db` 之上，所以这里要回答三个真问题：

    1. **并发**：GUI 的工作线程会同时命中同一个 `StateStore` 单例
       （PipelineWorker 写、UI 读、recover_orphans 启动时写）。
       sqlite 连接跨线程共享，必须真的是安全的。
    2. **崩溃恢复**：应用被强杀（Ctrl+C / 断电）后，数据库要能打开、
       已完成的工作不能丢、卡在中间态的任务要能续跑。
    3. **幂等**：`recover_orphans()` 每次启动都会跑。如果它对同一个任务
       反复「回退」，就会把已经推进的进度反复抹掉（用户看到进度倒退）。

这些都不是理论问题 —— 「断点续跑」是目标的硬性要求，而它此前只有单线程测试。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ecnu_transcribe.store import Stage, StateStore, TaskRecord

ROOT = Path(__file__).resolve().parents[1]


def _task(**kw) -> TaskRecord:
    base = dict(course="课程", course_id="C1", resource_id="R1", title="第1讲")
    base.update(kw)
    return TaskRecord(**base)


# --------------------------------------------------------------------------- #
# 1. 并发
# --------------------------------------------------------------------------- #
def test_concurrent_upsert_creates_exactly_one_task(tmp_path):
    """多个线程同时入队同一资源：只能有一条任务（唯一索引 + 幂等 upsert）。"""
    store = StateStore(tmp_path / "s.db")
    ids: list[int] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            t = store.upsert_task(_task(resource_id="RACE-1"))
            ids.append(t.id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"并发 upsert 出错：{errors}"
    assert len(set(ids)) == 1, f"同一资源产生了多个任务 id：{set(ids)}"
    assert len(store.list_tasks()) == 1
    store.close()


def test_concurrent_stage_updates_do_not_corrupt(tmp_path):
    """多线程并发推进阶段：最终状态合法、进度在范围内、无异常。"""
    store = StateStore(tmp_path / "s.db")
    task = store.upsert_task(_task(resource_id="CONC-1"))
    stops = threading.Event()
    errors: list[Exception] = []
    reads: list[int] = []

    def writer(stage: Stage) -> None:
        try:
            for pct in range(0, 101, 5):
                if stops.is_set():
                    break
                store.update_stage(task.id, stage, progress=float(pct))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            while not stops.is_set():
                t = store.get_task(task.id)
                if t is not None:
                    reads.append(int(t.progress))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(Stage.DOWNLOADING,)),
        threading.Thread(target=writer, args=(Stage.DOWNLOADING,)),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    time.sleep(1.0)
    stops.set()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"并发读写出错：{errors}"
    assert reads, "读线程应至少读到一次"
    assert all(0 <= p <= 100 for p in reads), "进度必须始终在 0~100"
    final = store.get_task(task.id)
    assert final is not None and final.stage == str(Stage.DOWNLOADING)
    store.close()


def test_concurrent_locks_only_one_winner(tmp_path):
    """并发抢锁：只能有一个赢家（否则同一任务会被两个 worker 同时跑）。"""
    store = StateStore(tmp_path / "s.db")
    task = store.upsert_task(_task(resource_id="LOCK-1"))
    winners: list[str] = []
    barrier = threading.Barrier(6)

    def claim(name: str) -> None:
        barrier.wait(timeout=5)
        if store.acquire_lock(task.id, name):
            winners.append(name)

    threads = [threading.Thread(target=claim, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(winners) == 1, f"应只有一个赢家，实际 {winners}"
    store.close()


def test_concurrent_stats_and_events(tmp_path):
    """并发写入时统计与事件表保持一致，不丢事件。"""
    store = StateStore(tmp_path / "s.db")
    n = 20
    tasks = [store.upsert_task(_task(resource_id=f"E{i}")) for i in range(n)]
    errors: list[Exception] = []

    def advance(t: TaskRecord) -> None:
        try:
            store.update_stage(t.id, Stage.PROBING, message="go")
            store.update_stage(t.id, Stage.DOWNLOADING, progress=50)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=advance, args=(t,)) for t in tasks]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert store.stats().get(str(Stage.DOWNLOADING)) == n, store.stats()
    events = store.list_events(limit=1000)
    assert sum(1 for e in events if e["to_stage"] == str(Stage.DOWNLOADING)) == n
    store.close()


# --------------------------------------------------------------------------- #
# 2. 崩溃恢复
# --------------------------------------------------------------------------- #
def test_reopen_after_close_preserves_everything(tmp_path):
    """正常关闭再打开：任务、进度、产物、事件都还在。"""
    db = tmp_path / "s.db"
    s1 = StateStore(db)
    t = s1.upsert_task(_task(resource_id="PERSIST-1"))
    s1.update_stage(t.id, Stage.DOWNLOADING, progress=42.0, audio_path="a.mp3")
    s1.update_stage(t.id, Stage.DONE, progress=100.0, outputs=["x.txt", "x.srt", "x.md"])
    s1.record_artifact(t.id, "txt", "x.txt", sha256="abc", size=12)
    s1.close()

    s2 = StateStore(db)
    got = s2.get_task(t.id)
    assert got is not None
    assert got.stage == str(Stage.DONE)
    assert got.progress == pytest.approx(100.0)
    assert got.outputs == ["x.txt", "x.srt", "x.md"]
    assert got.audio_path == "a.mp3"
    assert s2.list_artifacts(t.id)[0]["sha256"] == "abc"
    assert s2.list_events(t.id), "事件历史应保留"
    s2.close()


def test_hard_kill_then_recover(tmp_path):
    """模拟被强杀（SIGKILL 等价）：子进程写一半被杀，父进程要能打开并续跑。"""
    db = tmp_path / "crash.db"
    partial_audio = tmp_path / "partial.mp3"
    partial_audio.write_bytes(b"x" * 4096)      # 已下好一部分音频
    code = f'''
import sys, time
sys.path.insert(0, r"{ROOT / 'src'}")
from ecnu_transcribe.store import StateStore, TaskRecord, Stage
s = StateStore(r"{db}")
t = s.upsert_task(TaskRecord(course_id="C1", resource_id="CRASH-1", title="崩溃任务"))
s.update_stage(t.id, Stage.DOWNLOADING, progress=37.0, audio_path=r"{partial_audio}")
print("READY", flush=True)
time.sleep(60)      # 等父进程强杀
'''
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    try:
        line = proc.stdout.readline() if proc.stdout else ""
        assert "READY" in line, f"子进程未就绪：{line}"
    finally:
        proc.kill()          # 强杀，不给它 close() 的机会
        proc.wait(timeout=10)

    # 父进程打开同一个库：必须能读、能续跑
    store = StateStore(db)
    tasks = store.list_tasks()
    assert len(tasks) == 1, f"崩溃后应还能读到任务：{tasks}"
    assert tasks[0].stage == str(Stage.DOWNLOADING)
    assert tasks[0].audio_path == str(partial_audio)

    fixed = store.recover_orphans()
    assert len(fixed) == 1
    # 有音频 → 回到 audio_ready（不重下）
    assert fixed[0].stage == str(Stage.AUDIO_READY), fixed[0].stage
    # 还能继续推进并完成
    store.update_stage(tasks[0].id, Stage.TRANSCRIBING, progress=95.0)
    store.update_stage(tasks[0].id, Stage.DONE, progress=100.0, outputs=["a.txt"])
    assert store.get_task(tasks[0].id).stage == str(Stage.DONE)
    store.close()


def test_wal_mode_is_enabled_and_survives(tmp_path):
    """WAL 模式要真的开起来（崩溃恢复与并发读写都依赖它）。"""
    store = StateStore(tmp_path / "wal.db")
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal", mode
    store.close()


def test_stale_lock_from_previous_run_is_cleared(tmp_path):
    """上次运行留下的锁（进程被强杀）不能永久阻塞任务。"""
    db = tmp_path / "lock.db"
    s1 = StateStore(db)
    t = s1.upsert_task(_task(resource_id="STALE-1"))
    assert s1.acquire_lock(t.id, "dead-worker")
    s1.close()   # 模拟进程消失，锁没释放

    s2 = StateStore(db)
    assert not s2.acquire_lock(t.id, "new-worker"), "残留锁应仍然存在（需要显式清理）"
    s2.clear_all_locks()          # GUI 启动/退出时会调用
    assert s2.acquire_lock(t.id, "new-worker"), "清理后应能拿到锁"
    s2.close()


def test_recover_orphans_is_idempotent(tmp_path):
    """``recover_orphans()`` 每次启动都跑，重复调用不能反复抹掉进度。"""
    db = tmp_path / "idem.db"
    store = StateStore(db)
    t = store.upsert_task(_task(resource_id="IDEM-1"))
    store.update_stage(t.id, Stage.DOWNLOADING, progress=66.0)
    first = store.recover_orphans()
    assert len(first) == 1
    after_first = store.get_task(t.id)
    assert after_first.stage == str(Stage.PENDING)

    # 用户重新跑了一段，进到 transcribing
    store.update_stage(t.id, Stage.TRANSCRIBING, progress=92.0, transcript_path="t.json")
    # 再次启动 → 又要回退（这次应回到 pending，且不报错）
    second = store.recover_orphans()
    assert len(second) == 1
    # 第三次调用：已经是 pending（非中间态），不该再被处理
    third = store.recover_orphans()
    assert third == [], f"已回退过的任务不应再被反复处理：{third}"
    assert store.get_task(t.id).stage == str(Stage.PENDING)
    store.close()


def test_recover_orphans_never_touches_terminal_states(tmp_path):
    """已完成 / 已失败 / 已取消 的任务绝不能被启动恢复改动。"""
    store = StateStore(tmp_path / "term.db")
    done = store.upsert_task(_task(resource_id="T-DONE"))
    store.update_stage(done.id, Stage.DONE, progress=100.0, outputs=["a.txt"])
    failed = store.upsert_task(_task(resource_id="T-FAIL"))
    store.update_stage(failed.id, Stage.FAILED, force=True, error="boom")
    canceled = store.upsert_task(_task(resource_id="T-CANCEL"))
    store.update_stage(canceled.id, Stage.CANCELED, force=True)

    fixed = store.recover_orphans()
    assert fixed == [], fixed
    assert store.get_task(done.id).stage == str(Stage.DONE)
    assert store.get_task(done.id).outputs == ["a.txt"]
    assert store.get_task(failed.id).error == "boom"
    assert store.get_task(canceled.id).stage == str(Stage.CANCELED)
    store.close()


def test_recover_orphans_prefers_audio_ready_when_audio_exists(tmp_path):
    """有音频文件的任务回退到 audio_ready（不重下）；音频不存在则回退到 pending。"""
    store = StateStore(tmp_path / "audio.db")
    audio = tmp_path / "ok.mp3"
    audio.write_bytes(b"x" * 2048)

    with_audio = store.upsert_task(_task(resource_id="A-EXISTS"))
    store.update_stage(with_audio.id, Stage.DOWNLOADING, audio_path=str(audio))

    without = store.upsert_task(_task(resource_id="A-GONE"))
    store.update_stage(without.id, Stage.DOWNLOADING, audio_path=str(tmp_path / "gone.mp3"))

    fixed = {t.id: t for t in store.recover_orphans()}
    assert fixed[with_audio.id].stage == str(Stage.AUDIO_READY)
    assert fixed[without.id].stage == str(Stage.PENDING)
    store.close()


# --------------------------------------------------------------------------- #
# 3. 数据库损坏时的行为
# --------------------------------------------------------------------------- #
def test_corrupt_db_reports_clearly(tmp_path):
    """库文件被截断/损坏时，应抛出可读异常而不是静默给出空清单。"""
    db = tmp_path / "broken.db"
    db.write_bytes(b"this is not a sqlite database at all" * 10)
    with pytest.raises(sqlite3.DatabaseError):
        StateStore(db)


def test_truncated_db_does_not_silently_lose_tasks(tmp_path):
    """先正常写入，再截断文件：重新打开要么报错，要么仍能读出任务 —— 不能假装「没有任务」。"""
    db = tmp_path / "trunc.db"
    store = StateStore(db)
    store.upsert_task(_task(resource_id="TRUNC-1"))
    store.close()

    data = db.read_bytes()
    db.write_bytes(data[: max(1, len(data) // 3)])
    try:
        store2 = StateStore(db)
    except sqlite3.DatabaseError:
        return  # 明确报错 = 可接受（用户能看到）
    tasks = store2.list_tasks()
    assert tasks, "截断后不应静默返回空清单（那会让用户以为任务丢了）"
    store2.close()
