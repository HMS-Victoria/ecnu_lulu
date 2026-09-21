"""仅供隔离副本验收：复制为副本 scripts/doctor.py，再运行该副本 EXE --doctor。

不通过源码解释器执行；模块由 PyInstaller 冻结归档导入。
UI_VERIFY_OUT 指向证据目录，LOCALAPPDATA 必须指向隔离目录。
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts"))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase
from app.ui import theme
from app.ui.main_window import MainWindow
from app.ui.settings_dialog import SettingsDialog
from ecnu_transcribe import __version__, paths
from ecnu_transcribe.catalog import Catalog, Course, Resource
from ecnu_transcribe.config import ConfigManager
from ecnu_transcribe.store import StateStore, Stage

assert getattr(sys, "frozen", False), "必须运行冻结态副本"
out = Path(os.environ["UI_VERIFY_OUT"])
out.mkdir(parents=True, exist_ok=True)
app = QApplication([])
families = QFontDatabase.families()
for family in ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei"):
    if family in families:
        theme.FONT_FAMILIES = (family, *theme.FONT_FAMILIES)
        break
theme.apply_theme(app)
cm = ConfigManager()
cm.load()
cm.cfg.asr_provider = "openai_compatible"
cm.cfg.asr_base_url = "http://127.0.0.1:1/v1"
store = StateStore()
win = MainWindow(cm, store)
win._startup_hints = lambda: None
win.setWindowTitle("v0.12.0 冻结态验收 · 模拟数据")
report = {"version": __version__, "frozen": True, "platform": app.platformName(), "checks": []}

def check(name, ok):
    report["checks"].append({"name": name, "ok": bool(ok)})

try:
    resource = Resource(resource_id="demo-recording", course_id="demo-course", title="第 1 讲 · 课堂复习", duration_sec=120)
    course = Course(course_id="demo-course", course_name="演示课程", resources=[resource])
    win.catalog = Catalog(courses=[course])
    win._render_tree()
    win.resize(1240, 820)
    win.show()
    app.processEvents()
    check("version", __version__ == "0.12.0")
    check("course and task pages", win.pages.count() == 2)
    check("logs collapsed", win.log_panel.isHidden())
    win.tree.topLevelItem(0).child(0).setCheckState(0, Qt.Checked)
    win.on_enqueue_checked()
    check("enqueue stable resource", len(store.list_tasks()) == 1 and store.list_tasks()[0].resource_id == resource.resource_id)
    app.processEvents()
    win.grab().save(str(out / "frozen-courses.png"))
    task = store.list_tasks()[0]
    output = paths.output_dir() / "演示文稿.txt"
    output.write_text("冻结态验收的合成内容。", encoding="utf-8-sig")
    store.update_stage(task.id, Stage.DONE, force=True, progress=100, outputs=[str(output)], output_dir=str(output.parent))
    win._reload_tasks()
    win.pages.setCurrentIndex(1)
    win.task_filter.setCurrentIndex(win.task_filter.findData("done"))
    win._select_task(task.id)
    app.processEvents()
    check("filtered task identity", win._selected_task_ids() == [task.id])
    check("output details", win.detail_panel.isVisible() and win.output_row.count() > 0)
    win.grab().save(str(out / "frozen-tasks.png"))
    dlg = SettingsDialog(cm.cfg, cm)
    dlg.show()
    app.processEvents()
    check("four settings categories", dlg.navigation.count() == 4)
    dlg.navigation.setCurrentRow(2)
    app.processEvents()
    check("settings navigation", dlg.tabs.currentIndex() == 2)
    dlg.grab().save(str(out / "frozen-settings.png"))
    dlg.reject()
    try:
        from faster_whisper import WhisperModel
        report["local_engine_import"] = "ok (model loading not tested)"
        check("local engine import", True)
    except Exception as exc:
        report["local_engine_import"] = f"{type(exc).__name__}: {exc}"
        check("local engine import", False)
finally:
    win.close()
    store.close()
    (out / "frozen-ui.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if all(c["ok"] for c in report["checks"]) else 1)
