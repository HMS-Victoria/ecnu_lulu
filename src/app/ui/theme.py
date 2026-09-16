"""界面主题：**自带的浅色高对比配色**（不跟随系统深色模式）。

为什么需要它
------------
用户实测反馈「字体对比度太低，灰灰的看不清」。根因不是"选了浅灰"，而是：

* 这台机器的 Windows 处于**深色模式**（``AppsUseLightTheme = 0``），
  Qt 于是给出**深色**背景；
* 而界面里有一批文字被硬编码成**深灰**（``#333`` / ``#444`` / ``#555``）——
  深灰字压在深色背景上，对比度极低，看起来就是"灰灰的、看不清"；
* 应用此前**没有设置自己的调色板**，所以外观随系统漂移，没人能保证可读性。

做法：启动时固定一套浅色高对比主题（Fusion + 显式 QPalette + QSS）。
配色都按 WCAG 2.1 挑过，正文对比度 ≥ 12:1，次要文字 ≥ 7:1（阈值见
``scripts/verify_contrast.py``，那里会把这些数字**算出来**而不是"看着差不多"）。

以后新增文字颜色请从这里取常量，不要写字面量 —— 否则又会随主题漂移。
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# 配色（全部按 WCAG 2.1 选过；括号里是相对下方背景的对比度）
# --------------------------------------------------------------------------- #
BG = "#ffffff"            # 主背景
BG_ALT = "#f3f5f7"        # 交替行 / 次级面板
BG_INPUT = "#ffffff"      # 输入框
BORDER = "#c8d1da"        # 分隔线、边框（非文字，只需可见）
BORDER_STRONG = "#9aa7b4"

TEXT = "#16191d"          # 正文        白底 16.9:1
TEXT_STRONG = "#000000"   # 标题        白底 21:1
TEXT_MUTED = "#414850"    # 次要说明    白底 9.5:1
TEXT_FAINT = "#5a636c"    # 最弱提示    白底 6.6:1（仍高于 AA 的 4.5:1）

ACCENT = "#0b4f9e"        # 链接 / 强调 白底 8.1:1
ACCENT_TEXT = "#ffffff"   # 强调底上的文字 8.1:1
OK = "#0b6b2e"            # 成功        白底 6.6:1
WARN = "#7a4b00"          # 警告        白底 7.6:1
DANGER = "#a4160c"        # 错误        白底 7.6:1

#: 状态提示条的底色 + 边框（文字仍用上面的高对比色）
NOTICE_BG = "#fff6e0"
NOTICE_BORDER = "#e0b95c"
INFO_BG = "#e8f1fd"
INFO_BORDER = "#8ab6e8"

#: 等宽日志区（浅底深字，长时间盯也不累）
LOG_BG = "#fbfcfd"
LOG_TEXT = "#16191d"

#: 字号：9pt 在 1080p 上偏小（用户反馈"看不清"的一部分）
BASE_FONT_PT = 10
LOG_FONT_PT = 10

#: 中文字体优先级（缺字时逐个回退）
FONT_FAMILIES = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "sans-serif")
MONO_FAMILIES = ("Cascadia Mono", "Consolas", "JetBrains Mono", "monospace")


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 2.1 对比度：(L1+0.05)/(L2+0.05)，1.0~21.0。"""
    l1, l2 = _luminance(fg), _luminance(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def _luminance(color: str) -> float:
    r, g, b = _rgb(color)
    def chan(v: float) -> float:
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def _rgb(color: str) -> tuple[int, int, int]:
    c = color.strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


# --------------------------------------------------------------------------- #
# 应用主题
# --------------------------------------------------------------------------- #
def stylesheet() -> str:
    """返回全局 QSS。所有颜色都来自本模块常量（不写字面量）。"""
    return f"""
    QWidget {{
        color: {TEXT};
        background-color: {BG};
    }}
    QMainWindow, QDialog {{ background-color: {BG}; }}

    QLabel {{ color: {TEXT}; background: transparent; }}
    QLabel[role="hint"] {{ color: {TEXT_MUTED}; }}
    QLabel[role="muted"] {{ color: {TEXT_FAINT}; }}
    QLabel[role="title"] {{ color: {TEXT_STRONG}; font-weight: 700; }}
    QLabel[role="danger"] {{ color: {DANGER}; }}
    QLabel[role="ok"] {{ color: {OK}; }}

    QGroupBox {{
        border: 1px solid {BORDER};
        border-radius: 6px;
        margin-top: 10px;
        padding: 10px 8px 8px 8px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {TEXT_STRONG};
    }}

    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
        color: {TEXT};
        background-color: {BG_INPUT};
        border: 1px solid {BORDER};
        border-radius: 4px;
        padding: 3px 6px;
        selection-background-color: {ACCENT};
        selection-color: {ACCENT_TEXT};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
    QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
        border: 1px solid {ACCENT};
    }}
    QComboBox QAbstractItemView {{
        color: {TEXT};
        background-color: {BG};
        selection-background-color: {INFO_BG};
        selection-color: {TEXT_STRONG};
    }}

    QPushButton {{
        color: {TEXT};
        background-color: {BG_ALT};
        border: 1px solid {BORDER_STRONG};
        border-radius: 4px;
        padding: 5px 14px;
        min-height: 18px;
    }}
    QPushButton:hover {{ background-color: #e7ebef; }}
    QPushButton:pressed {{ background-color: #dbe1e7; }}
    QPushButton:disabled {{ color: {TEXT_FAINT}; background-color: {BG_ALT}; border-color: {BORDER}; }}
    QPushButton[role="primary"] {{
        color: {ACCENT_TEXT}; background-color: {ACCENT}; border: 1px solid {ACCENT};
        font-weight: 600;
    }}
    QPushButton[role="primary"]:hover {{ background-color: #0a4489; }}
    QPushButton[role="primary"]:disabled {{ background-color: #9db8d6; border-color: #9db8d6; }}

    QToolBar {{ background-color: {BG_ALT}; border-bottom: 1px solid {BORDER}; spacing: 4px; }}
    QToolBar QToolButton {{ color: {TEXT}; padding: 4px 8px; border-radius: 4px; }}
    QToolBar QToolButton:hover {{ background-color: #e2e7ec; }}
    QToolBar QToolButton:disabled {{ color: {TEXT_FAINT}; }}

    QStatusBar {{ color: {TEXT}; background-color: {BG_ALT}; }}
    QStatusBar::item {{ border: none; }}

    QTreeWidget, QTableWidget, QListWidget {{
        color: {TEXT};
        background-color: {BG};
        alternate-background-color: {BG_ALT};
        border: 1px solid {BORDER};
        gridline-color: {BORDER};
        selection-background-color: {ACCENT};
        selection-color: {ACCENT_TEXT};
    }}
    QTreeWidget::item, QTableWidget::item, QListWidget::item {{ padding: 3px 2px; }}
    QTreeWidget::item:selected, QTableWidget::item:selected, QListWidget::item:selected {{
        color: {ACCENT_TEXT}; background-color: {ACCENT};
    }}
    QHeaderView::section {{
        color: {TEXT_STRONG};
        background-color: {BG_ALT};
        border: none;
        border-right: 1px solid {BORDER};
        border-bottom: 1px solid {BORDER};
        padding: 5px 6px;
        font-weight: 600;
    }}
    QTableCornerButton::section {{ background-color: {BG_ALT}; border: 1px solid {BORDER}; }}

    QTabWidget::pane {{ border: 1px solid {BORDER}; }}
    QTabBar::tab {{
        color: {TEXT}; background: {BG_ALT};
        border: 1px solid {BORDER}; border-bottom: none;
        padding: 6px 14px; margin-right: 2px;
    }}
    QTabBar::tab:selected {{ color: {TEXT_STRONG}; background: {BG}; font-weight: 600; }}
    QTabBar::tab:hover {{ background: #e7ebef; }}

    QCheckBox, QRadioButton {{ color: {TEXT}; }}
    QCheckBox:disabled, QRadioButton:disabled {{ color: {TEXT_FAINT}; }}

    QMenu {{ color: {TEXT}; background-color: {BG}; border: 1px solid {BORDER}; }}
    QMenu::item:selected {{ color: {TEXT_STRONG}; background-color: {INFO_BG}; }}
    QMenuBar {{ color: {TEXT}; background-color: {BG_ALT}; }}
    QMenuBar::item:selected {{ background-color: #e2e7ec; }}

    QScrollBar:vertical {{ background: {BG_ALT}; width: 12px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {BORDER_STRONG}; border-radius: 5px; min-height: 24px; }}
    QScrollBar::handle:vertical:hover {{ background: #7d8b99; }}
    QScrollBar:horizontal {{ background: {BG_ALT}; height: 12px; margin: 0; }}
    QScrollBar::handle:horizontal {{ background: {BORDER_STRONG}; border-radius: 5px; min-width: 24px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}

    QToolTip {{
        color: {TEXT_STRONG};
        background-color: {NOTICE_BG};
        border: 1px solid {NOTICE_BORDER};
        padding: 4px 6px;
    }}
    QProgressBar {{
        color: {TEXT_STRONG};
        background-color: {BG_ALT};
        border: 1px solid {BORDER};
        border-radius: 4px;
        text-align: center;
    }}
    QProgressBar::chunk {{ background-color: {ACCENT}; }}

    QSplitter::handle {{ background-color: {BORDER}; }}
    """


def apply_theme(app) -> None:
    """把主题装到 QApplication 上（幂等，可重复调用）。"""
    from PySide6.QtGui import QColor, QFont, QPalette
    from PySide6.QtWidgets import QApplication, QStyleFactory

    if QStyleFactory.keys() and "Fusion" in QStyleFactory.keys():
        # Fusion 在各 Windows 版本上表现一致，且完全尊重我们给的调色板
        app.setStyle(QStyleFactory.create("Fusion"))

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(BG))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Base, QColor(BG_INPUT))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(BG_ALT))
    pal.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_FAINT))
    pal.setColor(QPalette.ColorRole.Button, QColor(BG_ALT))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(NOTICE_BG))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT_STRONG))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(ACCENT_TEXT))
    pal.setColor(QPalette.ColorRole.Link, QColor(ACCENT))
    pal.setColor(QPalette.ColorRole.BrightText, QColor(DANGER))
    # 禁用态：仍然要看得见（不要把文字压到几乎与背景同色）
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(TEXT_FAINT))
    app.setPalette(pal)

    font = QFont()
    for family in FONT_FAMILIES:
        font.setFamily(family)
        if QFont(family).exactMatch():
            break
    font.setPointSize(BASE_FONT_PT)
    app.setFont(font)

    app.setStyleSheet(stylesheet())
    _ = QApplication  # 仅为类型检查保留引用

    # 把关键值写进日志：用户反馈"看不清"时，第一件事就是确认主题到底有没有生效
    # （打包态也能在 logs/app.log 里看到这一行，不用猜）
    try:
        from ecnu_transcribe.logbus import get_logger

        get_logger("theme").info(
            "界面主题：浅色高对比（正文 %s / 底 %s，对比度 %.1f:1，%d pt）",
            TEXT, BG, contrast_ratio(TEXT, BG), font.pointSize(),
        )
    except Exception:  # noqa: BLE001 — 日志失败不能影响界面
        pass
