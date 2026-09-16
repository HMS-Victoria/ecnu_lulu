"""用**普通 Chrome 窗口**登录，再通过 CDP 把会话读回来（缺陷 48 的替代路径）。

为什么不用 Playwright 直接启动浏览器：这台机器上 Playwright 启动的 Chromium 窗口会被
窗口管理器最小化/摆到屏幕外（`(-25600,-25600)`、宽度只剩 159px），用户根本看不到；
而强行置顶又挡住了用户看别的东西。两边都不讨好。

这个方案把「浏览器」和「自动化」解耦：

    1. 用**系统自带的 Chrome** 启动一个**普通窗口**（独立 profile + 调试端口）——
       它就是您平时用的那种窗口：可以拖、可以最小化、不会置顶；
    2. 您在窗口里完成统一身份认证（本工具不碰密码、不做验证码识别）；
    3. 脚本通过 CDP 连上去，检测到登录成功就把 Cookie / localStorage 导出成
       `storage_state.json`，供后续 httpx 请求复用；
    4. 之后浏览器可以随便关掉，不影响已保存的登录态。

用法::

    .venv\\Scripts\\python scripts\\login_via_cdp.py            # 打开窗口并等待登录
    .venv\\Scripts\\python scripts\\login_via_cdp.py --timeout 3600
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PORT = 9222
PROFILE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ecnu-transcribe" / "chrome-profile"


def find_chrome() -> Path | None:
    cands = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for c in cands:
        if c.is_file():
            return c
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="用普通 Chrome 窗口登录并通过 CDP 取回会话")
    ap.add_argument("--timeout", type=float, default=1800.0, help="等待登录的秒数")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    from ecnu_transcribe import paths
    from ecnu_transcribe.client import load_session_state
    from ecnu_transcribe.config import ConfigManager
    from ecnu_transcribe.logbus import setup_logging

    setup_logging()
    cfg = ConfigManager().load()
    chrome = find_chrome()
    if chrome is None:
        print("⛔ 找不到 Chrome / Edge 可执行文件")
        return 2

    PROFILE.mkdir(parents=True, exist_ok=True)
    print("=" * 84)
    print("用普通浏览器窗口登录（不会置顶、可随意拖动/最小化）")
    print(f"  浏览器   : {chrome}")
    print(f"  独立配置 : {PROFILE}")
    print(f"  调试端口 : 127.0.0.1:{args.port}")
    print(f"  入口     : {cfg.portal_url}")
    print("=" * 84, flush=True)

    proc = subprocess.Popen(
        [
            str(chrome),
            f"--user-data-dir={PROFILE}",
            f"--remote-debugging-port={args.port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            cfg.portal_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"已启动浏览器（PID {proc.pid}）—— 请在弹出的窗口里完成统一身份认证：", flush=True)
    print("  · 你的学号 + 密码（验证码请手动过）", flush=True)
    print("  · 登录成功后本脚本会自动保存登录态并退出，浏览器可以留着也可以关掉", flush=True)

    from playwright.sync_api import sync_playwright

    deadline = time.time() + args.timeout
    saved = False
    with sync_playwright() as pw:
        browser = None
        for attempt in range(30):
            try:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 0:
                    print(f"  等待调试端口…（{type(exc).__name__}）", flush=True)
                time.sleep(1.0)
        if browser is None:
            print("⛔ 连不上调试端口，浏览器可能没起来")
            return 1

        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        while time.time() < deadline:
            page = None
            for p in ctx.pages:
                if "ecnu.edu.cn" in (p.url or ""):
                    page = p
                    break
            if page is None and ctx.pages:
                page = ctx.pages[0]
            if page is not None:
                try:
                    url = page.url or ""
                    if "ecnu.edu.cn" in url and "/login" not in url and "oauth" not in url:
                        body = ""
                        try:
                            body = page.inner_text("body", timeout=3000)[:4000]
                        except Exception:  # noqa: BLE001
                            pass
                        markers = ("录播", "课程", "资源管理", "我的课程", "回放", "章节", "资源列表")
                        ls = 0
                        try:
                            ls = page.evaluate(
                                "() => Object.keys(window.localStorage||{}).filter("
                                "k => /token|jwt|ticket|auth/i.test(k)).length"
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        if url != getattr(main, "_last_url", ""):
                            main._last_url = url  # type: ignore[attr-defined]
                            print(f"  当前页面：{url[:120]}", flush=True)
                        if sum(1 for m in markers if m in body) >= 1 or ls > 0:
                            target = paths.storage_state_path()
                            ctx.storage_state(path=str(target))
                            print(f"✅ 已检测到登录态并保存：{target}", flush=True)
                            print(f"   Cookie {len((load_session_state() or {}).cookies if False else (ctx.cookies() or []))} 个", flush=True)
                            saved = True
                            break
                except Exception as exc:  # noqa: BLE001
                    print(f"  检测异常（继续等）：{type(exc).__name__}: {str(exc)[:80]}", flush=True)
            time.sleep(2.0)

    if not saved:
        print(f"⛔ 等待登录超时（{args.timeout:.0f}s）。浏览器还开着的话，登录后重跑本脚本即可。")
        return 1
    print("登录态已保存，可以继续跑验收了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
