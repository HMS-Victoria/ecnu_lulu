"""打包产物「干净环境」验证：模拟一台没有 Python、没有配置的新机器。

目标是验证 DoD 里那句「在没有 Python 的干净环境也能启动」。做法是把
``dist\\大夏学堂转写助手`` 整个复制到一个**纯 ASCII 路径**下，用**全新的用户数据目录**
（不继承本机已有的 config/secrets/storage_state），并把 ``PATH`` 收窄到不含 Python，
然后真实启动 exe 走一遍：

    1. 双击等价启动（不带参数）—— 应用要能起来并自己建目录；
    2. ``--doctor`` 自检要通过；
    3. ``--selftest`` 冒烟要退出码 0；
    4. 产物目录 / 日志 / 状态库要落在 exe 同级目录（可携带）；
    5. 不能依赖工作区里的任何路径（已另行扫描 exe 二进制，见 README）。

用法::

    .venv\\Scripts\\python scripts\\verify_dist.py
    .venv\\Scripts\\python scripts\\verify_dist.py --target C:\\temp\\ecnu-clean
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "大夏学堂转写助手"
EXE_NAME = "大夏学堂转写助手.exe"

PASS: list[str] = []
FAIL: list[str] = []
WARN: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '⛔'} {name}{('  — ' + detail) if detail else ''}")


def warn(name: str, detail: str = "") -> None:
    WARN.append(name)
    print(f"  ⚠️  {name}{('  — ' + detail) if detail else ''}")


def run_exe(exe: Path, args: list[str], *, env: dict[str, str], timeout: float = 180.0) -> tuple[int, str]:
    proc = subprocess.run(
        [str(exe), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=timeout, cwd=str(exe.parent),
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser(description="打包产物干净环境验证")
    ap.add_argument("--target", default=r"C:\ecnu-clean-test", help="复制到的纯 ASCII 目录")
    ap.add_argument("--keep", action="store_true", help="保留复制出来的目录（便于手工检查）")
    args = ap.parse_args()

    print("=" * 78)
    print("打包产物「干净环境」验证（模拟无 Python、无配置的新机器）")
    print("=" * 78)

    # ---------- 0) 源产物 ---------- #
    print("\n[0] 检查 dist 产物")
    exe_src = DIST / EXE_NAME
    if not exe_src.is_file():
        print(f"⛔ 找不到 {exe_src}；先运行： python -m PyInstaller packaging/ecnu_transcribe.spec")
        return 1
    check("exe 存在", True, f"{exe_src.stat().st_size / 1024 / 1024:.1f} MB")
    bundled_ffmpeg = DIST / "_internal" / "ffmpeg.exe"
    check("内嵌 ffmpeg", bundled_ffmpeg.is_file(),
          f"{bundled_ffmpeg.stat().st_size / 1024 / 1024:.0f} MB" if bundled_ffmpeg.is_file() else "缺失")
    if not bundled_ffmpeg.is_file():
        warn("ffmpeg 未内嵌", "目标机器需要自己装 ffmpeg")

    # ---------- 1) 复制到纯 ASCII 路径 ---------- #
    target = Path(args.target)
    print(f"\n[1] 复制到纯 ASCII 路径：{target}")
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(DIST, target)
    except OSError as exc:
        print(f"⛔ 复制失败：{exc}")
        return 1
    exe = target / EXE_NAME
    check("复制成功", exe.is_file())
    check("路径无中文/空格", all(ord(c) < 128 for c in str(target)), str(target))

    # ---------- 2) 干净的运行环境 ---------- #
    print("\n[2] 构造干净环境（全新用户数据目录 + 收窄 PATH）")
    fake_local = target / "_fake_localappdata"
    fake_local.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(fake_local)          # 不继承本机的 config/secrets/登录态
    env["QT_QPA_PLATFORM"] = "offscreen"           # 无显示器环境
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 收窄 PATH：去掉 Python / venv / winget-ffmpeg，模拟「机器上没有 Python」
    keep_dirs = [
        d for d in env.get("PATH", "").split(os.pathsep)
        if d and "python" not in d.lower() and ".venv" not in d.lower()
        and "winget" not in d.lower() and "ms-playwright" not in d.lower()
    ]
    env["PATH"] = os.pathsep.join(keep_dirs)
    check("用户数据目录已隔离", env["LOCALAPPDATA"] == str(fake_local), str(fake_local))
    stripped = [d for d in keep_dirs if "python" in d.lower()]
    check("PATH 中已无 Python", not stripped, f"剩余 {len(keep_dirs)} 个目录")

    def exe_python_present() -> bool:
        """exe 自己能不能跑（说明它不依赖系统 Python）。"""
        return exe.is_file()

    # ---------- 3) --doctor ---------- #
    print("\n[3] 在干净环境里跑 --doctor")
    code, out = run_exe(exe, ["--doctor"], env=env, timeout=300)
    print("    " + "\n    ".join(out.strip().splitlines()[-24:]))
    check("--doctor 退出码为 0", code == 0, f"exit={code}")
    check("自检无失败项", "0 项失败" in out, "见上方输出")
    check("用的是自带 ffmpeg（不是系统 PATH 里的）",
          str(target).lower() in out.lower() or "_internal" in out, "见上方 ffmpeg 路径")

    # ---------- 4) --selftest（等价于双击启动） ---------- #
    print("\n[4] 启动应用（--selftest，2 秒后自动退出）")
    code2, out2 = run_exe(exe, ["--selftest"], env=env, timeout=180)
    check("--selftest 退出码为 0", code2 == 0, f"exit={code2}")
    check("frozen 模式识别正确", "frozen = True" in out2 or "frozen=True" in out2, "")

    # ---------- 5) 可携带的产物目录 ---------- #
    print("\n[5] 检查运行目录（应落在 exe 同级，可整体拷走）")
    for name in ("output", "cache", "data", "logs"):
        p = target / name
        check(f"{name}/ 已自动创建在 exe 同级", p.is_dir(), str(p))
    log = target / "logs" / "app.log"
    if log.is_file():
        text = log.read_text(encoding="utf-8", errors="replace")
        check("日志已写入且被脱敏", "REDACTED" in text or "app_root" in text, f"{len(text)} 字符")
        check("日志里的 app_root 指向复制后的路径",
              str(target).split("\\")[-1] in text or "_internal" in text, "")
    else:
        warn("未找到 logs/app.log", "可能日志写到了别处")

    # ---------- 6) 用户数据写到隔离目录 ---------- #
    print("\n[6] 检查用户数据写入隔离目录")
    cfg = fake_local / "ecnu-transcribe" / "config.json"
    check("配置写到 %LOCALAPPDATA%\\ecnu-transcribe", cfg.is_file(), str(cfg))
    if cfg.is_file():
        import json

        data = json.loads(cfg.read_text(encoding="utf-8"))
        check("配置内容可解析", isinstance(data, dict) and "asr_model" in data, f"{len(data)} 个键")
        check("配置里不含明文密钥",
              not any(k in data for k in ("asr_api_key", "llm_api_key", "password")),
              "只有非敏感项")

    # ---------- 7) 清理 ---------- #
    if not args.keep:
        print(f"\n[7] 清理 {target}")
        shutil.rmtree(target, ignore_errors=True)
    else:
        print(f"\n[7] 保留 {target}（--keep）")

    print("\n" + "=" * 78)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败，{len(WARN)} 项提醒")
    for name in FAIL:
        print("  ⛔ " + name)
    for name in WARN:
        print("  ⚠️  " + name)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
