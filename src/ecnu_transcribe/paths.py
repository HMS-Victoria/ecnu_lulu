"""运行时路径解析：区分「开发态」与「PyInstaller 冻结态」。

目录约定（与 README.md / PROGRESS.md 保持一致）::

    <APP_ROOT>/                    开发态 = 仓库根；冻结态 = exe 所在目录
        output/<课程名>/<标题>.{txt,srt,md}
        cache/media/               音频缓存（命中即跳过下载）
        data/catalog.json          清单缓存
        logs/                      滚动日志
    %LOCALAPPDATA%/ecnu-transcribe/
        browser/                   Playwright 持久化用户目录（含登录态，敏感）
        storage_state.json        仅 cookie/localStorage，敏感
        secrets.json              DPAPI 加密后的凭据

敏感文件一律放在 %LOCALAPPDATA%，**不落在工作区**，避免误提交。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "ecnu-transcribe"
APP_NAME_CN = "大夏学堂转写助手"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出的 exe 中。"""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """只读资源根目录（PyInstaller 解包目录 ``_MEIPASS``，否则为仓库根）。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
    return Path(__file__).resolve().parents[2]


def app_root() -> Path:
    """可写的应用根目录（存放 output / cache / data / logs）。"""
    env = os.environ.get("ECNU_TRANSCRIBE_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def user_data_dir() -> Path:
    """用户级数据目录（敏感文件专用）。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        d = Path(base) / APP_NAME
    else:
        d = Path.home() / f".{APP_NAME}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def browser_profile_dir() -> Path:
    """Playwright 持久化浏览器用户目录（含真实登录态，敏感）。"""
    d = user_data_dir() / "browser"
    d.mkdir(parents=True, exist_ok=True)
    return d


def storage_state_path() -> Path:
    """storage_state.json —— 由 Playwright 导出，供 httpx 复用登录态。"""
    return user_data_dir() / "storage_state.json"


def secrets_path() -> Path:
    """DPAPI 加密的凭据文件。"""
    return user_data_dir() / "secrets.json"


def recon_dir() -> Path:
    """抓包产物目录（必须脱敏）。工作区下的 recon/。"""
    d = app_root() / "recon"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ensure(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def output_dir() -> Path:
    return _ensure(app_root() / "output")


def cache_dir() -> Path:
    return _ensure(app_root() / "cache")


def media_cache_dir() -> Path:
    return _ensure(cache_dir() / "media")


def data_dir() -> Path:
    return _ensure(app_root() / "data")


def log_dir() -> Path:
    return _ensure(app_root() / "logs")


def catalog_path() -> Path:
    return data_dir() / "catalog.json"


def state_db_path() -> Path:
    return data_dir() / "state.db"


def config_path() -> Path:
    """用户配置：放用户目录，升级 exe 不丢配置。"""
    return user_data_dir() / "config.json"


def assets_dir() -> Path:
    return resource_root() / "assets"


def bundled_ffmpeg() -> Path | None:
    """随包携带的 ffmpeg.exe（packaging/ffmpeg.exe 或 _MEIPASS/ffmpeg.exe）。"""
    for cand in (
        resource_root() / "ffmpeg.exe",
        resource_root() / "assets" / "ffmpeg.exe",
        app_root() / "ffmpeg.exe",
        Path(sys.executable).resolve().parent / "ffmpeg.exe" if is_frozen() else None,
    ):
        if cand and cand.is_file():
            return cand
    return None


def describe() -> dict[str, str]:
    """给「关于」对话框和日志用的一览。"""
    return {
        "frozen": str(is_frozen()),
        "resource_root": str(resource_root()),
        "app_root": str(app_root()),
        "user_data_dir": str(user_data_dir()),
        "output_dir": str(output_dir()),
        "media_cache_dir": str(media_cache_dir()),
        "state_db": str(state_db_path()),
        "config": str(config_path()),
        "storage_state": str(storage_state_path()),
    }
