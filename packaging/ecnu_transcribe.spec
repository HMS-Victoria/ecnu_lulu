# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：大夏学堂录播转写助手（--onedir，稳定优先）。

用法::

    .venv\\Scripts\\python -m PyInstaller packaging\\ecnu_transcribe.spec --noconfirm

产物::

    dist\\大夏学堂转写助手\\大夏学堂转写助手.exe

要点
----
* ``--onedir``（不是 onefile）：启动快、杀软误报少、便于携带 ffmpeg。
* 内嵌 ffmpeg：优先复制仓库 ``assets/ffmpeg.exe``；没有就复制
  ``imageio-ffmpeg`` 自带的那份（保证「装完依赖就能打包出可用 exe」）。
* 排除无用的大块依赖（matplotlib/numpy/tk 等），控制体积。
* ``console=False``：GUI 应用不弹黑窗。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent
SRC = ROOT / "src"
ASSETS = ROOT / "assets"
APP_NAME = "大夏学堂转写助手"
ENTRY = ROOT / "main.py"

# --------------------------------------------------------------------------- #
# ffmpeg 内嵌
# --------------------------------------------------------------------------- #
def locate_ffmpeg() -> Path | None:
    """按优先级找一个可随包携带的 ffmpeg.exe。"""
    candidates: list[Path] = []
    env = __import__("os").environ.get("ECNU_FFMPEG")
    if env:
        candidates.append(Path(env))
    candidates.append(ASSETS / "ffmpeg.exe")
    exe = shutil.which("ffmpeg")
    if exe:
        candidates.append(Path(exe))
    try:
        import imageio_ffmpeg  # type: ignore

        candidates.append(Path(imageio_ffmpeg.get_ffmpeg_exe()))
    except Exception:
        pass
    for cand in candidates:
        try:
            if cand and cand.is_file():
                return cand
        except OSError:
            continue
    return None


ffmpeg_src = locate_ffmpeg()
binaries: list[tuple[str, str]] = []
if ffmpeg_src is not None:
    binaries.append((str(ffmpeg_src), "."))
    print(f"[spec] 将内嵌 ffmpeg: {ffmpeg_src}")
else:
    print("[spec] ⚠ 未找到 ffmpeg，打包产物需要用户自行安装或在设置页指定路径")

# ffprobe（可选，有就一起带上，探测更准）
ffprobe_src = None
if ffmpeg_src is not None:
    sibling = ffmpeg_src.with_name("ffprobe.exe")
    if sibling.is_file():
        ffprobe_src = sibling
        binaries.append((str(ffprobe_src), "."))
        print(f"[spec] 将内嵌 ffprobe: {ffprobe_src}")

# --------------------------------------------------------------------------- #
# 数据文件
# --------------------------------------------------------------------------- #
datas: list[tuple[str, str]] = []
for extra in (ROOT / "README.md", ROOT / "requirements.txt", ROOT / "PROGRESS.md", ROOT / "CHANGELOG.md"):
    if extra.is_file():
        datas.append((str(extra), "."))
docs_dir = ROOT / "docs"
if docs_dir.is_dir():
    datas.append((str(docs_dir), "docs"))

# 把 scripts/ 一起打包：exe 支持 `--doctor` 自检，也能在冻结态复用这些工具
scripts_dir = ROOT / "scripts"
if scripts_dir.is_dir():
    datas.append((str(scripts_dir), "scripts"))
    print(f"[spec] 将打包 scripts/: {scripts_dir}")

hiddenimports: list[str] = []
hiddenimports += collect_submodules("ecnu_transcribe")
hiddenimports += collect_submodules("app")
hiddenimports += [
    "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
    "win32crypt", "win32api", "win32con",
    "keyring.backends.Windows",
    "playwright", "playwright.sync_api",
    "sqlite3", "srt",
]

excludes = [
    "matplotlib", "numpy", "pandas", "scipy", "tkinter", "test", "unittest",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtMultimedia",
    "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtBluetooth", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtLocation", "PySide6.QtNfc", "PySide6.QtOpenGL",
    "PySide6.QtPdf", "PySide6.QtPositioning", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSpatialAudio",
    "PySide6.QtSql", "PySide6.QtStateMachine", "PySide6.QtSvg", "PySide6.QtTest",
    "PySide6.QtTextToSpeech", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtNetworkAuth", "PySide6.QtUiTools", "PySide6.QtConcurrent",
]

a = Analysis(
    [str(ENTRY)],
    pathex=[str(SRC), str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ASSETS / "app.ico") if (ASSETS / "app.ico").is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
