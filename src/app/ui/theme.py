"""温暖书房：固定调色板与原生 Qt 控件，保证系统深色模式下仍然清晰。"""
from __future__ import annotations

BG = "#FFFEFA"
BG_ALT = "#F6F3EC"
BG_INPUT = "#FFFEFA"
BORDER = "#DDDCD2"
BORDER_STRONG = "#8B978D"
TEXT = "#263A32"
TEXT_STRONG = "#203B2E"
TEXT_MUTED = "#4B574E"
TEXT_FAINT = "#59645A"
ACCENT = "#285846"
ACCENT_TEXT = "#FFFEFA"
OK = "#285846"
WARN = "#775016"
DANGER = "#A13229"
NOTICE_BG = "#FBF1DB"
NOTICE_BORDER = "#D3BE87"
INFO_BG = "#E7EEE5"
INFO_BORDER = "#A8BCA8"
LOG_BG = "#FFFEFA"
LOG_TEXT = TEXT
BASE_FONT_PT = 11
LOG_FONT_PT = 10
FONT_FAMILIES = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "sans-serif")
MONO_FAMILIES = ("Cascadia Mono", "Consolas", "monospace")

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
    return f"""
    QWidget {{ color: {TEXT}; background-color: {BG}; }}
    QMainWindow, QDialog, QStatusBar, QToolBar {{ background-color: {BG_ALT}; }}
    QLabel {{ background: transparent; }}
    QLabel[role="brand"] {{ font-size: 18px; font-weight: 600; padding: 8px 14px; }}
    QLabel[role="heading"] {{ font-size: 26px; font-weight: 700; color: {TEXT_STRONG}; }}
    QLabel[role="section"] {{ font-size: 17px; font-weight: 600; padding: 6px 0; }}
    QLabel[role="hint"], QLabel[role="muted"] {{ color: {TEXT_MUTED}; }}
    QLabel[role="empty"] {{ color: {TEXT_MUTED}; padding: 24px; font-size: 16px; }}
    QLabel[role="danger"] {{ color: {DANGER}; }}
    QLabel[role="ok"] {{ color: {OK}; }}
    QFrame[role="card"] {{ border: 1px solid {BORDER}; border-radius: 10px; }}
    QFrame[role="notice"] {{ background: {NOTICE_BG}; border: 1px solid {NOTICE_BORDER}; border-radius: 8px; }}
    QFrame[role="notice"] QLabel {{ background: transparent; }}
    QToolBar {{ border: none; padding: 10px 16px; spacing: 10px; }}
    QToolBar QWidget {{ background: transparent; }}
    QStatusBar {{ padding: 2px 18px; border-top: 1px solid {BORDER}; }}
    QStatusBar::item {{ border: none; }}
    QPushButton, QToolButton {{
        background: {BG}; color: {TEXT}; border: 1px solid {BORDER};
        border-radius: 7px; padding: 7px 12px; min-height: 20px;
    }}
    QPushButton:hover, QToolButton:hover {{ background: {INFO_BG}; border-color: {BORDER_STRONG}; }}
    QPushButton:pressed, QToolButton:pressed, QPushButton:checked {{ background: {INFO_BG}; border-color: {ACCENT}; }}
    QPushButton:focus, QToolButton:focus {{ border: 2px solid {ACCENT}; padding: 6px 11px; }}
    QPushButton[role="primary"], QToolButton[role="primary"] {{ background: {ACCENT}; color: {ACCENT_TEXT}; border-color: {ACCENT}; font-weight: 600; }}
    QPushButton[role="primary"]:hover, QToolButton[role="primary"]:hover {{ background: {TEXT_STRONG}; }}
    QPushButton:disabled, QToolButton:disabled, QPushButton[role="primary"]:disabled, QToolButton[role="primary"]:disabled {{ color: {TEXT_FAINT}; background: {BG_ALT}; border-color: {BORDER}; }}
    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
        color: {TEXT}; background: {BG_INPUT}; border: 1px solid {BORDER};
        border-radius: 6px; padding: 7px 8px; selection-background-color: {ACCENT}; selection-color: {ACCENT_TEXT};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
    QComboBox {{ min-width: 110px; padding-right: 24px; }}
    QComboBox QAbstractItemView {{ background: {BG}; color: {TEXT}; selection-background-color: {INFO_BG}; selection-color: {TEXT}; }}
    QTreeWidget, QTableWidget, QListWidget {{
        border: none; background: {BG}; alternate-background-color: {BG_ALT};
        selection-background-color: {INFO_BG}; selection-color: {TEXT_STRONG}; outline: 0;
    }}
    QTreeWidget::item, QTableWidget::item {{ padding: 7px 8px; border-bottom: 1px solid {BORDER}; }}
    QListWidget::item {{ padding: 12px; margin: 3px 0; border-radius: 8px; }}
    QTreeWidget::item:selected, QTableWidget::item:selected, QListWidget::item:selected {{ background: {INFO_BG}; color: {TEXT_STRONG}; }}
    QTreeWidget::item:hover, QTableWidget::item:hover, QListWidget::item:hover {{ background: {BG_ALT}; }}
    QTreeWidget:focus, QTableWidget:focus, QListWidget:focus {{ border: 1px solid {ACCENT}; }}
    QListWidget#courseList {{ background: {BG_ALT}; border-radius: 10px; }}
    QHeaderView::section {{ color: {TEXT_MUTED}; background: {BG}; border: none; border-bottom: 1px solid {BORDER}; padding: 10px 8px; }}
    QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 10px; background: {BG}; top: -1px; }}
    QTabBar::tab {{ background: {BG_ALT}; color: {TEXT_MUTED}; padding: 12px 24px; border-bottom: 3px solid transparent; }}
    QTabBar::tab:selected {{ color: {ACCENT}; border-bottom: 3px solid {ACCENT}; font-weight: 600; }}
    QTabBar::tab:hover {{ background: {INFO_BG}; }}
    QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
    QCheckBox:disabled, QRadioButton:disabled {{ color: {TEXT_FAINT}; }}
    QGroupBox {{ border: 1px solid {BORDER}; border-radius: 10px; margin-top: 16px; padding: 16px 12px 12px; font-weight: 600; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; }}
    QScrollArea {{ border: none; }}
    QScrollBar:vertical {{ background: {BG_ALT}; width: 10px; }}
    QScrollBar:horizontal {{ background: {BG_ALT}; height: 10px; }}
    QScrollBar::handle {{ background: {BORDER_STRONG}; border-radius: 4px; min-height: 24px; min-width: 24px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QProgressBar {{ background: {BG_ALT}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 5px; text-align: center; }}
    QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
    QSplitter::handle {{ background: {BG}; width: 8px; }}
    QMenu {{ background: {BG}; border: 1px solid {BORDER}; padding: 6px; }}
    QMenu::item {{ padding: 8px 20px; }}
    QMenu::item:selected {{ background: {INFO_BG}; color: {TEXT}; }}
    QToolTip {{ color: {TEXT}; background: {NOTICE_BG}; border: 1px solid {NOTICE_BORDER}; padding: 6px; }}
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
            "界面主题：温暖书房（正文 %s / 底 %s，对比度 %.1f:1，%d pt）",
            TEXT, BG, contrast_ratio(TEXT, BG), font.pointSize(),
        )
    except Exception:  # noqa: BLE001 — 日志失败不能影响界面
        pass
