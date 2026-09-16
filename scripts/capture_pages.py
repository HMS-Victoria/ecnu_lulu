"""复用已登录的浏览器会话重走页面，抓取真实接口（**不需要再输密码**）。

用法::

    .venv\\Scripts\\python scripts\\capture_pages.py --routes /home,/list-video --wait 15

为什么需要它：`scripts/login.py --capture` 只在「人工登录那一次」抓包，而平台是
hash 路由的 SPA —— 首页加载完只发出菜单/权限类请求，真正的「课程点播列表」
要等前端路由跳到对应页面才会发。本脚本用已保存的登录态（持久化浏览器目录）
直接访问这些路由，把真实接口与响应体抓下来，用于：

* 校正 `EcnuClient` 的接口候选与解析（M1 验收）；
* 填 `docs/API.md` 里标着「⬜ 待抓包」的部分。

抓包文件默认写工作区 ``recon/network.jsonl``（真实流量，符合该文件的用途），
每条都已脱敏（Cookie / token / 学号 / 手机号 / 身份证 / jwt-token 值）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="用已登录会话抓取平台真实接口")
    ap.add_argument("--routes", default="/home,/list-video",
                    help="要访问的 hash 路由，逗号分隔（如 /home,/list-video）")
    ap.add_argument("--wait", type=float, default=15.0, help="每个路由停留秒数")
    ap.add_argument("--out", default="", help="抓包输出路径（默认工作区 recon/network.jsonl）")
    ap.add_argument("--scroll", action="store_true", help="每个路由结束后滚动到底（触发懒加载）")
    ap.add_argument("--hash-nav", action="store_true",
                    help="用 location.hash 切换路由（SPA 内部跳转，不整页重载）——"
                         "整页重载会让平台重新走一次认证，反而抓不到列表接口")
    ap.add_argument("--print-endpoints", action="store_true", help="结束后打印本文件里的接口清单")
    args = ap.parse_args()

    from ecnu_transcribe import paths
    from ecnu_transcribe.config import ConfigManager
    from ecnu_transcribe.login import LoginSession

    cm = ConfigManager()
    cfg = cm.load()

    out_path = Path(args.out) if args.out else (paths.recon_dir() / "network.jsonl")
    if not paths.storage_state_path().is_file():
        print("⛔ 还没有登录态（storage_state.json 不存在）。请先运行 scripts/login.py 完成一次登录。")
        return 2

    print("=" * 78)
    print("用已登录会话抓取平台接口")
    print(f"  入口    : {cfg.portal_url}")
    print(f"  登录态  : {paths.storage_state_path()}")
    print(f"  抓包写到: {out_path}")
    print(f"  路由    : {args.routes}    每个停留 {args.wait:.0f}s")
    print("=" * 78)

    session = LoginSession(cfg, on_status=lambda m: print("  " + m), headless=False,
                           record_network=True)
    assert session.recorder is not None
    session.recorder.path = out_path          # 覆盖默认路径，明确写到哪里
    routes = [r.strip() for r in args.routes.split(",") if r.strip()]
    base = cfg.portal_url.split("#")[0]

    started = len(session.recorder.calls)
    try:
        session.open()
        page = session._page  # noqa: SLF001 — 本脚本是运维工具，直接用页面对象
        for route in routes:
            target = f"{base}#{route}"
            print(f"\n→ 访问 {target}{'（hash 内部跳转）' if args.hash_nav else ''}")
            try:
                if args.hash_nav:
                    # SPA 内部路由：只改 hash，不触发整页重载（重载会让平台重新认证）
                    page.evaluate("(h) => { window.location.hash = h; }", route)
                else:
                    page.goto(target, wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:  # noqa: BLE001
                print(f"  导航告警（继续）：{exc}")
            time.sleep(max(1.0, args.wait))
            if args.scroll:
                try:
                    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(2.0)
                except Exception:  # noqa: BLE001
                    pass
            print(f"  当前页面：{page.url}")
            try:
                title = page.title()
                body_len = len(page.inner_text("body", timeout=5000) or "")
                print(f"  标题：{title!r}，正文 {body_len} 字符")
            except Exception:  # noqa: BLE001
                pass
        print(f"\n✅ 登录态仍有效：{session._detect_logged_in()}")  # noqa: SLF001
    finally:
        new_calls = len(session.recorder.calls) - started
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
        print(f"\n本次新增抓包 {new_calls} 条 → {out_path}")

    if args.print_endpoints and out_path.is_file():
        seen: list[str] = []
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            u = urlsplit(str(d.get("url", "")))
            key = f"{d.get('method')} {d.get('status')} {u.netloc}{u.path}"
            if key not in seen:
                seen.append(key)
        print("\n抓包文件里的接口清单（去重）：")
        for line in seen:
            print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
