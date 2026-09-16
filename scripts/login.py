"""在应用内打开一个可见的浏览器完成统一身份认证（M0 侦察的登录部分）。

用法::

    .venv\\Scripts\\python scripts\\login.py                 # 交互式登录，保存 storage_state.json
    .venv\\Scripts\\python scripts\\login.py --check         # 只检查已有登录态是否可用
    .venv\\Scripts\\python scripts\\login.py --capture       # 登录 + 抓包（写 recon/network.jsonl）
    .venv\\Scripts\\python scripts\\login.py --timeout 1200  # 自定义等待时间（秒）

安全说明
--------
* 密码只在你面前的浏览器窗口里输入，本脚本**不接收**、不保存、不打印密码。
* 抓包产物写入 ``recon/network.jsonl`` 前会脱敏（Cookie / Authorization / token /
  手机号 / 身份证 / 学号 全部替换为占位符）。
* ``storage_state.json`` 只落在 ``%LOCALAPPDATA%\\ecnu-transcribe\\``，且已被 .gitignore 覆盖。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecnu_transcribe import paths  # noqa: E402
from ecnu_transcribe.config import ConfigManager  # noqa: E402
from ecnu_transcribe.logbus import get_logger, setup_logging  # noqa: E402
from ecnu_transcribe.login import LoginSession, check_session_alive  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="大夏学堂统一身份认证（人工登录一次）")
    ap.add_argument("--check", action="store_true", help="只检查已有登录态是否可用")
    ap.add_argument("--capture", action="store_true", help="同时抓包到 recon/network.jsonl")
    ap.add_argument("--timeout", type=float, default=900.0, help="等待登录完成的秒数")
    ap.add_argument("--headless", action="store_true", help="无头模式（不推荐：无法手动过验证码）")
    args = ap.parse_args()

    setup_logging()
    log = get_logger("scripts.login")

    cm = ConfigManager()
    cfg = cm.load()

    if args.check:
        ok, msg = check_session_alive(cfg)
        print(("✅ " if ok else "⛔ ") + msg)
        print("登录态文件：" + str(paths.storage_state_path()))
        return 0 if ok else 1

    print("=" * 70)
    print("即将打开一个可见的浏览器窗口（Chromium）。")
    print("请在窗口里用你的学号 + 密码登录华东师范大学统一身份认证；")
    print("如果出现图形验证码 / 短信二次验证，请手动完成。")
    print()
    print(f"浏览器用户目录：{paths.browser_profile_dir()}")
    print(f"登录态将保存到：{paths.storage_state_path()}")
    if args.capture:
        print(f"抓包将写入：    {paths.recon_dir() / 'network.jsonl'}（已脱敏）")
    print()
    print("提示：如果页面显示「需要校园网 / VPN」，请先连学校 SSL-VPN")
    print("      （https://vpn.ecnu.edu.cn/portal/）或接入校园网，再重新运行本脚本。")
    print("=" * 70)

    sess = LoginSession(cfg, on_status=lambda m: print(f"  {m}"), record_network=args.capture)
    try:
        sess.open()
        ok = sess.wait_for_login(timeout=args.timeout)
    except KeyboardInterrupt:
        print("\n已被用户中断。")
        ok = False
    finally:
        if args.capture and sess.recorder is not None:
            print(f"抓包记录：{len(sess.recorder.calls)} 条 → {sess.recorder.path}")
            print(f"接口候选汇总：{sess.recorder.dump_summary()}")
        sess.close()

    if ok:
        print("\n✅ 登录态已保存。接着跑：")
        print("   .venv\\Scripts\\python scripts\\fetch_catalog.py")
        return 0
    print("\n⛔ 登录未完成。可重新运行本脚本重试。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
