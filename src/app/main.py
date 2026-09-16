"""GUI 入口：``python -m app`` / ``python main.py`` / 打包后的 ``大夏学堂转写助手.exe``。

启动流程
--------
1. 建立 ``QApplication``，设置中文字体与高 DPI；
2. 初始化日志（文件 + GUI 总线）与配置（含 DPAPI 凭据）；
3. 打开状态库、恢复上次未完成的任务（断点续跑）；
4. 显示三栏主窗口。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

# 允许 `python main.py` 直接运行（把 src 加进 sys.path）
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _configure_playwright_browsers_path() -> None:
    """打包后必须显式指定 Playwright 的浏览器目录。

    Playwright 默认按**驱动所在目录**找浏览器，冻结态下会算到
    ``_internal/playwright/driver/package/.local-browsers``（不存在）。
    这里改指向标准的用户级缓存目录，并用 ``chromium-*`` 实际存在与否来确认。
    """
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return

    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "ms-playwright")
    home = Path.home()
    candidates.append(home / "AppData" / "Local" / "ms-playwright")
    candidates.append(home / ".cache" / "ms-playwright")

    for cand in candidates:
        try:
            if cand.is_dir() and any(p.name.startswith("chromium") for p in cand.iterdir()):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cand)
                return
        except OSError:
            continue


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    """未捕获异常：写日志 + 弹窗，避免 GUI 静默崩溃。"""
    from ecnu_transcribe.logbus import get_logger

    log = get_logger("app")
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    log.error("未捕获异常：\n%s", text)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is not None:
            QMessageBox.critical(
                None, "程序内部错误",
                f"发生了未预期的错误，已写入日志：\n\n{exc_value}\n\n"
                f"日志位置：{log_path_hint()}",
            )
    except Exception:
        pass


def log_path_hint() -> str:
    from ecnu_transcribe import paths

    return str(paths.log_dir() / "app.log")


def _run_doctor() -> int:
    """``--doctor``：在打包产物上跑一次自检（内嵌 ffmpeg / 路径 / 凭据 / 状态库 / Chromium）。"""
    import runpy
    from pathlib import Path

    from ecnu_transcribe import paths

    for base in (paths.app_root(), paths.resource_root(), Path(__file__).resolve().parents[2]):
        cand = base / "scripts" / "doctor.py"
        if cand.is_file():
            print(f"[doctor] 使用 {cand}")
            runpy.run_path(str(cand), run_name="__main__")
            return 0
    print("⛔ 找不到 scripts/doctor.py（打包产物里应位于 exe 同级或 _internal 下）")
    return 1


def _run_console_login() -> int:
    """``--login``：命令行方式完成一次人工登录（打包产物也能用）。"""
    import runpy
    from pathlib import Path as _Path

    from ecnu_transcribe import paths

    for base in (_Path(__file__).resolve().parents[2], paths.resource_root(), paths.app_root()):
        cand = base / "scripts" / "login.py"
        if cand.is_file():
            print(f"[login] 使用 {cand}")
            sys.argv = [str(cand), *[a for a in sys.argv[1:] if a != "--login"]]
            runpy.run_path(str(cand), run_name="__main__")
            return 0
    print("⛔ 找不到 scripts/login.py")
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    _configure_playwright_browsers_path()

    if "--doctor" in argv:
        return _run_doctor()
    if "--login" in argv:
        return _run_console_login()

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from ecnu_transcribe import __version__, paths
    from ecnu_transcribe.config import ConfigManager
    from ecnu_transcribe.logbus import LogBus, get_logger, setup_logging
    from ecnu_transcribe.store import StateStore

    # 高 DPI（Qt6 默认开启缩放，这里只保证取整策略稳定）
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(argv)
    app.setApplicationName("大夏学堂转写助手")
    app.setApplicationDisplayName("大夏学堂转写助手")
    app.setOrganizationName("ecnu-transcribe")
    app.setApplicationVersion(__version__)

    # 中文字体：优先微软雅黑，避免默认字体缺字
    for family in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"):
        f = QFont(family, 9)
        if f.exactMatch() or family.startswith("Microsoft YaHei"):
            app.setFont(f)
            break

    # 自带浅色高对比主题：**不跟随系统深色模式**
    # （本机 Windows 是深色模式，而界面里有一批硬编码的深灰文字 ⇒ 深灰压深底、
    #   对比度极低，用户实测反馈"灰灰的看不清"）
    from .ui.theme import apply_theme

    apply_theme(app)

    setup_logging()
    log = get_logger("app")
    log.info("=" * 70)
    log.info("大夏学堂录播转写助手 v%s 启动", __version__)
    for key, value in paths.describe().items():
        log.info("  %s = %s", key, value)

    sys.excepthook = _excepthook

    cm = ConfigManager()
    cm.load()
    store = StateStore()
    store.recover_orphans()

    from .ui.main_window import MainWindow

    window = MainWindow(cm, store)
    window.show()

    # 首次运行：引导用户先登录
    if not paths.storage_state_path().is_file():
        log.warning("尚未登录：请点顶部「登录」完成一次统一身份认证")

    if "--selftest" in argv:
        # 供打包后冒烟测试：启动 2 秒后自动退出
        from PySide6.QtCore import QTimer

        QTimer.singleShot(2000, app.quit)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
