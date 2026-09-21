"""温暖书房界面：隔离截图、布局检查及原生桌面验收入口。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", type=float, default=1)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--out", default=str(ROOT / "docs/ui-refactor/evidence/final"))
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix="ui-study-"))
    os.environ["ECNU_TRANSCRIBE_HOME"] = str(temp)
    os.environ["LOCALAPPDATA"] = str(temp / "user")
    os.environ["QT_QPA_PLATFORM"] = "windows" if args.native else "offscreen"
    os.environ["QT_SCALE_FACTOR"] = str(args.scale)
    os.environ["QT_QPA_FONTDIR"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts")
    sys.path.insert(0, str(ROOT / "src"))
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QApplication, QToolButton, QScrollArea
    from PySide6.QtTest import QTest
    from app.ui import theme
    from app.ui.main_window import MainWindow
    from app.ui.settings_dialog import SettingsDialog
    from ecnu_transcribe.config import ConfigManager
    from make_shots import build_catalog, build_store, pick_cjk_font
    from ecnu_transcribe.logbus import setup_logging

    app = QApplication([])
    if not args.native:
        font = pick_cjk_font()
        if font:
            theme.FONT_FAMILIES = (font, *theme.FONT_FAMILIES)
    theme.apply_theme(app)
    setup_logging()
    cm = ConfigManager()
    cm.load()
    cm.set_secret("asr_api_key", "demo-not-a-real-key")
    # 预览不访问真实平台或云端；用户点击检查也只会访问本机。
    cm.cfg.portal_url = "http://127.0.0.1:1/#/home"
    cm.cfg.api_base = "http://127.0.0.1:1"
    cm.cfg.asr_base_url = "http://127.0.0.1:1/v1"
    cm.cfg.asr_provider = "openai_compatible"
    cm.cfg.request_timeout = 3
    store = build_store(temp)
    win = MainWindow(cm, store)
    win._startup_hints = lambda: None
    win.setWindowTitle("大夏学堂 · 界面验收（模拟数据）")
    win.catalog = build_catalog()
    win._render_tree()
    win._login_verified = True
    win._update_setup()
    win.lbl_login.setText("演示账号 · 模拟数据")
    win.show()
    app.processEvents()
    if args.native:
        # 关闭后留下原生 Qt 窗口最终画面，交互证据另见 Computer Use 记录。
        app.aboutToQuit.connect(lambda: win.grab().save(str(out / "native-final.png")))
        return app.exec()

    report = {"scale": args.scale, "platform": app.platformName(), "checks": []}
    def check(label, ok, detail=""):
        report["checks"].append({"name": label, "ok": bool(ok), "detail": detail})

    def inside(window, widget):
        rect = widget.rect()
        top = widget.mapTo(window, QPoint(0, 0))
        return window.rect().contains(top) and window.rect().contains(top + rect.bottomRight())

    try:
        for physical in ((1366, 768), (1920, 1080)):
            width, height = (int(value / args.scale) for value in physical)
            tag = f"{physical[0]}x{physical[1]}-{args.scale:g}"
            win.resize(width - 20, height - 45)
            app.processEvents()
            win.resize(width - 20, height - 45)
            app.processEvents()
            check(tag + " window fits", win.width() <= width and win.height() <= height,
                  f"logical={win.width()}x{win.height()}, available={width}x{height}")
            win.pages.setCurrentIndex(0)
            win.tree.topLevelItem(0).child(0).setCheckState(0, Qt.Checked)
            app.processEvents()
            check(tag + " course controls", inside(win, win.btn_transcribe) and inside(win, win.edit_search))
            check(tag + " course no horizontal scroll", win.tree.horizontalScrollBar().maximum() == 0)
            win.grab().save(str(out / f"courses-{tag}.png"))
            win.pages.setCurrentIndex(1)
            app.processEvents()
            check(tag + " task no horizontal scroll", win.table.horizontalScrollBar().maximum() == 0)
            win.grab().save(str(out / f"tasks-{tag}.png"))
            win._select_task(store.list_tasks()[0].id)
            app.processEvents()
            check(tag + " task details fit", inside(win, win.detail_panel))
            win.grab().save(str(out / f"details-{tag}.png"))
            win.table.clearSelection()
            win.btn_logs.setChecked(True)
            app.processEvents()
            check(tag + " logs fit", inside(win, win.log_panel) and win.table.height() >= 80)
            win.grab().save(str(out / f"logs-{tag}.png"))
            win.btn_logs.setChecked(False)
            dlg = SettingsDialog(cm.cfg, cm)
            dlg.resize(min(980, width - 20), min(740, height - 45))
            dlg.show()
            app.processEvents()
            for index in range(4):
                dlg.navigation.setCurrentRow(index)
                app.processEvents()
                check(tag + f" settings {index} fits", dlg.width() <= width and dlg.height() <= height)
                dlg.grab().save(str(out / f"settings{index}-{tag}.png"))
                for button in dlg.tabs.currentWidget().findChildren(QToolButton):
                    if button.isCheckable() and not button.isChecked():
                        button.click()
                app.processEvents()
                check(tag + f" settings {index} expanded no horizontal scroll", all(
                    area.horizontalScrollBar().maximum() == 0
                    for area in dlg.tabs.currentWidget().findChildren(QScrollArea)))
            dlg.navigation.setFocus()
            QTest.keyClick(dlg.navigation, Qt.Key_Home)
            QTest.keyClick(dlg.navigation, Qt.Key_Down)
            check(tag + " keyboard settings navigation", dlg.tabs.currentIndex() == 1)
            dlg.close()
            win.pages.setCurrentIndex(0)
            win._login_verified = False
            win._update_setup()
            app.processEvents()
            check(tag + " first use fits", win.height() <= height and inside(win, win.btn_transcribe))
            win.grab().save(str(out / f"first-use-{tag}.png"))
            win._on_auth_expired("模拟登录过期；不涉及真实账号")
            app.processEvents()
            check(tag + " error action visible", inside(win, win.notice_action) and inside(win, win.btn_transcribe))
            win.grab().save(str(out / f"login-expired-{tag}.png"))
            win.notice.hide()
            win._login_verified = True
            win._update_setup()
        # 沿用项目原有的颜色验算，同时验证新的选中背景。
        from verify_contrast import PAIRS
        for label, fg, bg, minimum in PAIRS + [("selected text", theme.TEXT_STRONG, theme.INFO_BG, 4.5)]:
            ratio = theme.contrast_ratio(fg, bg)
            check(label, ratio >= minimum, f"{ratio:.2f} >= {minimum}")
    finally:
        win.close()
        store.close()
    (out / f"layout-{args.scale:g}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    failures = [item for item in report["checks"] if not item["ok"]]
    print(json.dumps({"checks": len(report["checks"]), "failures": failures}, ensure_ascii=False, indent=2))
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
