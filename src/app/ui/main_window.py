"""主窗口：单窗口三栏 + 顶部操作条。

    ┌──────────────────────────────────────────────────────────────────────┐
    │ 登录 刷新清单 加入队列 开始 暂停 停止 设置 打开输出目录   状态/进度   │
    ├───────────────────┬──────────────────────────┬───────────────────────┤
    │ 左：课程→录播树   │ 中：任务队列             │ 右：实时日志          │
    │ 搜索/全选/反选    │ 标题 阶段 进度 重试 错误 │ 可复制 / 可清空       │
    │ 时长合计/已转写   │ 打开输出目录             │                       │
    └───────────────────┴──────────────────────────┴───────────────────────┘

线程规则：所有耗时操作都在 :mod:`app.workers` 的 QThread 里跑，
UI 只通过信号槽更新，绝不在工作线程里碰控件。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme
from ecnu_transcribe import __version__, media, paths
from ecnu_transcribe.catalog import Catalog, Resource, safe_filename
from ecnu_transcribe.client import load_session_state
from ecnu_transcribe.config import AppConfig, ConfigManager
from ecnu_transcribe.errors import AuthExpiredError
from ecnu_transcribe.logbus import LogBus, get_logger
from ecnu_transcribe.store import Stage, StateStore, TaskRecord

from ..workers import CatalogWorker, LoginWorker, PipelineWorker, ProbeWorker, QueueItem
from .settings_dialog import SettingsDialog

log = get_logger("app.window")

STAGE_LABEL = {
    "pending": "待处理",
    "probing": "解析地址",
    "downloading": "下载音频",
    "audio_ready": "音频就绪",
    "splitting": "切分",
    "transcribing": "语音识别",
    "post_processing": "文本加工",
    "writing": "写出文件",
    "done": "完成",
    "failed": "失败",
    "canceled": "已取消",
    "skipped": "已跳过",
}
#: 任务状态颜色。取值全部来自主题（白底对比度 ≥ 6.6:1），
#: 不要写死字面量 —— 之前用的是深色系，在系统深色主题下几乎看不见。
STAGE_COLOR = {
    "done": theme.OK,
    "failed": theme.DANGER,
    "canceled": theme.WARN,
    "transcribing": theme.ACCENT,
    "downloading": theme.ACCENT,
    "writing": theme.ACCENT,
    "pending": theme.TEXT_MUTED,
}


class MainWindow(QMainWindow):
    """应用主窗口。"""

    log_signal = Signal(str, str)  # 供 LogBus → UI 的线程安全转发

    def __init__(self, cm: ConfigManager, store: StateStore) -> None:
        super().__init__()
        self.cm = cm
        self.cfg: AppConfig = cm.cfg
        self.store = store
        self.catalog: Catalog | None = None
        self._resource_index: dict[str, Resource] = {}
        self._row_of_task: dict[int, int] = {}

        self.login_worker: LoginWorker | None = None
        self.catalog_worker: CatalogWorker | None = None
        self.pipeline_worker: PipelineWorker | None = None
        self.probe_worker: ProbeWorker | None = None

        self.setWindowTitle(f"大夏学堂录播转写助手 v{__version__}")
        self.resize(1560, 940)

        self._build_actions()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

        self.log_signal.connect(self._append_log, Qt.QueuedConnection)
        LogBus.instance().subscribe(lambda level, msg: self.log_signal.emit(level, msg))
        for level, msg in LogBus.instance().history():
            self._append_log(level, msg)

        self._load_catalog_if_exists()
        self._reload_tasks()
        QTimer.singleShot(200, self._startup_hints)

    # ================================================================== #
    # 构建 UI
    # ================================================================== #
    def _build_actions(self) -> None:
        def act(text: str, slot, tip: str = "", shortcut: str = "") -> QAction:
            a = QAction(text, self)
            a.triggered.connect(slot)
            if tip:
                a.setToolTip(tip)
            if shortcut:
                a.setShortcut(shortcut)
            return a

        self.act_login = act("登录", self.on_login, "打开可见浏览器，手动完成学校统一身份认证", "Ctrl+L")
        self.act_logout = act("清除登录态", self.on_clear_login, "删除本地保存的登录态（storage_state.json）")
        self.act_refresh = act("刷新清单", self.on_refresh_catalog, "拉取账号下全部课程与录播（幂等）", "F5")
        self.act_enqueue = act("加入队列", self.on_enqueue_checked, "把左侧勾选的录播加入任务队列", "Ctrl+Enter")
        self.act_start = act("开始", self.on_start, "开始执行队列（下载 + 转写）", "Ctrl+R")
        self.act_pause = act("暂停", self.on_pause, "在最近的断点停下（阶段之间 / 音频分段前 / 文本处理块前）；拉流会被就地冻结，恢复后继续而不重下")
        self.act_stop = act("停止", self.on_stop, "停止队列；未完成的任务保留断点，可续跑")
        self.act_settings = act("设置", self.on_settings, "ASR / DeepSeek / 输出目录 / 并发等", "Ctrl+,")
        self.act_readiness = act("首启检查", self.on_readiness, "一键检查：网络是否通、登录态、Chromium、ffmpeg、ASR 是否就绪，并告诉你下一步做什么", "F2")
        self.act_open_out = act("打开输出目录", self.on_open_output, "在资源管理器里打开输出目录")
        self.act_export_catalog = act("导出清单", self.on_export_catalog, "把当前清单另存为 JSON")

    def _build_toolbar(self) -> None:
        tb = QToolBar("主操作")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        for a in (self.act_login, self.act_readiness, self.act_refresh, self.act_enqueue, self.act_start,
                  self.act_pause, self.act_stop):
            tb.addAction(a)
        tb.addSeparator()
        for a in (self.act_settings, self.act_open_out, self.act_export_catalog, self.act_logout):
            tb.addAction(a)
        tb.addSeparator()

        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy(), spacer.sizePolicy().verticalPolicy())
        tb.addWidget(spacer)

        self.lbl_toolbar_status = QLabel("就绪")
        self.lbl_toolbar_status.setStyleSheet(f"color:{theme.TEXT}; padding-right:8px;")
        tb.addWidget(self.lbl_toolbar_status)
        self.addToolBar(tb)

    # ------------------------------------------------------------------ #
    def _build_body(self) -> None:
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_center_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 3)
        splitter.setSizes([560, 560, 420])
        self.setCentralWidget(splitter)

    def _build_left_panel(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(6, 6, 3, 6)

        title = QLabel("① 课程 / 录播清单")
        title.setStyleSheet("font-weight:600;")
        lay.addWidget(title)

        row = QHBoxLayout()
        self.edit_search = QLineEdit()
        self.edit_search.setPlaceholderText("搜索课程、标题、讲师…")
        self.edit_search.textChanged.connect(self._apply_filter)
        row.addWidget(self.edit_search, 1)
        btn_clear = QPushButton("清空")
        btn_clear.setFixedWidth(52)
        btn_clear.clicked.connect(lambda: self.edit_search.setText(""))
        row.addWidget(btn_clear)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        self.btn_select_all = QPushButton("全选")
        self.btn_select_all.clicked.connect(lambda: self._set_all_checked(True))
        self.btn_select_none = QPushButton("反选/清空")
        self.btn_select_none.clicked.connect(lambda: self._set_all_checked(False))
        self.btn_select_untranscribed = QPushButton("只选未转写")
        self.btn_select_untranscribed.clicked.connect(self._check_untranscribed)
        for b in (self.btn_select_all, self.btn_select_none, self.btn_select_untranscribed):
            row2.addWidget(b)
        lay.addLayout(row2)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["标题", "时长", "录制时间", "状态", "讲师"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for i in (1, 2, 3, 4):
            self.tree.header().setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.tree.itemChanged.connect(self._on_tree_item_changed)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        lay.addWidget(self.tree, 1)

        self.lbl_catalog_stats = QLabel("尚未加载清单")
        self.lbl_catalog_stats.setWordWrap(True)
        self.lbl_catalog_stats.setStyleSheet(f"color:{theme.TEXT_MUTED};")
        lay.addWidget(self.lbl_catalog_stats)

        row3 = QHBoxLayout()
        self.lbl_checked = QLabel("已勾选：0 条 / 00:00:00")
        row3.addWidget(self.lbl_checked, 1)
        btn_expand = QPushButton("展开")
        btn_expand.setFixedWidth(52)
        btn_expand.clicked.connect(self.tree.expandAll)
        btn_collapse = QPushButton("折叠")
        btn_collapse.setFixedWidth(52)
        btn_collapse.clicked.connect(self.tree.collapseAll)
        row3.addWidget(btn_expand)
        row3.addWidget(btn_collapse)
        lay.addLayout(row3)
        return box

    def _build_center_panel(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(3, 6, 3, 6)

        title = QLabel("② 任务队列")
        title.setStyleSheet("font-weight:600;")
        lay.addWidget(title)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["标题", "课程", "阶段", "进度", "重试", "耗时", "错误详情", "操作"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in (1, 2, 3, 4, 5, 6):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        self.table.setColumnWidth(6, 160)
        lay.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.btn_queue_remove = QPushButton("移除选中")
        self.btn_queue_remove.clicked.connect(self.on_remove_selected_tasks)
        self.btn_queue_requeue = QPushButton("重跑选中")
        self.btn_queue_requeue.clicked.connect(lambda: self.on_requeue_selected(force=False))
        self.btn_queue_force = QPushButton("强制重跑")
        self.btn_queue_force.setToolTip("忽略已有产物与缓存，从下载开始重跑（会消耗 ASR 额度）")
        self.btn_queue_force.clicked.connect(lambda: self.on_requeue_selected(force=True))
        self.btn_queue_clear = QPushButton("清空列表")
        self.btn_queue_clear.clicked.connect(self.on_clear_tasks)
        for b in (self.btn_queue_remove, self.btn_queue_requeue, self.btn_queue_force, self.btn_queue_clear):
            row.addWidget(b)
        lay.addLayout(row)

        self.lbl_queue_stats = QLabel("队列为空")
        lay.addWidget(self.lbl_queue_stats)
        return box

    def _build_right_panel(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(3, 6, 6, 6)

        header = QHBoxLayout()
        title = QLabel("③ 实时日志")
        title.setStyleSheet("font-weight:600;")
        header.addWidget(title)
        header.addStretch(1)
        self.chk_autoscroll = QCheckBox("自动滚动")
        self.chk_autoscroll.setChecked(True)
        header.addWidget(self.chk_autoscroll)
        btn_copy = QPushButton("复制")
        btn_copy.clicked.connect(self.on_copy_log)
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self.on_clear_log)
        btn_open_log = QPushButton("日志文件")
        btn_open_log.clicked.connect(self.on_open_log_dir)
        for b in (btn_copy, btn_clear, btn_open_log):
            header.addWidget(b)
        lay.addLayout(header)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(6000)
        self.log_view.setFont(QFont(theme.MONO_FAMILIES[0], theme.LOG_FONT_PT))
        lay.addWidget(self.log_view, 1)

        self.lbl_paths = QLabel("")
        self.lbl_paths.setWordWrap(True)
        self.lbl_paths.setStyleSheet(f"color:{theme.TEXT_MUTED}; font-size:11px;")
        lay.addWidget(self.lbl_paths)
        return box

    def _build_statusbar(self) -> None:
        sb = QStatusBar()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedWidth(260)
        self.progress.setTextVisible(True)
        sb.addPermanentWidget(self.progress)
        self.lbl_login = QLabel("登录态：未知")
        sb.addPermanentWidget(self.lbl_login)
        self.setStatusBar(sb)
        self.refresh_login_hint()

    # ================================================================== #
    # 启动提示
    # ================================================================== #
    def _startup_hints(self) -> None:
        # 这是一个 200ms 后触发的定时回调：如果用户在 200ms 内就关掉窗口
        # （或程序化地 close + 关闭状态库），回调再来跑就会踩到已关闭的数据库
        # （``sqlite3.ProgrammingError: Cannot operate on a closed database``）。
        # 这里做一次守卫，让「快速关闭」不会打出吓人的堆栈。
        if not self.isVisible():
            return
        self.lbl_paths.setText(
            "输出目录：" + str(self.cfg.resolved_output_dir())
            + "\n音频缓存：" + str(paths.media_cache_dir())
            + "\n日志：    " + str(paths.log_dir() / "app.log")
        )
        try:
            recovered = self.store.recover_orphans()
        except Exception as exc:  # noqa: BLE001 - 启动提示不能反过来把应用搞崩
            log.warning("启动恢复失败（不影响使用）：%s", exc)
            recovered = []
        if recovered:
            self._append_log("INFO", f"启动恢复：{len(recovered)} 条未完成任务已回退到可续跑起点")
        self._reload_tasks()
        state_file = paths.storage_state_path()
        if not state_file.is_file():
            self._append_log(
                "WARN",
                "尚未登录。建议先点顶部「首启检查」（F2）——它会一次查完"
                "「网络是否通 / 登录态 / 浏览器 / ffmpeg / 语音识别」，并明确告诉你下一步做什么。",
            )
            self._append_log(
                "WARN",
                "然后点「登录」：会打开一个可见的浏览器窗口，请在窗口里手动完成学校统一身份认证"
                "（学号 + 密码 / 可能的验证码）。本应用不会保存你的密码，也不会绕过任何验证。",
            )
            # 首启自动跑一次检查，用户一打开就知道缺什么（不弹窗，只写日志）
            QTimer.singleShot(800, lambda: self.on_readiness(silent=True))
        else:
            # 登录态会过期：实测学校 webVPN 会话**隔夜即失效**（第二天刷新清单会报
            # 「登录态已失效」）。与其让用户第二天撞上一次莫名的失败，不如一开始就说清楚。
            try:
                age_h = max(0.0, (time.time() - state_file.stat().st_mtime) / 3600.0)
            except OSError:
                age_h = 0.0
            if age_h >= 12.0:
                self._append_log(
                    "WARN",
                    f"登录态保存于 {age_h:.0f} 小时前，很可能已过期（实测 webVPN 会话隔夜失效）。"
                    "若刷新清单提示「登录态已失效」，点「登录」重新认证一次即可；"
                    "已下载的音频与已完成的转写都不会重跑。",
                )

    # ================================================================== #
    # 清单
    # ================================================================== #
    def _load_catalog_if_exists(self) -> None:
        p = paths.catalog_path()
        if not p.is_file():
            return
        try:
            self.catalog = Catalog.load(p)
            self._render_tree()
            self._append_log("INFO", f"已加载本地清单缓存：{self.catalog.summary()}")
        except Exception as exc:  # noqa: BLE001
            self._append_log("WARN", f"读取本地清单失败：{exc}")

    def on_refresh_catalog(self) -> None:
        if self.catalog_worker is not None and self.catalog_worker.isRunning():
            QMessageBox.information(self, "正在刷新", "清单刷新正在进行中，请稍候。")
            return
        self.act_refresh.setEnabled(False)
        self._set_toolbar_status("正在拉取清单…")
        self.catalog_worker = CatalogWorker(self.cfg, self.cm)
        self.catalog_worker.status.connect(self._append_log_info)
        self.catalog_worker.finished_ok.connect(self._on_catalog_finished)
        self.catalog_worker.start()

    def _on_catalog_finished(self, catalog: object, error: str) -> None:
        self.act_refresh.setEnabled(True)
        if catalog is None:
            if error.startswith("AUTH_EXPIRED::"):
                self._on_auth_expired(error.split("::", 1)[1])
            else:
                self._set_toolbar_status("清单拉取失败")
                self._append_log("ERROR", f"清单拉取失败：{error}")
                QMessageBox.warning(self, "清单拉取失败", error)
            return
        assert isinstance(catalog, Catalog)
        self.catalog = catalog
        self._render_tree()
        self._set_toolbar_status("清单已更新")
        self._append_log("INFO", "清单已更新：" + catalog.summary())
        # 自动把新条目加进队列（状态库 upsert 幂等，不影响已有进度）
        added = self._enqueue_resources(catalog.resources, quiet=True)
        self._append_log("INFO", f"已同步 {added} 条录播到任务队列（去重后）")
        self._reload_tasks()

    def _render_tree(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        if self.catalog is None:
            self.tree.blockSignals(False)
            return
        task_by_res = {
            (t.course_id, t.resource_id): t for t in self.store.list_tasks(include_deleted=False)
        }
        for course in self.catalog.courses:
            total = sum(r.duration_sec for r in course.resources)
            node = QTreeWidgetItem([
                f"{course.course_name}（{len(course.resources)} 条）",
                media.human_duration(total),
                "",
                "",
                course.teacher,
            ])
            node.setFirstColumnSpanned(False)
            f = node.font(0)
            f.setBold(True)
            node.setFont(0, f)
            node.setFlags(node.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            node.setCheckState(0, Qt.Unchecked)
            node.setData(0, Qt.UserRole, {"kind": "course", "course_id": course.course_id})
            self.tree.addTopLevelItem(node)

            for res in course.resources:
                task = task_by_res.get((res.course_id, res.resource_id))
                state = STAGE_LABEL.get(task.stage, task.stage) if task else "未入队"
                child = QTreeWidgetItem([
                    res.title,
                    media.human_duration(res.duration_sec),
                    res.record_time,
                    state,
                    res.teacher or course.teacher,
                ])
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Unchecked)
                child.setData(0, Qt.UserRole, {"kind": "resource", "key": res.unique_key})
                if task and task.stage == str(Stage.DONE):
                    child.setForeground(0, QColor(STAGE_COLOR["done"]))
                elif task and task.stage == str(Stage.FAILED):
                    child.setForeground(0, QColor(STAGE_COLOR["failed"]))
                node.addChild(child)
                self._resource_index[res.unique_key] = res
        self.tree.blockSignals(False)
        self.lbl_catalog_stats.setText(
            (self.catalog.summary() if self.catalog else "尚未加载清单")
            + f"\n清单缓存：{paths.catalog_path()}"
        )
        self._update_checked_stats()

    def _apply_filter(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            course_item = self.tree.topLevelItem(i)
            visible_children = 0
            for j in range(course_item.childCount()):
                child = course_item.child(j)
                haystack = " ".join(child.text(c) for c in range(5)).lower()
                match = (not needle) or (needle in haystack)
                child.setHidden(not match)
                visible_children += int(match)
            course_hit = (not needle) or (needle in course_item.text(0).lower())
            course_item.setHidden(not (course_hit or visible_children > 0))

    def _iter_resource_items(self):
        for i in range(self.tree.topLevelItemCount()):
            course_item = self.tree.topLevelItem(i)
            for j in range(course_item.childCount()):
                yield course_item.child(j)

    def _set_all_checked(self, checked: bool) -> None:
        self.tree.blockSignals(True)
        for child in self._iter_resource_items():
            if not child.isHidden():
                child.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
        self.tree.blockSignals(False)
        self._update_checked_stats()

    def _check_untranscribed(self) -> None:
        done = {t.resource_id for t in self.store.list_tasks(stages=[str(Stage.DONE)])}
        self.tree.blockSignals(True)
        for child in self._iter_resource_items():
            data = child.data(0, Qt.UserRole) or {}
            res = self._resource_index.get(data.get("key", ""))
            if res is not None and not child.isHidden():
                child.setCheckState(0, Qt.Checked if res.resource_id not in done else Qt.Unchecked)
        self.tree.blockSignals(False)
        self._update_checked_stats()

    def _on_tree_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column == 0:
            self._update_checked_stats()

    def _on_selection_changed(self) -> None:
        items = self.tree.selectedItems()
        self._set_toolbar_status(f"已选中 {len(items)} 个节点")

    def checked_resources(self) -> list[Resource]:
        out: list[Resource] = []
        for child in self._iter_resource_items():
            if child.checkState(0) == Qt.Checked:
                data = child.data(0, Qt.UserRole) or {}
                res = self._resource_index.get(data.get("key", ""))
                if res is not None:
                    out.append(res)
        return out

    def _update_checked_stats(self) -> None:
        res = self.checked_resources()
        total = sum(r.duration_sec for r in res)
        self.lbl_checked.setText(
            f"已勾选：{len(res)} 条 / {media.human_duration(total)}"
        )

    def on_export_catalog(self) -> None:
        if self.catalog is None:
            QMessageBox.information(self, "没有清单", "请先「刷新清单」。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出清单", str(paths.data_dir() / "catalog_export.json"), "JSON (*.json)"
        )
        if path:
            self.catalog.save(Path(path))
            self._append_log("INFO", f"清单已导出：{path}")

    # ================================================================== #
    # 任务队列
    # ================================================================== #
    def on_enqueue_checked(self) -> None:
        res = self.checked_resources()
        if not res:
            QMessageBox.information(self, "未勾选", "请先在左侧勾选要转写的录播。")
            return
        added = self._enqueue_resources(res)
        self._reload_tasks()
        self._append_log("INFO", f"已加入队列：{added} 条")

    def _enqueue_resources(self, resources: list[Resource], *, quiet: bool = False) -> int:
        added = 0
        for res in resources:
            if not res.resource_id:
                continue
            before = self.store.find_task(res.course_id, res.resource_id)
            task = self.store.upsert_task(
                TaskRecord(
                    course=res.course_name,
                    course_id=res.course_id,
                    resource_id=res.resource_id,
                    title=res.title,
                    output_dir=str(self.cfg.resolved_output_dir()),
                    duration_sec=res.duration_sec,
                    play_url=res.play_url,
                    stage=str(Stage.PENDING),
                )
            )
            if before is None and task.id:
                added += 1
        if not quiet and added:
            self._render_tree()
        return added

    def _reload_tasks(self) -> None:
        tasks = self.store.list_tasks(include_deleted=False)
        self.table.setRowCount(0)
        self._row_of_task.clear()
        for row, task in enumerate(tasks):
            self._insert_task_row(row, task)
        self._update_queue_stats()
        self._refresh_tree_states()

    def _insert_task_row(self, row: int, task: TaskRecord) -> None:
        self.table.insertRow(row)
        self._row_of_task[task.id] = row
        self._fill_task_row(row, task)

    def _fill_task_row(self, row: int, task: TaskRecord) -> None:
        def cell(text: str, tip: str = "") -> QTableWidgetItem:
            item = QTableWidgetItem(text)
            if tip:
                item.setToolTip(tip)
            return item

        self.table.setItem(row, 0, cell(task.title, task.title))
        self.table.setItem(row, 1, cell(task.course))
        stage_label = STAGE_LABEL.get(task.stage, task.stage)
        stage_item = cell(stage_label)
        color = STAGE_COLOR.get(task.stage)
        if color:
            stage_item.setForeground(QColor(color))
        self.table.setItem(row, 2, stage_item)

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(int(task.progress))
        bar.setTextVisible(True)
        bar.setFixedHeight(18)
        self.table.setCellWidget(row, 3, bar)

        self.table.setItem(row, 4, cell(str(task.retry)))
        elapsed = ""
        if task.started_at:
            end = task.finished_at or time.time()
            elapsed = media.human_duration(max(0.0, end - task.started_at))
        self.table.setItem(row, 5, cell(elapsed, f"updated_at={time.strftime('%H:%M:%S', time.localtime(task.updated_at or 0))}"))
        err_item = cell((task.error or "")[:200], task.error or "")
        if task.error:
            err_item.setForeground(QColor(STAGE_COLOR["failed"]))
        self.table.setItem(row, 6, err_item)

        btn = QPushButton("打开输出目录")
        btn.setFixedHeight(20)
        btn.clicked.connect(lambda _=False, t=task: self._open_dir(Path(t.output_dir or self.cfg.resolved_output_dir())))
        self.table.setCellWidget(row, 7, btn)

    def _update_queue_stats(self) -> None:
        stats = self.store.stats()
        parts = [f"{STAGE_LABEL.get(k, k)} {v}" for k, v in sorted(stats.items())]
        total = sum(stats.values())
        self.lbl_queue_stats.setText(f"共 {total} 条：" + ("，".join(parts) if parts else "空"))

    def _task_row_of(self, task_id: int) -> int | None:
        return self._row_of_task.get(task_id)

    def _refresh_tree_states(self) -> None:
        if self.catalog is None:
            return
        task_by_res = {
            (t.course_id, t.resource_id): t for t in self.store.list_tasks(include_deleted=False)
        }
        for i in range(self.tree.topLevelItemCount()):
            course_item = self.tree.topLevelItem(i)
            for j in range(course_item.childCount()):
                child = course_item.child(j)
                data = child.data(0, Qt.UserRole) or {}
                res = self._resource_index.get(data.get("key", ""))
                if res is None:
                    continue
                task = task_by_res.get((res.course_id, res.resource_id))
                child.setText(3, STAGE_LABEL.get(task.stage, task.stage) if task else "未入队")
                if task and task.stage == str(Stage.DONE):
                    child.setForeground(0, QColor(STAGE_COLOR["done"]))
                elif task and task.stage == str(Stage.FAILED):
                    child.setForeground(0, QColor(STAGE_COLOR["failed"]))
                else:
                    child.setForeground(0, QColor(theme.TEXT))

    def _selected_task_ids(self) -> list[int]:
        ids: list[int] = []
        for idx in self.table.selectionModel().selectedRows():
            row = idx.row()
            for tid, r in self._row_of_task.items():
                if r == row:
                    ids.append(tid)
                    break
        return ids

    def on_remove_selected_tasks(self) -> None:
        ids = self._selected_task_ids()
        if not ids:
            QMessageBox.information(self, "未选中", "请先在中间列表选中任务。")
            return
        for tid in ids:
            self.store.delete_task(tid, hard=False)
        self._append_log("INFO", f"已移除 {len(ids)} 条任务（软删除，产物文件保留）")
        self._reload_tasks()

    def on_requeue_selected(self, *, force: bool) -> None:
        ids = self._selected_task_ids()
        if not ids:
            QMessageBox.information(self, "未选中", "请先在中间列表选中任务。")
            return
        for tid in ids:
            task = self.store.get_task(tid)
            self.store.reset_for_rerun(tid, keep_audio=not force)
        self._reload_tasks()
        self._append_log("INFO", f"已把 {len(ids)} 条任务打回待处理（{'强制重跑' if force else '续跑'}）")

    def on_clear_tasks(self) -> None:
        if QMessageBox.question(self, "清空列表", "将从列表中移除全部任务（产物文件不会被删除）。确定吗？") != QMessageBox.Yes:
            return
        for task in self.store.list_tasks(include_deleted=False):
            self.store.delete_task(task.id, hard=False)
        self._reload_tasks()

    # ================================================================== #
    # 运行
    # ================================================================== #
    def on_start(self) -> None:
        if self.pipeline_worker is not None and self.pipeline_worker.isRunning():
            QMessageBox.information(self, "正在运行", "队列已经在运行中。")
            return
        pending = [
            t for t in self.store.list_tasks(include_deleted=False)
            if t.stage in {str(Stage.PENDING), str(Stage.FAILED), str(Stage.CANCELED)}
        ]
        if not pending:
            QMessageBox.information(
                self, "没有待处理任务", "队列里没有待处理/失败的任务。\n请在左侧勾选录播后点「加入队列」。"
            )
            return
        if self._asr_not_configured():
            QMessageBox.warning(
                self, "未配置语音识别",
                "还没有配置 ASR（语音识别）端点，无法转写。\n\n"
                "注意：DeepSeek 只有文本模型，**没有**语音转文字接口。\n"
                "请到「设置 → 语音识别」填入阿里云百炼 DashScope 的 API Key 与模型名。",
            )
            return

        items: list[QueueItem] = []
        for task in pending:
            res = self._resource_index.get(f"{task.course_id}::{task.resource_id}")
            if res is None:
                from ecnu_transcribe.pipeline import resource_from_task

                res = resource_from_task(task)
            items.append(QueueItem(task_id=task.id, resource=res))

        worker = PipelineWorker(self.cfg, self.cm, self.store, items, force=False)
        worker.task_stage.connect(self._on_task_stage)
        worker.log_line.connect(self._append_log)
        worker.task_done.connect(self._on_task_done)
        worker.queue_finished.connect(self._on_queue_finished)
        worker.request_relogin.connect(self._on_auth_expired)
        self.pipeline_worker = worker
        worker.start()
        self.act_start.setEnabled(False)
        self._set_toolbar_status(f"队列运行中（{len(items)} 条）…")
        self._append_log("INFO", f"开始执行队列：{len(items)} 条任务")

    def _asr_not_configured(self) -> bool:
        provider = (self.cfg.asr_provider or "").lower()
        if provider in ("faster_whisper_local", "local", "faster-whisper"):
            return False
        return not bool(self.cm.secret("asr_api_key"))

    def on_pause(self) -> None:
        w = self.pipeline_worker
        if w is None or not w.isRunning():
            return
        if w.paused:
            w.resume()
            self.act_pause.setText("暂停")
            self._set_toolbar_status("已恢复")
            self._append_log("INFO", "队列已恢复（从断点继续，已完成的音频/分段/产物都不重跑）")
        else:
            w.request_pause()
            self.act_pause.setText("继续")
            self._set_toolbar_status("已暂停：将在最近断点停下")
            self._append_log(
                "INFO",
                "已请求暂停：正在跑的那一小段不打断，会在最近的断点停下"
                "（阶段之间 / 每个音频分段前 / 每个文本处理块前）；"
                "拉流会被就地冻结，恢复后继续下载而不重下。",
            )

    def on_stop(self) -> None:
        w = self.pipeline_worker
        if w is None or not w.isRunning():
            return
        w.request_stop()
        self._set_toolbar_status("正在停止…")
        self._append_log("WARN", "已请求停止：未完成的任务会保留断点，下次可续跑")

    def _on_task_stage(self, task_id: int, stage: str, progress: float, message: str) -> None:
        row = self._task_row_of(task_id)
        if row is None:
            self._reload_tasks()
            row = self._task_row_of(task_id)
            if row is None:
                return
        stage_item = self.table.item(row, 2)
        if stage_item is not None:
            stage_item.setText(STAGE_LABEL.get(stage, stage))
            color = STAGE_COLOR.get(stage)
            stage_item.setForeground(QColor(color) if color else QColor(theme.TEXT))
        bar = self.table.cellWidget(row, 3)
        if isinstance(bar, QProgressBar):
            bar.setValue(int(progress))
        if message:
            self._set_toolbar_status(f"{STAGE_LABEL.get(stage, stage)}：{message[:80]}")
        self.progress.setValue(int(progress))

    def _on_task_done(self, task_id: int, ok: bool, error: str) -> None:
        task = self.store.get_task(task_id)
        row = self._task_row_of(task_id)
        if row is not None and task is not None:
            self._fill_task_row(row, task)
        self._update_queue_stats()
        self._refresh_tree_states()

    def _on_queue_finished(self, ok: int, fail: int) -> None:
        self.act_start.setEnabled(True)
        self.act_pause.setText("暂停")
        self.progress.setValue(100 if fail == 0 else self.progress.value())
        self._set_toolbar_status(f"队列结束：成功 {ok} 条，失败 {fail} 条")
        self._append_log("INFO", f"队列结束：成功 {ok} 条，失败 {fail} 条")
        self._reload_tasks()
        if fail == 0 and ok > 0:
            QMessageBox.information(
                self, "完成", f"全部 {ok} 条任务处理完成。\n输出目录：{self.cfg.resolved_output_dir()}"
            )

    # ================================================================== #
    # 首启一键检查
    # ================================================================== #
    def on_readiness(self, *, silent: bool = False) -> None:
        """一键把「能不能开始用」查清楚：网络 / 登录态 / Chromium / ffmpeg / ASR。

        ``silent=True`` 时只在日志与状态栏输出，不弹对话框（用于首启自动检查）。
        """
        if self.probe_worker is not None and self.probe_worker.isRunning():
            if not silent:
                QMessageBox.information(self, "正在检查", "首启检查还在进行中，请稍候。")
            return
        self._readiness_silent = silent
        self.act_readiness.setEnabled(False)
        self._set_toolbar_status("正在进行首启检查…")
        self._append_log("INFO", "开始首启一键检查（网络 / 登录态 / Chromium / ffmpeg / ASR）…")

        w = ProbeWorker(self.cfg, self.cm)
        w.full_readiness = True
        w.result.connect(lambda name, ok, msg: self._append_log("INFO" if ok else "WARN", f"{name}：{msg}"))
        w.report.connect(self._on_readiness_report)
        w.finished_ready.connect(self._on_readiness_done)
        self.probe_worker = w
        w.start()

    def _on_readiness_report(self, text: str) -> None:
        self._last_readiness = text

    def _on_readiness_done(self, ready: bool) -> None:
        self.act_readiness.setEnabled(True)
        text = getattr(self, "_last_readiness", "")
        self._set_toolbar_status("首启检查完成" + ("（全部就绪）" if ready else "（有未通过项）"))
        if getattr(self, "_readiness_silent", False):
            self._append_log("INFO", "首启检查结果：\n" + text)
            return
        if ready:
            QMessageBox.information(self, "首启检查：全部就绪", text)
        else:
            QMessageBox.warning(self, "首启检查：还有未就绪项", text)

    # ================================================================== #
    # 登录
    # ================================================================== #
    def on_login(self) -> None:
        if self.login_worker is not None and self.login_worker.isRunning():
            self.login_worker.bring_to_front()
            QMessageBox.information(self, "登录进行中", "登录窗口已经打开，请在其中完成认证。")
            return

        dlg = _LoginGuideDialog(self)
        self._login_dialog = dlg

        self.login_worker = LoginWorker(self.cfg, timeout=900.0)
        self.login_worker.status.connect(self._append_log_info)
        self.login_worker.finished_ok.connect(self._on_login_finished)
        # 先接好交互信号**再**启动线程。
        # 反过来的话存在竞态：线程一起来就在轮询登录态，而「我已登录完成 / 取消」
        # 还没接上，用户在那一瞬间点击会被丢掉（表现为「点了没反应」）。
        dlg.confirmed.connect(self.login_worker.confirm_logged_in)
        dlg.cancelled.connect(self.login_worker.request_stop)
        self.login_worker.canceled.connect(self._on_login_canceled)
        dlg.show()  # 非模态：用户可同时操作浏览器
        self.login_worker.start()

    def _on_login_canceled(self) -> None:
        """用户主动取消登录：安静收尾，不弹错误框。"""
        dlg = getattr(self, "_login_dialog", None)
        if dlg is not None and dlg.isVisible():
            dlg.close()
        self._append_log("INFO", "已取消登录（随时可以重新点「登录」）。")
        self._set_toolbar_status("已取消登录")
        self.refresh_login_hint()

    def _on_login_finished(self, ok: bool, error: str) -> None:
        dlg = getattr(self, "_login_dialog", None)
        if dlg is not None:
            dlg.close()
        if ok:
            self._append_log("INFO", "登录态已保存。可以点「刷新清单」拉取课程与录播了。")
            self.refresh_login_hint()
            QMessageBox.information(self, "登录成功", "登录态已保存。\n下一步：点「刷新清单」。")
        else:
            self._append_log("ERROR", f"登录未完成：{error}")
            self.refresh_login_hint()
            QMessageBox.warning(
                self, "登录未完成",
                f"{error}\n\n可以重试。若站点提示需要校园网/VPN，请先连接学校 SSL-VPN"
                "（https://vpn.ecnu.edu.cn/portal/）再试。",
            )

    def on_clear_login(self) -> None:
        if QMessageBox.question(self, "清除登录态", "将删除本地保存的 Cookie / storage_state。确定吗？") != QMessageBox.Yes:
            return
        for p in (paths.storage_state_path(),):
            try:
                if p.is_file():
                    p.unlink()
            except OSError as exc:
                self._append_log("ERROR", f"删除失败 {p}：{exc}")
        self._append_log("INFO", "已清除本地登录态。")
        self.refresh_login_hint()

    def refresh_login_hint(self) -> None:
        st = load_session_state()
        if st.is_empty():
            self.lbl_login.setText("登录态：未登录")
            self.lbl_login.setStyleSheet(f"color:{theme.DANGER}; font-weight:600;")
        else:
            when = time.strftime("%m-%d %H:%M", time.localtime(st.saved_at or 0))
            self.lbl_login.setText(f"登录态：已保存（{when}，{len(st.cookies)} cookies）")
            self.lbl_login.setStyleSheet(f"color:{theme.OK}; font-weight:600;")

    def _on_auth_expired(self, message: str) -> None:
        self.refresh_login_hint()
        self._append_log("ERROR", f"登录态失效：{message}")
        self._set_toolbar_status("登录态失效，请重新登录")
        QMessageBox.warning(
            self, "登录态失效",
            f"{message}\n\n请点顶部「登录」重新完成一次统一身份认证；"
            "已下载的音频与已完成的转写不会丢失，可继续续跑。",
        )

    # ================================================================== #
    # 设置 / 杂项
    # ================================================================== #
    def on_settings(self) -> None:
        dlg = SettingsDialog(self.cfg, self.cm, self)
        if dlg.exec() == QDialog.Accepted:
            self.cfg = self.cm.cfg
            self.lbl_paths.setText(
                "输出目录：" + str(self.cfg.resolved_output_dir())
                + "\n音频缓存：" + str(paths.media_cache_dir())
                + "\n日志：    " + str(paths.log_dir() / "app.log")
            )
            self._append_log("INFO", "设置已保存。")
            if self.pipeline_worker is None or not self.pipeline_worker.isRunning():
                self._reload_tasks()

    def on_open_output(self) -> None:
        self._open_dir(self.cfg.resolved_output_dir())

    def on_open_log_dir(self) -> None:
        self._open_dir(paths.log_dir())

    def _open_dir(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
            self._append_log("INFO", f"已打开目录：{path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打开失败", f"{path}\n{exc}")

    def on_copy_log(self) -> None:
        QGuiApplication.clipboard().setText(self.log_view.toPlainText())
        self._set_toolbar_status("日志已复制到剪贴板")

    def on_clear_log(self) -> None:
        self.log_view.clear()
        LogBus.instance().clear()

    def _append_log(self, level: str, message: str) -> None:
        color = {
            "ERROR": "#b42318",
            "CRITICAL": "#b42318",
            "WARNING": "#8a6d3b",
            "WARN": "#8a6d3b",
            "INFO": "#1f2937",
            "DEBUG": "#6b7280",
        }.get(str(level).upper(), "#1f2937")
        safe = (
            str(message)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )
        self.log_view.appendHtml(f'<span style="color:{color}">{safe}</span>')
        if self.chk_autoscroll.isChecked():
            sb = self.log_view.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _append_log_info(self, message: str) -> None:
        self._append_log("INFO", message)

    def _set_toolbar_status(self, text: str) -> None:
        self.lbl_toolbar_status.setText(text)

    # ================================================================== #
    def _all_workers(self) -> list:
        """窗口拥有的全部后台线程。

        必须**动态枚举**，不能手写清单 —— 原来只列了
        ``pipeline/catalog/login`` 三个，后来加进来的 ``probe_worker``
        （首启自动诊断，会在启动 800ms 后自己跑起来）没被列进去：
        关窗时它还在跑，Qt 直接析构仍在运行的 QThread → 进程崩溃
        （0xC0000409，Windows 上表现为「一关就异常退出」）。
        """
        from PySide6.QtCore import QThread

        workers = []
        for attr in ("pipeline_worker", "catalog_worker", "login_worker", "probe_worker"):
            w = getattr(self, attr, None)
            if w is not None and w not in workers:
                workers.append(w)
        # 兜底：任何以本窗口为父对象的 QThread 都要一起收尾
        for child in self.findChildren(QThread):
            if child not in workers:
                workers.append(child)
        return workers

    def closeEvent(self, event) -> None:  # noqa: N802
        workers = self._all_workers()
        running = [w for w in workers if w.isRunning()]

        # 只有「会影响断点」的任务型线程才需要征求确认；
        # 诊断（probe）这类只读线程直接静默停掉即可，不该弹窗打扰。
        interactive = [
            w for w in running
            if w in (getattr(self, "pipeline_worker", None), getattr(self, "catalog_worker", None),
                     getattr(self, "login_worker", None))
        ]
        if interactive:
            if QMessageBox.question(
                self, "仍在运行", "还有后台任务在运行。要停止并退出吗？\n（断点已保存在 state.db，下次可续跑）"
            ) != QMessageBox.Yes:
                event.ignore()
                return

        for w in running:
            try:
                if hasattr(w, "request_stop"):
                    w.request_stop()
                elif hasattr(w, "stop"):
                    w.stop()
            except Exception:
                pass
        # 逐个等待；仍不退出的线程不能阻塞关窗（否则用户「点了关闭却没反应」）
        from PySide6.QtCore import QThread

        for w in running:
            try:
                if not w.wait(8000):
                    log.warning("后台线程未在 8s 内退出，将继续关闭：%s", type(w).__name__)
                    if isinstance(w, QThread) and w.isRunning():
                        w.terminate()
                        w.wait(2000)
            except Exception as exc:  # noqa: BLE001
                log.debug("等待线程退出异常：%s", exc)

        try:
            self.store.clear_all_locks()
            self.store.close()
        except Exception:
            pass
        log.info("应用退出")
        event.accept()


# --------------------------------------------------------------------------- #
class _LoginGuideDialog(QDialog):
    """登录指引（非模态，用户可一边看提示一边操作浏览器）。"""

    confirmed = Signal()
    cancelled = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("请完成学校统一身份认证")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.resize(560, 320)

        lay = QVBoxLayout(self)
        text = QLabel(
            "<h3>登录步骤</h3>"
            "<ol>"
            "<li>已经在屏幕上为你打开了一个 <b>可见的浏览器窗口</b>（Chromium）。</li>"
            "<li>在窗口里用 <b>学号 + 密码</b> 登录华东师范大学统一身份认证；"
            "如出现图形验证码或二次验证，请手动完成。</li>"
            "<li>登录成功并看到课程/资源管理页面后，本应用会自动检测并保存登录态。</li>"
            "<li>若自动检测没反应，点下方「我已登录完成」按钮即可。</li>"
            "</ol>"
            f"<p style='color:{theme.WARN}'><b>安全说明：</b>本应用<b>不会</b>保存你的密码，"
            "<b>不会</b>识别验证码，<b>不会</b>绕过任何验证。"
            "登录态以 Cookie 形式保存在 <code>%LOCALAPPDATA%\\ecnu-transcribe\\</code>，"
            "仅在你自己配置的接口调用中使用。</p>"
            f"<p style='color:{theme.ACCENT}'>若浏览器里提示站点需要校园网或 VPN：请先连接学校 SSL-VPN"
            "（<code>https://vpn.ecnu.edu.cn/portal/</code>）或接入校园网，然后重新点「登录」。</p>"
        )
        text.setWordWrap(True)
        lay.addWidget(text)
        lay.addStretch(1)

        row = QHBoxLayout()
        btn_ok = QPushButton("我已登录完成")
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(self._on_confirm)
        btn_cancel = QPushButton("取消登录")
        btn_cancel.clicked.connect(self._on_cancel)
        row.addStretch(1)
        row.addWidget(btn_cancel)
        row.addWidget(btn_ok)
        lay.addLayout(row)

    def _on_confirm(self) -> None:
        self.confirmed.emit()

    def _on_cancel(self) -> None:
        self.cancelled.emit()
        self.close()
