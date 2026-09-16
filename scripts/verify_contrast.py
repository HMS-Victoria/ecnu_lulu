"""界面可读性验证：**算对比度** + **真截图**（跑一次，两项证据都拿到）。

为什么要写成脚本：用户反馈"字体对比度太低、灰灰的看不清"。根因是应用没有自带主题，
在系统**深色模式**下用了深灰/纯黑文字。修复后不能只靠"我看着还行"，所以这里

1. 用 WCAG 2.1 公式把主题里每一对「文字色 / 背景色」的对比度**算出来**并对照阈值；
2. 用**离屏真实渲染**主窗口与设置对话框，落两张 PNG —— 颜色是否真的生效、字号是否够大，
   看图和看数字互为佐证（此前"看着通过其实没测到点子上"的教训太多了）。

用法：
    python scripts/verify_contrast.py            # 审计 + 截图
    QT_QPA_PLATFORM=offscreen python scripts/verify_contrast.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.ui import theme  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '⛔'} {name}" + (f"  — {detail}" if detail else ""))


#: （说明，文字色，背景色，最低要求）
#: AA 正文 4.5:1；大号/次要文字 3:1；这里对正文一律按 7:1（AAA）要求，留足余量。
PAIRS = [
    ("正文（标签/表格/树）", theme.TEXT, theme.BG, 7.0),
    ("标题（加粗）", theme.TEXT_STRONG, theme.BG, 7.0),
    ("次要说明文字", theme.TEXT_MUTED, theme.BG, 7.0),
    ("最弱提示文字", theme.TEXT_FAINT, theme.BG, 4.5),
    ("正文 / 交替行底色", theme.TEXT, theme.BG_ALT, 7.0),
    ("输入框文字", theme.TEXT, theme.BG_INPUT, 7.0),
    ("日志区文字", theme.LOG_TEXT, theme.LOG_BG, 7.0),
    ("选中项文字 / 选中底", theme.ACCENT_TEXT, theme.ACCENT, 4.5),
    ("主按钮文字 / 按钮底", theme.ACCENT_TEXT, theme.ACCENT, 4.5),
    ("链接与强调色", theme.ACCENT, theme.BG, 4.5),
    ("成功状态色", theme.OK, theme.BG, 4.5),
    ("警告状态色", theme.WARN, theme.BG, 4.5),
    ("错误状态色", theme.DANGER, theme.BG, 4.5),
    ("提示条文字 / 提示底", theme.TEXT, theme.NOTICE_BG, 7.0),
    ("信息条文字 / 信息底", theme.TEXT, theme.INFO_BG, 7.0),
    ("禁用态文字（仍需看得见）", theme.TEXT_FAINT, theme.BG_ALT, 4.5),
]


def audit_colors() -> None:
    print("\n[1] WCAG 2.1 对比度审计（阈值：正文 7:1，次要/状态 4.5:1）")
    print(f"    {'用途':<22}{'文字':<10}{'背景':<10}{'实测':>7}{'要求':>6}  判定")
    for label, fg, bg, need in PAIRS:
        ratio = theme.contrast_ratio(fg, bg)
        ok = ratio >= need
        print(f"    {label:<22}{fg:<10}{bg:<10}{ratio:>7.2f}{need:>6.1f}   {'✅' if ok else '⛔'}")
        check(f"对比度 {label} ≥ {need:.1f}", ok, f"{ratio:.2f}:1")

    # 反向保护：不该再出现"深色文字 + 深色背景"这类组合
    worst = min(theme.contrast_ratio(fg, bg) for _l, fg, bg, _n in PAIRS)
    check("所有组合的最低对比度 ≥ 4.5:1", worst >= 4.5, f"最低 {worst:.2f}:1")


def screenshot() -> None:
    print("\n[2] 离屏真实渲染截图（确认主题真的生效）")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from app.ui.main_window import MainWindow
    from app.ui.settings_dialog import SettingsDialog

    from ecnu_transcribe.catalog import Catalog, Course
    from ecnu_transcribe.config import ConfigManager
    from ecnu_transcribe.store import StateStore

    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app)

    # 主题是否落到应用上
    check("QApplication 已套用自带主题", app.styleSheet().strip() != "", "stylesheet 非空" if app.styleSheet() else "空")
    check("基础字号 ≥ 10pt", app.font().pointSize() >= 10, f"{app.font().pointSize()}pt")
    check("调色板正文色 = 主题色",
          app.palette().color(app.palette().ColorRole.WindowText).name().lower() == theme.TEXT.lower(),
          app.palette().color(app.palette().ColorRole.WindowText).name())

    out = ROOT / "build" / "ui_shots"
    out.mkdir(parents=True, exist_ok=True)

    cm = ConfigManager()
    store = StateStore()

    # 塞一份示例清单，让树/表里有内容可看
    win = MainWindow(cm, store)
    try:
        cat = Catalog(fetched_at="2026-09-14 15:00:00", student_id="20261234567", source="verify")
        cat.courses = [
            Course(course_id="381401", course_name="高等数学（二）", term="2025-2026 第1学期",
                   teacher="唐晓艳", resources=[]),
            Course(course_id="395443", course_name="世界政治经济地理", term="2025-2026 第1学期",
                   teacher="杜德斌", resources=[]),
        ]
        win.catalog = cat
        win._render_tree()  # noqa: SLF001
        win.log_view.appendPlainText("2026-09-14 15:00:00 INFO    ecnu_transcribe.pipeline: 开始处理《高等数学（二）》")
        win.log_view.appendPlainText("2026-09-14 15:00:01 INFO    ecnu_transcribe.downloader: 命中音频缓存（3301.5s）")
        win.resize(1500, 900)
        win.show()
        for _ in range(8):
            app.processEvents()
        main_png = out / "main_window.png"
        win.grab().save(str(main_png))
        check("主窗口截图已生成", main_png.is_file() and main_png.stat().st_size > 5000,
              f"{main_png.name} {main_png.stat().st_size // 1024 if main_png.is_file() else 0} KB")

        dlg = SettingsDialog(cm.load(), cm)
        dlg.resize(1000, 760)
        dlg.show()
        for _ in range(8):
            app.processEvents()
        dlg_png = out / "settings_dialog.png"
        dlg.grab().save(str(dlg_png))
        check("设置对话框截图已生成", dlg_png.is_file() and dlg_png.stat().st_size > 5000,
              f"{dlg_png.name} {dlg_png.stat().st_size // 1024 if dlg_png.is_file() else 0} KB")
        dlg.close()
    finally:
        win.close()
        store.close()
        QTimer.singleShot(0, app.quit)

    print(f"\n    截图目录：{out}")


def main() -> int:
    print("=" * 84)
    print("界面可读性验证（对比度 + 真实截图）")
    print("=" * 84)
    audit_colors()
    screenshot()
    print("\n" + "=" * 84)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    for f in FAIL:
        print(f"  ⛔ {f}")
    print("=" * 84)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
