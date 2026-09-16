"""sqlite3 状态库：任务、产物、断点续跑。

表结构
------
``tasks``  （硬性要求字段全部包含，另加断点续跑所需列）
    id / course / resource_id / title / stage / progress / retry / error
    / output_dir / updated_at
    以及：course_id / duration_sec / play_url / audio_path / audio_sha256
    / transcript_path / outputs_json / started_at / finished_at / attempts
    / lock_owner / deleted

``artifacts``
    每次产出的文件（txt/srt/md）路径 + sha256，便于「只增不改」审计。

``events``
    状态机迁移审计（谁在什么时候把任务推进到哪个阶段），日志可追溯。

阶段（stage）状态机::

    pending → probing → downloading → audio_ready → splitting → transcribing
            → post_processing → writing → done
    任意阶段 → failed / skipped / canceled
    done / failed / canceled → pending（重跑）

所有写操作都在一个 ``threading.RLock`` + 独立连接下完成（GUI 多线程安全）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import paths
from .logbus import get_logger

log = get_logger("store")


class Stage(StrEnum):
    PENDING = "pending"
    PROBING = "probing"
    DOWNLOADING = "downloading"
    AUDIO_READY = "audio_ready"
    SPLITTING = "splitting"
    TRANSCRIBING = "transcribing"
    POST_PROCESSING = "post_processing"
    WRITING = "writing"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"
    SKIPPED = "skipped"


TERMINAL_STAGES = {Stage.DONE, Stage.FAILED, Stage.CANCELED, Stage.SKIPPED}

#: 阶段序号：用于判定「前进 / 后退」。同一阶段总是允许（进度更新）。
_STAGE_ORDER: dict[str, int] = {
    Stage.PENDING: 0,
    Stage.PROBING: 1,
    Stage.DOWNLOADING: 2,
    Stage.AUDIO_READY: 3,
    Stage.SPLITTING: 4,
    Stage.TRANSCRIBING: 5,
    Stage.POST_PROCESSING: 6,
    Stage.WRITING: 7,
    Stage.DONE: 8,
}
#: 任意阶段都允许迁移到的「出口」状态
_ANY_TO = {Stage.FAILED, Stage.CANCELED, Stage.SKIPPED, Stage.PENDING}
#: 允许的「后退」（重跑 / 续跑需要）
_BACKWARD_ALLOWED = {
    (Stage.AUDIO_READY, Stage.DOWNLOADING),   # 音频损坏，重新拉流
    (Stage.TRANSCRIBING, Stage.AUDIO_READY),  # 重新切分
    (Stage.WRITING, Stage.POST_PROCESSING),   # 只重跑 LLM
    (Stage.POST_PROCESSING, Stage.TRANSCRIBING),
    (Stage.WRITING, Stage.TRANSCRIBING),
    (Stage.DONE, Stage.PROBING),
    (Stage.FAILED, Stage.PROBING),
    (Stage.CANCELED, Stage.PROBING),
    (Stage.SKIPPED, Stage.PROBING),
}


def can_transition(from_stage: str, to_stage: str) -> bool:
    """状态迁移合法性：同一状态总是允许；任意状态可到出口状态；

    其余情况要求「前进」，外加少量显式允许的「后退」（重跑 / 续跑）。
    """
    if from_stage == to_stage:
        return True
    try:
        f, t = Stage(from_stage), Stage(to_stage)
    except ValueError:
        return False
    if t in _ANY_TO:
        return True
    if (f, t) in _BACKWARD_ALLOWED:
        return True
    return _STAGE_ORDER.get(f, -1) < _STAGE_ORDER.get(t, -1)

RESUMABLE_STAGES = {
    Stage.PENDING,
    Stage.PROBING,
    Stage.DOWNLOADING,
    Stage.AUDIO_READY,
    Stage.SPLITTING,
    Stage.TRANSCRIBING,
    Stage.POST_PROCESSING,
    Stage.WRITING,
}


@dataclass
class TaskRecord:
    id: int = 0
    course: str = ""
    course_id: str = ""
    resource_id: str = ""
    title: str = ""
    stage: str = Stage.PENDING
    progress: float = 0.0
    retry: int = 0
    error: str = ""
    output_dir: str = ""
    updated_at: float = 0.0
    duration_sec: float = 0.0
    play_url: str = ""
    audio_path: str = ""
    audio_sha256: str = ""
    transcript_path: str = ""
    outputs: list[str] = field(default_factory=list)
    attempts: int = 0
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    lock_owner: str = ""
    deleted: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.stage in {str(s) for s in TERMINAL_STAGES}

    @property
    def is_resumable(self) -> bool:
        return self.stage in {str(s) for s in RESUMABLE_STAGES}

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["outputs_json"] = json.dumps(d.pop("outputs"), ensure_ascii=False)
        d["meta_json"] = json.dumps(d.pop("meta"), ensure_ascii=False)
        return d


_TASK_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("course", "TEXT NOT NULL DEFAULT ''"),
    ("course_id", "TEXT NOT NULL DEFAULT ''"),
    ("resource_id", "TEXT NOT NULL DEFAULT ''"),
    ("title", "TEXT NOT NULL DEFAULT ''"),
    ("stage", "TEXT NOT NULL DEFAULT 'pending'"),
    ("progress", "REAL NOT NULL DEFAULT 0"),
    ("retry", "INTEGER NOT NULL DEFAULT 0"),
    ("error", "TEXT NOT NULL DEFAULT ''"),
    ("output_dir", "TEXT NOT NULL DEFAULT ''"),
    ("updated_at", "REAL NOT NULL DEFAULT 0"),
    ("duration_sec", "REAL NOT NULL DEFAULT 0"),
    ("play_url", "TEXT NOT NULL DEFAULT ''"),
    ("audio_path", "TEXT NOT NULL DEFAULT ''"),
    ("audio_sha256", "TEXT NOT NULL DEFAULT ''"),
    ("transcript_path", "TEXT NOT NULL DEFAULT ''"),
    ("outputs_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("created_at", "REAL NOT NULL DEFAULT 0"),
    ("started_at", "REAL NOT NULL DEFAULT 0"),
    ("finished_at", "REAL NOT NULL DEFAULT 0"),
    ("lock_owner", "TEXT NOT NULL DEFAULT ''"),
    ("deleted", "INTEGER NOT NULL DEFAULT 0"),
    ("meta_json", "TEXT NOT NULL DEFAULT '{}'"),
)

_SCHEMA_VERSION = 1


class StateStore:
    """状态库。所有公开方法线程安全。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else paths.state_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30.0, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self.journal_mode = self._enable_wal()
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # ------------------------------------------------------------------ #
    def _enable_wal(self, *, attempts: int = 4) -> str:
        """启用 WAL 日志模式；**瞬时 I/O 错误要重试，实在不行就降级而不是崩**。

        实测触发场景（第 22 轮，缺陷 40）：上一个持有该库的进程刚被强杀，
        Windows 还没来得及释放 `-wal` / `-shm` 的文件映射，此时
        ``PRAGMA journal_mode=WAL`` 会抛 ``sqlite3.OperationalError: disk I/O error``。

        这条路径在**应用启动**上（`StateStore()` 是 GUI 启动时构造的），
        真把异常抛出去，用户的感受就是「双击 exe 没反应 / 一打开就崩」。
        而 WAL 只是个性能/并发优化：拿不到就退回默认的回滚日志模式，
        功能完全不受影响（本地单文件、单进程 GUI 场景下差异可以忽略）。
        """
        last: Exception | None = None
        for attempt in range(max(1, attempts)):
            try:
                row = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
                mode = str(row[0]).lower() if row and row[0] else "unknown"
                if mode != "wal":
                    log.warning("状态库未能启用 WAL（当前 %s）：%s", mode, self.db_path)
                return mode
            except sqlite3.OperationalError as exc:
                last = exc
                if attempt < attempts - 1:
                    time.sleep(0.15 * (attempt + 1))
        log.warning(
            "启用 WAL 失败（%s），退回默认日志模式继续运行：%s",
            last, self.db_path,
        )
        return "delete"
    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            cols = ",\n".join(f"{name} {decl}" for name, decl in _TASK_COLUMNS)
            self._conn.execute(f"CREATE TABLE IF NOT EXISTS tasks ({cols})")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_identity "
                "ON tasks(course_id, resource_id) WHERE resource_id <> ''"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_stage ON tasks(stage, deleted)"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL DEFAULT '',
                    bytes INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT 0,
                    UNIQUE(task_id, kind, path)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER,
                    from_stage TEXT NOT NULL DEFAULT '',
                    to_stage TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
                (str(_SCHEMA_VERSION),),
            )
            log.debug("状态库就绪：%s (schema v%s)", self.db_path, _SCHEMA_VERSION)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 任务 CRUD
    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> TaskRecord:
        d = dict(row)
        d["outputs"] = json.loads(d.pop("outputs_json", "[]") or "[]")
        d["meta"] = json.loads(d.pop("meta_json", "{}") or "{}")
        valid = set(TaskRecord.__dataclass_fields__)
        return TaskRecord(**{k: v for k, v in d.items() if k in valid})

    def upsert_task(self, task: TaskRecord) -> TaskRecord:
        """按 (course_id, resource_id) 幂等插入；已存在则返回原记录（保留进度）。"""
        with self._lock:
            if task.resource_id:
                cur = self._conn.execute(
                    "SELECT * FROM tasks WHERE course_id=? AND resource_id=?",
                    (task.course_id, task.resource_id),
                )
                row = cur.fetchone()
                if row is not None:
                    existing = self._row_to_task(row)
                    if existing.deleted:
                        self._conn.execute(
                            "UPDATE tasks SET deleted=0, updated_at=? WHERE id=?",
                            (time.time(), existing.id),
                        )
                        existing.deleted = 0
                    return existing
            now = time.time()
            task.created_at = task.created_at or now
            task.updated_at = now
            row = task.to_row()
            row.pop("id", None)
            keys = list(row)
            sql = (
                f"INSERT INTO tasks ({', '.join(keys)}) "
                f"VALUES ({', '.join('?' for _ in keys)})"
            )
            cur = self._conn.execute(sql, [row[k] for k in keys])
            task.id = int(cur.lastrowid or 0)
            self._log_event(task.id, "", str(task.stage), "enqueue")
            return task

    def get_task(self, task_id: int) -> TaskRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return self._row_to_task(row) if row else None

    def find_task(self, course_id: str, resource_id: str) -> TaskRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE course_id=? AND resource_id=?",
                (course_id, resource_id),
            ).fetchone()
            return self._row_to_task(row) if row else None

    def list_tasks(self, *, stages: Sequence[str] | None = None, include_deleted: bool = False) -> list[TaskRecord]:
        sql = "SELECT * FROM tasks WHERE 1=1"
        args: list[Any] = []
        if not include_deleted:
            sql += " AND deleted=0"
        if stages:
            sql += f" AND stage IN ({', '.join('?' for _ in stages)})"
            args.extend(stages)
        sql += " ORDER BY id ASC"
        with self._lock:
            return [self._row_to_task(r) for r in self._conn.execute(sql, args).fetchall()]

    def resumable_tasks(self) -> list[TaskRecord]:
        """需要续跑的任务：未完成 + 未取消 + 未跳过。"""
        return self.list_tasks(stages=[str(s) for s in RESUMABLE_STAGES])

    def delete_task(self, task_id: int, *, hard: bool = False) -> None:
        """默认软删除（保护用户数据：任务行还在，只是不在列表里）。"""
        with self._lock:
            if hard:
                self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            else:
                self._conn.execute(
                    "UPDATE tasks SET deleted=1, updated_at=? WHERE id=?", (time.time(), task_id)
                )

    # ------------------------------------------------------------------ #
    # 状态机
    # ------------------------------------------------------------------ #
    def can_transition(self, from_stage: str, to_stage: str) -> bool:
        return can_transition(from_stage, to_stage)

    def update_stage(
        self,
        task_id: int,
        stage: str | Stage,
        *,
        progress: float | None = None,
        error: str | None = None,
        retry: int | None = None,
        message: str = "",
        force: bool = False,
        **fields: Any,
    ) -> TaskRecord | None:
        """推进任务阶段。非法迁移会被拒绝并记日志（除非 ``force=True``）。"""
        stage = str(stage)
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            current = self._row_to_task(row)
            if not force and not self.can_transition(str(current.stage), stage):
                log.warning(
                    "拒绝非法状态迁移 task=%s %s → %s（已忽略）", task_id, current.stage, stage
                )
                return current

            sets: dict[str, Any] = {"stage": stage, "updated_at": time.time()}
            if progress is not None:
                sets["progress"] = max(0.0, min(100.0, float(progress)))
            if error is not None:
                sets["error"] = error
            if retry is not None:
                sets["retry"] = int(retry)
            if stage == str(Stage.DOWNLOADING) and not current.started_at:
                sets["started_at"] = time.time()
            if stage == str(Stage.PROBING):
                sets["attempts"] = current.attempts + 1

            # 允许用 **dataclass 字段名**（outputs / meta）或**列名**（outputs_json / meta_json）
            # 传入；两者都要接受，否则会被静默丢弃（这里曾经踩过坑）。
            json_columns = {"outputs", "meta"}
            allowed_extra = {name for name, _ in _TASK_COLUMNS} | json_columns
            dropped: list[str] = []
            for key, value in fields.items():
                if key not in allowed_extra:
                    dropped.append(key)
                    continue
                if key in json_columns:
                    sets[f"{key}_json"] = json.dumps(value, ensure_ascii=False)
                else:
                    sets[key] = value
            if dropped:
                log.warning(
                    "update_stage 忽略了未知字段 task=%s：%s", task_id, ", ".join(sorted(dropped))
                )

            if stage == str(Stage.DONE):
                sets["finished_at"] = time.time()
                sets["progress"] = 100.0

            assignments = ", ".join(f"{k}=?" for k in sets)
            self._conn.execute(
                f"UPDATE tasks SET {assignments} WHERE id=?", [*sets.values(), task_id]
            )
            self._log_event(task_id, str(current.stage), stage, message or "")
            return self.get_task(task_id)

    def mark_failed(self, task_id: int, error: str, *, retry_bump: bool = True) -> TaskRecord | None:
        with self._lock:
            cur = self.get_task(task_id)
            bump = (cur.retry + 1) if (cur and retry_bump) else (cur.retry if cur else 0)
            return self.update_stage(
                task_id, Stage.FAILED, error=(error or "")[:4000], retry=bump, force=True
            )

    def reset_for_rerun(self, task_id: int, *, keep_audio: bool = True) -> TaskRecord | None:
        """把任务打回 pending 以重跑；默认保留音频缓存（断点续跑：不重下）。"""
        fields: dict[str, Any] = {"error": ""}
        if not keep_audio:
            fields.update({"audio_path": "", "audio_sha256": ""})
        return self.update_stage(task_id, Stage.PENDING, progress=0.0, force=True, **fields)

    def recover_orphans(self) -> list[TaskRecord]:
        """启动时把「上次异常退出时卡在中间态」的任务打回可续跑的起点。

        断点续跑语义：已下好的音频继续用（保留 ``audio_path``），
        只是把阶段回退，让流水线重新决定从哪一步开始。
        """
        stale = [
            Stage.PROBING,
            Stage.DOWNLOADING,
            Stage.SPLITTING,
            Stage.TRANSCRIBING,
            Stage.POST_PROCESSING,
            Stage.WRITING,
        ]
        fixed: list[TaskRecord] = []
        with self._lock:
            for task in self.list_tasks(stages=[str(s) for s in stale]):
                target = Stage.AUDIO_READY if (task.audio_path and Path(task.audio_path).is_file()) else Stage.PENDING
                rec = self.update_stage(
                    task.id,
                    target,
                    progress=0.0,
                    force=True,
                    error="",
                    message="recover-orphan-on-startup",
                )
                if rec:
                    fixed.append(rec)
                    log.info("启动恢复：task=%s《%s》%s → %s", task.id, task.title, task.stage, target)
        return fixed

    # ------------------------------------------------------------------ #
    # 锁（防止同一任务被两个 worker 同时跑）
    # ------------------------------------------------------------------ #
    def acquire_lock(self, task_id: int, owner: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET lock_owner=?, updated_at=? WHERE id=? AND (lock_owner='' OR lock_owner=?)",
                (owner, time.time(), task_id, owner),
            )
            return cur.rowcount > 0

    def release_lock(self, task_id: int, owner: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET lock_owner='', updated_at=? WHERE id=? AND lock_owner=?",
                (time.time(), task_id, owner),
            )

    def clear_all_locks(self) -> None:
        with self._lock:
            self._conn.execute("UPDATE tasks SET lock_owner=''")

    # ------------------------------------------------------------------ #
    # 产物 / 事件
    # ------------------------------------------------------------------ #
    def record_artifact(self, task_id: int, kind: str, path: str, *, sha256: str = "", size: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO artifacts(task_id, kind, path, sha256, bytes, created_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(task_id, kind, path) DO UPDATE SET
                    sha256=excluded.sha256, bytes=excluded.bytes, created_at=excluded.created_at
                """,
                (task_id, kind, path, sha256, size, time.time()),
            )

    def list_artifacts(self, task_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM artifacts WHERE task_id=? ORDER BY id", (task_id,)
                ).fetchall()
            ]

    def _log_event(self, task_id: int | None, from_stage: str, to_stage: str, message: str) -> None:
        self._conn.execute(
            "INSERT INTO events(task_id, from_stage, to_stage, message, created_at) VALUES (?,?,?,?,?)",
            (task_id, from_stage, to_stage, message[:2000], time.time()),
        )

    def list_events(self, task_id: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            if task_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?",
                    (task_id, limit),
                ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT stage, COUNT(*) AS n FROM tasks WHERE deleted=0 GROUP BY stage"
            ).fetchall()
            return {str(r["stage"]): int(r["n"]) for r in rows}

    def bulk_stage(self, task_ids: Iterable[int], stage: str | Stage) -> int:
        n = 0
        for tid in task_ids:
            if self.update_stage(tid, stage, force=True):
                n += 1
        return n
