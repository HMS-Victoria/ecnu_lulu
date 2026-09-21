"""温暖书房工作区：我的课程、转写任务与按需展开的详细日志。

线程规则：所有耗时操作都在 :mod:`app.workers` 的 QThread 里跑，
UI 只通过信号槽更新，绝不在工作线程里碰控件。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QModelIndex, QSize, Qt, QTimer, Signal
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
    QListWidget,
    QListWidgetItem,
    QMenu,
    QToolButton,
    QSizePolicy,
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
        self.resize(1240, 820)
        screen = QGuiApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            self.resize(min(1240, available.width() - 40), min(820, available.height() - 60))
        self._selected_course = ""
        self._login_verified = False
        self._batch_active = False
        self._task_stages: dict[int, str] = {}

        self._build_actions()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

        self.log_signal.connect(self._append_log, Qt.QueuedConnection)
        self._unsubscribe_log = LogBus.instance().subscribe(lambda level, msg: self.log_signal.emit(level, msg))
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

    def _action_button(self, action: QAction, *, primary: bool = False) -> QToolButton:
        button = QToolButton()
        button.setDefaultAction(action)
        button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        if primary:
            button.setProperty("role", "primary")
        return button

    def _build_toolbar(self) -> None:
        self.act_login.setText("登录学校账号")
        self.act_refresh.setText("刷新课程")
        self.act_enqueue.setText("加入待办")
        self.act_start.setText("开始待办")
        self.act_readiness.setText("使用检查")
        tb = QToolBar("应用")
        tb.setMovable(False)
        brand = QLabel("大夏学堂  /  转写助手")
        brand.setProperty("role", "brand")
        tb.addWidget(brand)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.lbl_login = QLabel("尚未登录")
        self.lbl_login.setProperty("role", "hint")
        tb.addWidget(self.lbl_login)
        tb.addWidget(self._action_button(self.act_login))
        tb.addWidget(self._action_button(self.act_settings))
        more = QToolButton()
        more.setText("更多")
        more.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(more)
        for action in (self.act_readiness, self.act_open_out, self.act_export_catalog, self.act_logout):
            menu.addAction(action)
        more.setMenu(menu)
        tb.addWidget(more)
        self.addToolBar(tb)
        # 菜单里的快捷键在页面切换后仍可用。
        self.addActions([self.act_enqueue, self.act_start, self.act_refresh])
        self.act_pause.setEnabled(False)
        self.act_stop.setEnabled(False)


    def _build_body(self) -> None:
        shell = QWidget()
        layout = QVBoxLayout(shell)
        self.shell_layout = layout
        layout.setContentsMargins(24, 12, 24, 12)
        layout.setSpacing(12)
        self.notice = QFrame()
        self.notice.setProperty("role", "notice")
        notice_row = QHBoxLayout(self.notice)
        self.notice_text = QLabel()
        self.notice_text.setWordWrap(True)
        self.notice_text.setTextFormat(Qt.PlainText)
        self.notice_text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        notice_row.addWidget(self.notice_text, 1)
        self.notice_action = QPushButton("查看详情")
        self.notice_action.clicked.connect(self._notice_next)
        notice_row.addWidget(self.notice_action)
        dismiss = QPushButton("关闭提示")
        dismiss.clicked.connect(self.notice.hide)
        notice_row.addWidget(dismiss)
        self.notice.hide()
        layout.addWidget(self.notice)
        self.setup_card = QFrame()
        self.setup_card.setProperty("role", "card")
        setup_layout = QVBoxLayout(self.setup_card)
        self.setup_text = QLabel("开始前，完成这三步")
        self.setup_text.setWordWrap(True)
        self.setup_text.setProperty("role", "section")
        setup_layout.addWidget(self.setup_text)
        setup_row = QHBoxLayout()
        for label, action in (("① 登录学校账号", self.on_login), ("② 设置语音识别", self.on_settings), ("③ 选择课程", self.on_refresh_catalog)):
            button = QPushButton(label)
            button.clicked.connect(action)
            setup_row.addWidget(button)
        setup_row.addStretch()
        setup_row.addWidget(self._action_button(self.act_readiness))
        setup_layout.addLayout(setup_row)
        layout.addWidget(self.setup_card)
        self.pages = QTabWidget()
        self.pages.setObjectName("workspacePages")
        self.pages.addTab(self._build_left_panel(), "我的课程")
        self.pages.addTab(self._build_center_panel(), "转写任务")
        layout.addWidget(self.pages, 1)
        self.log_panel = self._build_right_panel()
        self.log_panel.setMaximumHeight(230)
        self.log_panel.hide()
        layout.addWidget(self.log_panel)
        self.setCentralWidget(shell)

    def _show_notice(self, message: str, action: str = "details") -> None:
        self.notice_text.setText(message)
        self._notice_action = action
        self.notice_action.setText({"login": "重新登录", "settings": "打开设置", "details": "查看日志"}.get(action, "查看日志"))
        self.notice.show()

    def _notice_next(self) -> None:
        action = getattr(self, "_notice_action", "details")
        if action == "login":
            self.on_login()
        elif action == "settings":
            self.on_settings()
        else:
            self.btn_logs.setChecked(True)


    def _build_left_panel(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.course_layout = layout
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(14)
        heading = QLabel("把课堂，留在文字里")
        self.course_hero = heading
        heading.setProperty("role", "heading")
        layout.addWidget(heading)
        hint = QLabel("选择想复习的录播，生成文稿、字幕与课堂笔记。")
        self.course_hint = hint
        hint.setProperty("role", "hint")
        layout.addWidget(hint)
        row = QHBoxLayout()
        self.btn_courses = QPushButton("课程列表")
        self.btn_courses.setCheckable(True)
        self.btn_courses.setChecked(True)
        row.addWidget(self.btn_courses)
        self.edit_search = QLineEdit()
        self.edit_search.setPlaceholderText("搜索课程、录播或讲师")
        self.edit_search.setClearButtonEnabled(True)
        self.edit_search.textChanged.connect(self._apply_filter)
        row.addWidget(self.edit_search, 1)
        row.addWidget(self._action_button(self.act_refresh))
        layout.addLayout(row)
        self.course_splitter = QSplitter(Qt.Horizontal)
        self.course_list = QListWidget()
        self.course_list.setObjectName("courseList")
        self.course_list.setMinimumWidth(160)
        self.course_list.setMaximumWidth(300)
        self.course_list.currentItemChanged.connect(self._choose_course)
        self.btn_courses.toggled.connect(self.course_list.setVisible)
        self.course_splitter.addWidget(self.course_list)
        lessons = QWidget()
        lesson_layout = QVBoxLayout(lessons)
        lesson_layout.setContentsMargins(12, 0, 0, 0)
        self.course_heading = QLabel("我的录播")
        self.course_heading.setProperty("role", "section")
        lesson_layout.addWidget(self.course_heading)
        selection_row = QHBoxLayout()
        self.btn_select_all = QPushButton("全选当前结果")
        self.btn_select_all.clicked.connect(lambda: self._set_all_checked(True))
        self.btn_select_none = QPushButton("清空全部选择")
        self.btn_select_none.clicked.connect(self._clear_selection)
        self.btn_select_untranscribed = QPushButton("只选未转写")
        self.btn_select_untranscribed.clicked.connect(self._check_untranscribed)
        for button in (self.btn_select_all, self.btn_select_untranscribed, self.btn_select_none):
            selection_row.addWidget(button)
        selection_row.addStretch()
        lesson_layout.addLayout(selection_row)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["录播", "时长", "录制时间", "状态", "讲师"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setTextElideMode(Qt.ElideRight)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for i in (1, 3):
            self.tree.header().setSectionResizeMode(i, QHeaderView.Fixed)
            self.tree.setColumnWidth(i, 110)
        for i in (2, 4):
            self.tree.hideColumn(i)
        self.tree.itemChanged.connect(self._on_tree_item_changed)
        self.tree.itemDoubleClicked.connect(self._open_resource_result)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        lesson_layout.addWidget(self.tree, 1)
        self.catalog_empty = QLabel("还没有课程\n登录学校账号后，点击「刷新课程」。")
        self.catalog_empty.setAlignment(Qt.AlignCenter)
        self.catalog_empty.setProperty("role", "empty")
        self.catalog_empty.setWordWrap(True)
        lesson_layout.addWidget(self.catalog_empty, 1)
        self.course_splitter.addWidget(lessons)
        self.course_splitter.setStretchFactor(1, 1)
        self.course_splitter.setSizes([230, 800])
        layout.addWidget(self.course_splitter, 1)
        self.lbl_catalog_stats = QLabel("尚未加载课程")
        self.lbl_catalog_stats.setProperty("role", "hint")
        layout.addWidget(self.lbl_catalog_stats)
        footer = QHBoxLayout()
        self.lbl_checked = QLabel("尚未选择录播")
        self.lbl_checked.setWordWrap(True)
        footer.addWidget(self.lbl_checked, 1)
        footer.addWidget(self._action_button(self.act_enqueue))
        self.btn_transcribe = QPushButton("开始转写")
        self.btn_transcribe.setProperty("role", "primary")
        self.btn_transcribe.clicked.connect(self.on_transcribe_selected)
        footer.addWidget(self.btn_transcribe)
        layout.addLayout(footer)
        return page


    def _build_center_panel(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.task_layout = layout
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(14)
        heading = QLabel("转写任务")
        self.task_hero = heading
        heading.setProperty("role", "heading")
        layout.addWidget(heading)
        self.lbl_queue_stats = QLabel("选择录播后，任务会出现在这里。")
        self.lbl_queue_stats.setProperty("role", "hint")
        layout.addWidget(self.lbl_queue_stats)
        row = QHBoxLayout()
        self.task_filter = QComboBox()
        for text, key in (("全部任务", "all"), ("待处理", "pending"), ("进行中", "running"), ("已完成", "done"), ("需处理", "attention")):
            self.task_filter.addItem(text, key)
        self.task_filter.currentIndexChanged.connect(self._filter_tasks)
        row.addWidget(self.task_filter)
        row.addStretch()
        row.addWidget(self._action_button(self.act_start, primary=True))
        row.addWidget(self._action_button(self.act_pause))
        row.addWidget(self._action_button(self.act_stop))
        layout.addLayout(row)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["录播 / 课程", "课程", "当前阶段", "进度", "重试", "耗时", "错误详情", "操作"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(64)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False)
        hdr = self.table.horizontalHeader()
        hdr.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in ((2, 130), (3, 140), (7, 100)):
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)
        for col in (1, 4, 5, 6):
            self.table.hideColumn(col)
        self.table.itemSelectionChanged.connect(self._show_task_details)
        layout.addWidget(self.table, 1)
        self.task_empty = QLabel("还没有转写任务\n到「我的课程」选择录播，然后开始转写。")
        self.task_empty.setAlignment(Qt.AlignCenter)
        self.task_empty.setProperty("role", "empty")
        self.task_empty.setWordWrap(True)
        layout.addWidget(self.task_empty, 1)
        self.detail_panel = QFrame()
        self.detail_panel.setProperty("role", "card")
        detail_layout = QVBoxLayout(self.detail_panel)
        self.task_details = QLabel()
        self.task_details.setTextFormat(Qt.PlainText)
        self.task_details.setWordWrap(True)
        self.task_details.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        detail_layout.addWidget(self.task_details)
        self.output_row = QHBoxLayout()
        detail_layout.addLayout(self.output_row)
        self.detail_panel.hide()
        layout.addWidget(self.detail_panel)
        row2 = QHBoxLayout()
        self.btn_queue_requeue = QPushButton("继续处理选中")
        self.btn_queue_requeue.clicked.connect(lambda: self.on_requeue_selected(force=False))
        self.btn_queue_remove = QPushButton("移除选中")
        self.btn_queue_remove.clicked.connect(self.on_remove_selected_tasks)
        row2.addWidget(self.btn_queue_requeue)
        row2.addWidget(self.btn_queue_remove)
        row2.addStretch()
        more = QToolButton()
        more.setText("更多操作")
        more.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(more)
        self.btn_queue_force = menu.addAction("重新识别选中…", lambda: self.on_requeue_selected(force=True))
        self.btn_queue_clear = menu.addAction("清空任务列表…", self.on_clear_tasks)
        more.setMenu(menu)
        row2.addWidget(more)
        layout.addLayout(row2)
        return page


    def _build_right_panel(self) -> QWidget:
        box = QFrame()
        box.setProperty("role", "card")
        layout = QVBoxLayout(box)
        header = QHBoxLayout()
        header.addWidget(QLabel("详细日志"))
        header.addStretch()
        self.chk_autoscroll = QCheckBox("自动滚动")
        self.chk_autoscroll.setChecked(True)
        header.addWidget(self.chk_autoscroll)
        for label, callback in (("复制", self.on_copy_log), ("清空视图", self.on_clear_log), ("日志文件", self.on_open_log_dir)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            header.addWidget(button)
        layout.addLayout(header)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(6000)
        self.log_view.setFont(QFont(theme.MONO_FAMILIES[0], theme.LOG_FONT_PT))
        layout.addWidget(self.log_view, 1)
        self.lbl_paths = QLabel()
        self.lbl_paths.hide()
        layout.addWidget(self.lbl_paths)
        return box

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not hasattr(self, "log_panel"):
            return
        compact = self.height() < 680
        if compact == getattr(self, "_compact_layout", None):
            return
        self._compact_layout = compact
        for label in (self.course_hero, self.course_hint, self.task_hero, self.setup_text,
                      self.lbl_catalog_stats, self.course_heading, self.lbl_queue_stats):
            label.setVisible(not compact)
        self.shell_layout.setContentsMargins(*( (12, 6, 12, 6) if compact else (24, 12, 24, 12) ))
        self.shell_layout.setSpacing(6 if compact else 12)
        for layout in (self.course_layout, self.task_layout):
            layout.setContentsMargins(*( (10, 8, 10, 8) if compact else (20, 20, 20, 16) ))
            layout.setSpacing(6 if compact else 14)
        self.log_panel.setMaximumHeight(110 if compact else 230)
        self.log_panel.layout().setContentsMargins(*( (6, 6, 6, 6) if compact else (11, 11, 11, 11) ))
        self.log_panel.layout().setSpacing(4 if compact else 6)
        self.log_view.setMinimumHeight(45 if compact else 70)
        if compact and self.width() < 1000:
            self.btn_courses.setChecked(False)


    def _build_statusbar(self) -> None:
        sb = QStatusBar()
        self.lbl_toolbar_status = QLabel("就绪")
        self.lbl_toolbar_status.setMinimumWidth(0)
        self.lbl_toolbar_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        sb.addWidget(self.lbl_toolbar_status, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFixedWidth(150)
        self.progress.setTextVisible(False)
        sb.addPermanentWidget(self.progress)
        self.btn_logs = QPushButton("查看详细日志")
        self.btn_logs.setCheckable(True)
        self.btn_logs.toggled.connect(self.log_panel.setVisible)
        sb.addPermanentWidget(self.btn_logs)
        self.setStatusBar(sb)
        self.refresh_login_hint()


    def _startup_hints(self) -> None:
        # 这是一个 200ms 后触发的定时回调：如果用户在 200ms 内就关掉窗口
        # （或程序化地 close + 关闭状态库），回调再来跑就会踩到已关闭的数据库
        # （``sqlite3.ProgrammingError: Cannot operate on a closed database``）。
        # 这里做一次守卫，让「快速关闭」不会打出吓人的堆栈。
        if not self.isVisible():
            return
        self._update_setup()
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
            # 首屏直接给出行动入口；联网诊断由用户点击使用检查触发。
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
        self.catalog_empty.setText("正在刷新课程，请稍候…")
        self.catalog_empty.show()
        self._set_toolbar_status("正在拉取清单…")
        self.catalog_worker = CatalogWorker(self.cfg, self.cm)
        self.catalog_worker.status.connect(self._append_log_info)
        self.catalog_worker.finished_ok.connect(self._on_catalog_finished)
        self.catalog_worker.start()

    def _on_catalog_finished(self, catalog: object, error: str) -> None:
        self.act_refresh.setEnabled(True)
        if catalog is None:
            self.catalog_empty.setText("课程加载失败，请检查登录与网络后重试。")
            self.catalog_empty.show()
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
        self._login_verified = True
        self.refresh_login_hint()
        self._reload_tasks()
        self._update_setup()

    def _render_tree(self) -> None:
        self.tree.blockSignals(True)
        selected = {r.unique_key for r in self.checked_resources()}
        self.tree.setRootIndex(QModelIndex())
        self.tree.clear()
        self._resource_index.clear()
        self.course_list.blockSignals(True)
        self.course_list.clear()
        if self.catalog is None:
            self.tree.blockSignals(False)
            self.course_list.blockSignals(False)
            return
        task_by_res = {
            (t.course_id, t.resource_id): t for t in self.store.list_tasks(include_deleted=False)
        }
        for course in self.catalog.courses:
            listing = QListWidgetItem(f"{course.course_name}\n{len(course.resources)} 节录播 · {course.teacher or '课程'}")
            listing.setData(Qt.UserRole, course.course_id)
            listing.setToolTip(course.course_name)
            self.course_list.addItem(listing)
            total = sum(r.duration_sec for r in course.resources)
            node = QTreeWidgetItem([
                f"{course.course_name}（{len(course.resources)} 条）",
                media.human_duration(total),
                "",
                "",
                course.teacher,
            ])
            node.setFirstColumnSpanned(False)
            node.setSizeHint(0, QSize(0, 60))
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
                    res.title + "\n" + " · ".join(x for x in (course.course_name, res.teacher or course.teacher, res.record_time[:10]) if x),
                    media.human_duration(res.duration_sec),
                    res.record_time,
                    state,
                    res.teacher or course.teacher,
                ])
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Checked if res.unique_key in selected else Qt.Unchecked)
                child.setData(0, Qt.UserRole, {"kind": "resource", "key": res.unique_key})
                if task and task.stage == str(Stage.DONE):
                    child.setForeground(0, QColor(STAGE_COLOR["done"]))
                elif task and task.stage == str(Stage.FAILED):
                    child.setForeground(0, QColor(STAGE_COLOR["failed"]))
                child.setToolTip(0, res.title)
                child.setSizeHint(0, QSize(0, 60))
                node.addChild(child)
                self._resource_index[res.unique_key] = res
        self.tree.blockSignals(False)
        self.course_list.blockSignals(False)
        chosen = next((i for i in range(self.course_list.count()) if self.course_list.item(i).data(Qt.UserRole) == self._selected_course), 0)
        if self.course_list.count():
            self.course_list.setCurrentRow(chosen)
        self.lbl_catalog_stats.setText(
            (f"{len(self.catalog.courses)} 门课程 · {len(self.catalog.resources)} 节录播 · "
             f"共 {media.human_duration(sum(r.duration_sec for r in self.catalog.resources))}"
             if self.catalog else "尚未加载课程")
        )
        self._apply_filter(self.edit_search.text())

    def _choose_course(self, current, previous=None) -> None:
        if current:
            self._selected_course = current.data(Qt.UserRole)
        self._apply_filter(self.edit_search.text())

    def _apply_filter(self, text: str) -> None:
        if not hasattr(self, "tree"):
            return
        needle = (text or "").strip().lower()
        self.tree.setRootIndex(QModelIndex())
        count = 0
        for i in range(self.tree.topLevelItemCount()):
            course = self.tree.topLevelItem(i)
            cid = course.data(0, Qt.UserRole)["course_id"]
            course_match = needle in course.text(0).lower() or needle in course.text(4).lower()
            visible = 0
            for j in range(course.childCount()):
                child = course.child(j)
                match = not needle or course_match or needle in " ".join(child.text(c) for c in range(5)).lower()
                child.setHidden(not match)
                visible += int(match)
            show_course = bool(visible) if needle else cid == self._selected_course
            course.setHidden(not show_course)
            if show_course:
                count += visible
                if not needle:
                    self.tree.setRootIndex(self.tree.indexFromItem(course))
                    self.course_heading.setText(course.text(0))
            course.setExpanded(True)
        if needle:
            self.course_heading.setText(f"搜索结果 · {count} 节录播")
        self.tree.setVisible(count > 0)
        self.catalog_empty.setVisible(count == 0)
        if self.catalog is not None:
            self.catalog_empty.setText("没有匹配的录播\n换个关键词试试。" if needle else "这门课程还没有录播。")
        self._update_checked_stats()

    def _resource_visible(self, child) -> bool:
        return not child.isHidden() and not child.parent().isHidden()

    def _clear_selection(self) -> None:
        self.tree.blockSignals(True)
        for child in self._iter_resource_items():
            child.setCheckState(0, Qt.Unchecked)
        self.tree.blockSignals(False)
        self._update_checked_stats()


    def _iter_resource_items(self):
        for i in range(self.tree.topLevelItemCount()):
            course_item = self.tree.topLevelItem(i)
            for j in range(course_item.childCount()):
                yield course_item.child(j)

    def _set_all_checked(self, checked: bool) -> None:
        self.tree.blockSignals(True)
        for child in self._iter_resource_items():
            if self._resource_visible(child):
                child.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
        self.tree.blockSignals(False)
        self._update_checked_stats()

    def _check_untranscribed(self) -> None:
        done = {(t.course_id, t.resource_id) for t in self.store.list_tasks(stages=[str(Stage.DONE)])}
        self.tree.blockSignals(True)
        for child in self._iter_resource_items():
            data = child.data(0, Qt.UserRole) or {}
            res = self._resource_index.get(data.get("key", ""))
            if res is not None and self._resource_visible(child):
                child.setCheckState(0, Qt.Checked if (res.course_id, res.resource_id) not in done else Qt.Unchecked)
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
        resources = self.checked_resources()
        total = sum(r.duration_sec for r in resources)
        hidden = sum(child.checkState(0) == Qt.Checked and not self._resource_visible(child) for child in self._iter_resource_items())
        extra = f"（含其他课程或筛选外 {hidden} 节）" if hidden else ""
        self.lbl_checked.setText(f"已勾选：{len(resources)} 条 · {media.human_duration(total)} {extra}")
        self.btn_transcribe.setEnabled(bool(resources))
        self.act_enqueue.setEnabled(bool(resources))
        self.btn_transcribe.setText("加入待办" if self._batch_active else "开始转写")
        pending = len(self._pending_tasks())
        self.btn_transcribe.setToolTip(f"已有 {pending} 条待办，开始后将与本次选择一起处理。" if pending else "转写选中的录播")

    def _pending_tasks(self) -> list[TaskRecord]:
        return [t for t in self.store.list_tasks() if t.stage in {"pending", "audio_ready"}]

    def on_transcribe_selected(self) -> None:
        if not self.checked_resources():
            return
        self.on_enqueue_checked()
        self.pages.setCurrentIndex(1)
        if not self._batch_active:
            self.on_start()

    def _open_resource_result(self, item, column=0) -> None:
        data = item.data(0, Qt.UserRole) or {}
        resource = self._resource_index.get(data.get("key"))
        if resource:
            task = self.store.find_task(resource.course_id, resource.resource_id)
            if task and task.stage == "done":
                self.pages.setCurrentIndex(1)
                self.task_filter.setCurrentIndex(0)
                row = self._task_row_of(task.id)
                if row is not None:
                    self.table.selectRow(row)


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
        self._set_toolbar_status(f"已加入 {added} 条待办" + ("，本批结束后点击开始" if self._batch_active else ""))

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
            if (before is None or before.deleted) and task.id:
                added += 1
        if not quiet and added:
            self._render_tree()
        return added

    def _reload_tasks(self) -> None:
        tasks = self.store.list_tasks(include_deleted=False)
        selected_ids = self._selected_task_ids()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self._task_stages.clear()
        self._row_of_task.clear()
        for row, task in enumerate(tasks):
            self._insert_task_row(row, task)
        self.table.blockSignals(False)
        self._filter_tasks()
        for tid in selected_ids:
            row = self._task_row_of(tid)
            if row is not None and not self.table.isRowHidden(row):
                self.table.selectRow(row)
        self._update_queue_stats()
        self._refresh_tree_states()
        self._show_task_details()

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

        self._task_stages[task.id] = task.stage
        title_cell = cell(task.title + "\n" + task.course, task.title)
        title_cell.setData(Qt.UserRole, task.id)
        self.table.setItem(row, 0, title_cell)
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
        bar.setTextVisible(False)
        bar.setToolTip(f"{int(task.progress)}%")
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

        btn = QPushButton("查看结果" if task.stage == "done" else "详情")
        btn.clicked.connect(lambda _=False, tid=task.id: self._select_task(tid))
        self.table.setCellWidget(row, 7, btn)

    def _update_queue_stats(self) -> None:
        stats = self.store.stats()
        parts = [f"{STAGE_LABEL.get(k, k)} {v}" for k, v in sorted(stats.items())]
        total = sum(stats.values())
        self.lbl_queue_stats.setText(f"共 {total} 条 · " + (" · ".join(parts) if parts else "选择录播后开始转写"))
        pending = len(self._pending_tasks())
        self.pages.setTabText(1, f"转写任务（{pending}）" if pending else "转写任务")
        self.act_start.setText(f"开始待办（{pending}）" if pending else "开始待办")

    def _task_row_of(self, task_id: int) -> int | None:
        return self._row_of_task.get(task_id)

    def _refresh_tree_states(self) -> None:
        if self.catalog is None:
            return
        self.tree.blockSignals(True)
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
        self.tree.blockSignals(False)

    def _selected_task_ids(self) -> list[int]:
        return [self.table.item(index.row(), 0).data(Qt.UserRole)
                for index in self.table.selectionModel().selectedRows()
                if not self.table.isRowHidden(index.row()) and self.table.item(index.row(), 0)]

    def _select_task(self, task_id: int) -> None:
        row = self._task_row_of(task_id)
        if row is not None:
            self.table.selectRow(row)
            self._show_task_details()

    def _filter_tasks(self) -> None:
        if not hasattr(self, "table"):
            return
        key = self.task_filter.currentData()
        groups = {"pending": {"pending", "audio_ready"}, "running": {"probing", "downloading", "splitting", "transcribing", "post_processing", "writing"}, "done": {"done"}, "attention": {"failed", "canceled", "skipped"}}
        count = 0
        for tid, row in self._row_of_task.items():
            visible = key == "all" or self._task_stages.get(tid) in groups.get(key, set())
            self.table.setRowHidden(row, not visible)
            count += int(visible)
        self.task_empty.setVisible(count == 0)
        self.table.setVisible(count > 0)
        self.task_empty.setText("这个分类还没有任务。" if self.table.rowCount() else "还没有转写任务\n到「我的课程」选择录播，然后开始转写。")
        self._show_task_details()

    def _show_task_details(self) -> None:
        if not hasattr(self, "detail_panel"):
            return
        ids = self._selected_task_ids()
        task = self.store.get_task(ids[0]) if len(ids) == 1 else None
        self.detail_panel.setVisible(task is not None)
        while self.output_row.count():
            entry = self.output_row.takeAt(0)
            if entry.widget():
                entry.widget().deleteLater()
        if task is None:
            return
        elapsed = media.human_duration(max(0, (task.finished_at or time.time()) - task.started_at)) if task.started_at else "尚未开始"
        self.task_details.setText(f"{task.title}\n{STAGE_LABEL.get(task.stage, task.stage)} · 用时 {elapsed} · 已重试 {task.retry} 次" + (f"\n{task.error[:220]}" if task.error else ""))
        self.task_details.setToolTip(task.error)
        if task.error:
            action = "login" if "登录" in task.error else "settings" if any(word in task.error.lower() for word in ("key", "配置", "401")) else "details"
            button = QPushButton({"login": "重新登录", "settings": "修改设置", "details": "查看完整错误"}[action])
            if action == "details":
                button.clicked.connect(lambda: QMessageBox.warning(self, "任务详情", task.error))
            else:
                button.clicked.connect(self.on_login if action == "login" else self.on_settings)
            self.output_row.addWidget(button)
        if task.stage == "done":
            labels = {".txt": "打开文稿", ".srt": "打开字幕", ".md": "打开笔记"}
            for file in task.outputs:
                path = Path(file)
                if path.suffix.lower() in labels:
                    button = QPushButton(labels[path.suffix.lower()])
                    button.clicked.connect(lambda _=False, p=path: self._open_existing(p))
                    self.output_row.addWidget(button)
        folder = QPushButton("打开文件夹")
        folder.clicked.connect(lambda: self._open_existing(Path(task.output_dir or self.cfg.resolved_output_dir())))
        self.output_row.addWidget(folder)
        self.output_row.addStretch()

    def _open_existing(self, path: Path) -> None:
        if not path.exists():
            self._show_notice("文件已移动或不存在，请检查保存位置。")
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve()))):
            self._show_notice("无法打开文件，请检查是否安装了对应的阅读软件。")

    def _allow_task_edit(self) -> bool:
        if self._batch_active or (self.pipeline_worker is not None and self.pipeline_worker.isRunning()):
            self._show_notice("请先停止当前批次，再移除任务或重新识别。")
            return False
        return True


    def on_remove_selected_tasks(self) -> None:
        if not self._allow_task_edit():
            return
        ids = self._selected_task_ids()
        if not ids:
            QMessageBox.information(self, "未选中", "请先在中间列表选中任务。")
            return
        for tid in ids:
            self.store.delete_task(tid, hard=False)
        self._append_log("INFO", f"已移除 {len(ids)} 条任务（软删除，产物文件保留）")
        self._reload_tasks()

    def on_requeue_selected(self, *, force: bool) -> None:
        if not self._allow_task_edit():
            return
        ids = self._selected_task_ids()
        if not ids:
            QMessageBox.information(self, "未选中", "请先在中间列表选中任务。")
            return
        if force and QMessageBox.question(self, "重新识别", "将重新获取音频并识别，可能再次产生识别费用。已有文稿会在写出时备份。继续吗？") != QMessageBox.Yes:
            return
        for tid in ids:
            task = self.store.get_task(tid)
            self.store.reset_for_rerun(tid, keep_audio=not force)
            if force and task:
                self.store.update_stage(tid, Stage.PENDING, meta={**task.meta, "force_retranscribe": True})
        self._reload_tasks()
        self._append_log("INFO", f"已把 {len(ids)} 条任务打回待处理（{'强制重跑' if force else '续跑'}）")

    def on_clear_tasks(self) -> None:
        if not self._allow_task_edit():
            return
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
        pending = self._pending_tasks()
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
        self._batch_active = True
        worker.finished.connect(self._worker_finished)
        self.act_pause.setEnabled(True)
        self.act_stop.setEnabled(True)
        self._update_checked_stats()
        self.pages.setCurrentIndex(1)
        worker.start()
        self.act_start.setEnabled(False)
        self._set_toolbar_status(f"队列运行中（{len(items)} 条）…")
        self._append_log("INFO", f"开始执行队列：{len(items)} 条任务")

    def _asr_not_configured(self) -> bool:
        from urllib.parse import urlparse
        provider = (self.cfg.asr_provider or "").lower()
        if provider in ("faster_whisper_local", "local", "faster-whisper"):
            from importlib.util import find_spec
            return find_spec("faster_whisper") is None
        if provider in ("none", ""):
            return True
        host = (urlparse(self.cfg.asr_base_url).hostname or "").lower()
        if provider == "openai_compatible" and host in {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}:
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
            self._set_toolbar_status("正在暂停，等待当前片段结束；可点继续取消暂停")
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
        self._task_stages[task_id] = stage
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
            bar.setToolTip(f"{int(progress)}%")
        if message:
            self._set_toolbar_status("已到达暂停点，点击继续恢复" if message.startswith(("已暂停", "⏸")) else STAGE_LABEL.get(stage, stage))
        self._filter_tasks()
        self.progress.setValue(int(progress))

    def _on_task_done(self, task_id: int, ok: bool, error: str) -> None:
        task = self.store.get_task(task_id)
        row = self._task_row_of(task_id)
        if row is not None and task is not None:
            self._fill_task_row(row, task)
        self._update_queue_stats()
        self._refresh_tree_states()
        self._filter_tasks()
        if not ok:
            if "登录" in error:
                self._show_notice("登录已过期，请重新登录后继续处理。", "login")
            else:
                self._show_notice("有任务未完成，可在「需处理」中查看原因并继续处理。")

    def _on_queue_finished(self, ok: int, fail: int) -> None:
        self._set_toolbar_status(f"本批结束：完成 {ok} 条，需处理 {fail} 条")
        self._append_log("INFO", f"队列结束：成功 {ok} 条，失败 {fail} 条")
        self._reload_tasks()
        # finished 信号到达前不允许启动第二个工作线程。
        if self.pipeline_worker is None or not self.pipeline_worker.isRunning():
            self._worker_finished()

    def _worker_finished(self) -> None:
        self._batch_active = False
        self.act_start.setEnabled(True)
        self.act_pause.setEnabled(False)
        self.act_stop.setEnabled(False)
        self.act_pause.setText("暂停")
        self._reload_tasks()
        self._update_checked_stats()
        pending = len(self._pending_tasks())
        if pending:
            self._set_toolbar_status(f"本批结束，还有 {pending} 条待办，可点击「开始待办」。")


    def on_readiness(self, *, silent: bool = False) -> None:
        """一键把「能不能开始用」查清楚：网络 / 登录态 / Chromium / ffmpeg / ASR。

        ``silent=True`` 时只在日志与状态栏输出，不弹对话框（用于首启自动检查）。
        """
        if self.probe_worker is not None and self.probe_worker.isRunning():
            if not silent:
                QMessageBox.information(self, "正在检查", "首启检查还在进行中，请稍候。")
            return
        self._readiness_silent = silent
        self._readiness_issues = []
        self.act_readiness.setEnabled(False)
        self._set_toolbar_status("正在进行首启检查…")
        self._append_log("INFO", "开始首启一键检查（网络 / 登录态 / Chromium / ffmpeg / ASR）…")

        w = ProbeWorker(self.cfg, self.cm)
        w.full_readiness = True
        w.result.connect(self._on_readiness_item)
        w.report.connect(self._on_readiness_report)
        w.finished_ready.connect(self._on_readiness_done)
        self.probe_worker = w
        w.start()

    def _on_readiness_item(self, name: str, ok: bool, message: str) -> None:
        self._append_log("INFO" if ok else "WARN", f"{name}：{message}")
        if not ok:
            if "浏览器" in name:
                issue = ("登录浏览器尚未就绪，可在设置中查看诊断详情。", "settings")
            elif "登录" in name:
                issue = ("需要重新登录学校账号，然后刷新课程。", "login")
            elif "语音" in name or "ASR" in name:
                issue = ("语音识别尚未就绪，请检查服务设置；本地服务需要先启动。", "settings")
            elif "ffmpeg" in name:
                issue = ("音频处理组件不可用，请在高级设置中检查组件路径。", "settings")
            elif "目录" in name:
                issue = ("当前文件保存位置不可写，请在设置中选择其他文件夹。", "settings")
            else:
                issue = ("连接检查未通过，请确认网络后重试；详细原因已记入日志。", "details")
            self._readiness_issues.append(issue)

    def _on_readiness_report(self, text: str) -> None:
        self._last_readiness = text

    def _on_readiness_done(self, ready: bool) -> None:
        self.act_readiness.setEnabled(True)
        text = getattr(self, "_last_readiness", "")
        self._set_toolbar_status("首启检查完成" + ("（全部就绪）" if ready else "（有未通过项）"))
        if not ready and getattr(self, "_readiness_issues", []):
            self._show_notice(*self._readiness_issues[0])
        elif ready:
            self.notice.hide()
        self._update_setup()
        self._append_log("INFO", "使用检查结果：\n" + text)
        if ready:
            self._show_notice("准备就绪，可以选择录播开始转写。")

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
            self._login_verified = True
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
        self._login_verified = False
        self._append_log("INFO", "已清除本地登录态。")
        self.refresh_login_hint()

    def refresh_login_hint(self) -> None:
        st = load_session_state()
        if st.is_empty():
            self.lbl_login.setText("登录态：未登录")
            self.lbl_login.setStyleSheet(f"color:{theme.DANGER}; font-weight:600;")
        else:
            when = time.strftime("%m-%d %H:%M", time.localtime(st.saved_at or 0))
            self.lbl_login.setText("已登录" if self._login_verified else "登录信息已保存 · 待验证")
            self.lbl_login.setStyleSheet(f"color:{theme.OK}; font-weight:600;")
        self._update_setup()

    def _update_setup(self) -> None:
        if not hasattr(self, "setup_card"):
            return
        saved = self._login_verified or not load_session_state().is_empty()
        configured = not self._asr_not_configured()
        has_catalog = self.catalog is not None
        self.setup_card.setVisible(not (saved and configured and has_catalog))
        if not saved:
            self.setup_text.setText("欢迎使用 · 先登录学校账号")
        elif not configured:
            self.setup_text.setText("还差一步 · 设置语音识别服务")
        else:
            self.setup_text.setText("准备开始 · 刷新课程，选择想复习的录播")

    def _on_auth_expired(self, message: str) -> None:
        self._login_verified = False
        self.refresh_login_hint()
        self._append_log("ERROR", f"登录态失效：{message}")
        self._show_notice("登录已过期，请重新登录后继续处理。", "login")
        self._set_toolbar_status("登录态失效，请重新登录")

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
            self._update_setup()
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
        self.lbl_toolbar_status.setToolTip(text)

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
        self._unsubscribe_log()
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
