"""生成 README / 作品展示用的界面截图（离屏真实渲染，不需要显示器）。

为什么要单独写这个脚本：`verify_contrast.py` 的截图只塞了两门空课程，用于**验证主题**
够用；但要放进 README 当作品展示，界面得是「有人真的在用」的样子 —— 课程树有内容、
任务队列有各阶段的任务、日志有真实格式的记录。这个脚本负责把这份演示数据喂进去。

演示数据全部是**虚构或已公开的课程名**，不含任何真实账号信息；任务记录写进
临时目录的状态库（`--tmp`），**不碰**你 `%LOCALAPPDATA%` 下的真实配置与任务。

用法：
    .venv\\Scripts\\python scripts\\make_shots.py                # 输出到 assets/shots/
    .venv\\Scripts\\python scripts\\make_shots.py --out build/ui_shots
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
#: 关键：offscreen 平台不带字体，必须显式指向系统字体目录，否则中文全是「豆腐块」。
#: （Windows 上直接用 C:\Windows\Fonts；实测 Qt 能从中读到 SimHei 等中文字体）
os.environ.setdefault("QT_QPA_FONTDIR", str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts"))


def pick_cjk_font() -> str | None:
    """挑一个**真的含中文字形**的字体族。

    offscreen 平台的字体回退不会自动避开不含 CJK 的字体（实测会选中 Segoe UI
    然后整屏豆腐块），所以这里主动挑一个并插到主题字体列表最前面。
    真实 Windows 桌面上 `Microsoft YaHei UI` 必然存在，本函数只是让截图脚本
    在没有该字体的环境里也能出图。
    """
    from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics

    families = set(QFontDatabase.families())
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DengXian"):
        if name not in families:
            continue
        fm = QFontMetrics(QFont(name, 12))
        # 真的能量出「中」字的宽度才算可用
        if fm.horizontalAdvance("中") > 0:
            return name
    return None


def build_catalog():
    """构造一份演示清单（结构真实、内容虚构/公开课程名）。"""
    from ecnu_transcribe.catalog import Catalog, Course, Resource

    seq = {"n": 700000}

    def res(title: str, cid: str, dur: float, when: str, teacher: str, cam: str = "") -> Resource:
        seq["n"] += 7
        return Resource(
            resource_id=str(seq["n"]),
            title=title + (f"（{cam}）" if cam else ""),
            course_id=cid,
            teacher=teacher,
            duration_sec=dur,
            record_time=when,
            mime="video/mp4",
        )

    cat = Catalog(fetched_at="2026-09-14 15:02:11", student_id="20261234567", source="demo")
    cat.courses = [
        Course(
            course_id="381401", course_name="高等数学（二）", term="2025-2026 第1学期",
            teacher="唐晓艳",
            resources=[
                res("第1讲 定积分的概念与性质", "381401", 3301, "2026-03-02 08:00", "唐晓艳"),
                res("第2讲 微积分基本定理", "381401", 3245, "2026-03-05 08:00", "唐晓艳"),
                res("第3讲 反常积分与敛散性判别", "381401", 3410, "2026-03-09 08:00", "唐晓艳"),
                res("第4讲 定积分的应用", "381401", 3188, "2026-03-12 08:00", "唐晓艳"),
            ],
        ),
        Course(
            course_id="395443", course_name="世界政治经济地理", term="2025-2026 第1学期",
            teacher="杜德斌",
            resources=[
                res("第1讲 导论：地理与政治经济格局", "395443", 5400, "2026-03-16 13:30", "杜德斌", "主机位"),
                res("第2讲 全球资源分布与地缘冲突", "395443", 5230, "2026-03-23 13:30", "杜德斌", "主机位"),
                res("第3讲 亚太区域经济一体化", "395443", 5012, "2026-03-30 13:30", "杜德斌", "主机位"),
            ],
        ),
        Course(
            course_id="364201", course_name="数据结构与算法", term="2025-2026 第2学期",
            teacher="张伟",
            resources=[
                res("第1讲 绪论与算法复杂度", "364201", 2820, "2026-06-18 13:00", "张伟"),
                res("第2讲 线性表：顺序存储与链式存储", "364201", 2955, "2026-06-20 13:00", "张伟"),
                res("第3讲 栈与队列", "364201", 2880, "2026-06-25 13:00", "张伟"),
                res("第4讲 二叉树遍历", "364201", 3010, "2026-06-27 13:00", "张伟"),
                res("第5讲 图的最短路径", "364201", 3120, "2026-07-02 13:00", "张伟"),
            ],
        ),
        Course(
            course_id="352210", course_name="软件工程数学", term="2025-2026 第2学期",
            teacher="李静",
            resources=[
                res("第1讲 命题逻辑与推理", "352210", 2700, "2026-03-31 09:50", "李静"),
                res("第2讲 集合与关系", "352210", 2760, "2026-04-07 09:50", "李静"),
            ],
        ),
    ]
    return cat


def build_store(tmp_root: Path):
    """往临时状态库里写几条处在不同阶段的任务，让队列看起来是「真在跑」。"""
    import time

    from ecnu_transcribe.store import Stage, StateStore, TaskRecord

    store = StateStore(tmp_root / "state.db")
    resources = {r.title: r for r in build_catalog().resources}
    now = time.time()
    rows = [
        # (课程, 标题, 阶段, 进度, 重试, 已跑秒数, 错误)
        ("高等数学（二）", "第1讲 定积分的概念与性质", Stage.DONE, 100.0, 0, 412.0, ""),
        ("高等数学（二）", "第2讲 微积分基本定理", Stage.DONE, 100.0, 0, 388.0, ""),
        ("世界政治经济地理", "第1讲 导论：地理与政治经济格局", Stage.TRANSCRIBING, 68.0, 0, 655.0, ""),
        ("世界政治经济地理", "第2讲 全球资源分布与地缘冲突", Stage.DOWNLOADING, 31.0, 1, 522.0, ""),
        ("数据结构与算法", "第1讲 绪论与算法复杂度", Stage.POST_PROCESSING, 84.0, 0, 96.0, ""),
        ("数据结构与算法", "第2讲 线性表：顺序存储与链式存储", Stage.PENDING, 0.0, 0, 0.0, ""),
        ("数据结构与算法", "第3讲 栈与队列", Stage.PENDING, 0.0, 0, 0.0, ""),
        ("软件工程数学", "第1讲 命题逻辑与推理", Stage.FAILED, 42.0, 3,
         240.0, "ASR 端点暂时不可用（HTTP 429）：已指数退避重试 3 次"),
    ]
    for course, title, stage, prog, retry, elapsed, err in rows:
        started = now - elapsed if elapsed else 0.0
        resource = resources.get(title)
        store.upsert_task(TaskRecord(
            course=course, course_id=resource.course_id if resource else "demo",
            resource_id=resource.resource_id if resource else title, title=title,
            stage=stage, progress=prog, retry=retry, error=err,
            output_dir=str(tmp_root / "output"),
            duration_sec=3000.0,
            updated_at=now - 3,
            created_at=now - elapsed - 30,
            started_at=started,
            finished_at=(now if stage in (Stage.DONE, Stage.FAILED) else 0.0),
        ))
    return store


#: 演示日志：格式与真实运行时**完全一致**（时间戳 + 级别 + logger 名 + 消息），
#: 因为这些字符串是通过项目自己的 LogBus 走 logging 发出来的，不是手写的。
DEMO_LOGS = [
    ("ecnu_transcribe.downloader", "开始拉流：《第1讲 定积分的概念与性质》 3301s，输出 cache/media/第1讲 定积分的概念与性质__381401.mp3"),
    ("ecnu_transcribe.media", "ffmpeg 就绪：<应用目录>\\_internal\\ffmpeg.exe（版本 7.1）"),
    ("ecnu_transcribe.downloader", "拉流完成：3301.2s / 清单 3301.0s（偏差 0.01%），sha256 已记录"),
    ("ecnu_transcribe.transcriber", "命中转写缓存（音频 sha256 一致），跳过 ASR —— 本次不产生费用"),
    ("ecnu_transcribe.exporter", "写出产物：《第1讲 定积分的概念与性质.txt》/ .srt / .md（UTF-8 带 BOM）"),
    ("ecnu_transcribe.pipeline", "任务完成：《第1讲 定积分的概念与性质》→ output/高等数学（二）/"),
    ("ecnu_transcribe.client", "清单已刷新：4 门课程 / 14 条录播，时长合计 18:06:14"),
    ("ecnu_transcribe.downloader", "断点续传：cache/media/….partial 已有 1420.5s，仅补抓剩余 3809.5s"),
    ("ecnu_transcribe.transcriber", "静音切分：本段 5400s → 10 个分段（单段 ≤ 600s，重叠 1.5s）"),
    ("ecnu_transcribe.transcriber", "分段 7/10 完成，时间轴平移 +3600.0s 后合并"),
    ("ecnu_transcribe.llm", "DeepSeek 文本加工：修复 42 处同音字，按语义重新分段为 18 段（时间轴未改动）"),
    ("ecnu_transcribe.pipeline", "ASR 端点暂时不可用（HTTP 429），指数退避后重试（第 3/4 次）"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="生成展示用界面截图")
    ap.add_argument("--out", default=str(ROOT / "assets" / "shots"), help="截图输出目录")
    ap.add_argument("--width", type=int, default=1240)
    ap.add_argument("--height", type=int, default=820)
    args = ap.parse_args()

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QSplitter

    app = QApplication.instance() or QApplication([])
    font_name = pick_cjk_font()
    if font_name is None:
        print("⛔ 找不到含中文字形的字体，中文会渲染成方块；请设置 QT_QPA_FONTDIR 指向系统字体目录")
        return 2
    print(f"· 截图字体：{font_name}")

    from app.ui import theme
    from app.ui.main_window import MainWindow, STAGE_LABEL  # noqa: F401
    from app.ui.settings_dialog import SettingsDialog
    from ecnu_transcribe.config import ConfigManager
    from ecnu_transcribe.logbus import LogBus, get_logger

    # 让主题的字体探测选中一个**确实含 CJK** 的字体（真实桌面上首选微软雅黑，行为不变）
    theme.FONT_FAMILIES = (font_name, *theme.FONT_FAMILIES)
    theme.apply_theme(app)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(tempfile.mkdtemp(prefix="ecnu-shots-"))
    os.environ["ECNU_TRANSCRIBE_HOME"] = str(tmp_root)
    os.environ["LOCALAPPDATA"] = str(tmp_root / "user")

    # 演示日志走真实 LogBus，于是界面渲染出来的就是真实格式（时间戳/级别/logger 名）
    LogBus.instance().clear()  # 从干净状态开始，避免重复
    for name, msg in DEMO_LOGS:
        get_logger(name).info(msg)

    cm = ConfigManager()
    cm.load()
    cm.set_secret("asr_api_key", "demo-not-a-real-key")
    store = build_store(tmp_root)

    win = MainWindow(cm, store)
    win._startup_hints = lambda: None  # 展示数据不触发真实网络诊断。
    try:
        win.catalog = build_catalog()
        win._render_tree()  # noqa: SLF001
        # 日志区内容来自 LogBus 历史，格式与真实运行一致
        win.log_view.clear()
        for _level, msg in LogBus.instance().history():
            win.log_view.appendPlainText(msg)
        win._reload_tasks()  # noqa: SLF001
        win._login_verified = True
        win._update_setup()
        win.lbl_toolbar_status.setText("界面演示 · 模拟数据")
        win.lbl_login.setText("演示账号 · 模拟数据")
        # 底部的清单缓存路径会带上截图机器的绝对路径，展示用改成相对写法
        win.lbl_catalog_stats.setText(win.lbl_catalog_stats.text().replace(str(ROOT), "<仓库根目录>"))
        win.resize(args.width, args.height)
        win.show()
        for _ in range(12):
            app.processEvents()

        main_png = out / "main_window.png"
        win.grab().save(str(main_png))
        print(f"✅ {main_png}  ({main_png.stat().st_size // 1024} KB)")
        win.pages.setCurrentIndex(1)
        app.processEvents()
        win.grab().save(str(out / "tasks.png"))
        win.btn_logs.setChecked(True)
        app.processEvents()
        win.grab().save(str(out / "tasks_logs.png"))

        dlg = SettingsDialog(cm.load(), cm)
        dlg.resize(1040, 780)
        dlg.show()
        for _ in range(12):
            app.processEvents()
        dlg_png = out / "settings_dialog.png"
        dlg.grab().save(str(dlg_png))
        print(f"✅ {dlg_png}  ({dlg_png.stat().st_size // 1024} KB)")
        dlg.close()
    finally:
        win.close()
        store.close()
        QTimer.singleShot(0, app.quit)

    print(f"\n截图目录：{out}")
    print("（演示数据只写进临时目录，未触碰真实配置与任务库）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
