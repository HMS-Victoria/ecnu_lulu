"""一键验收编排：等登录 → 真实 GUI 拉清单（M4）→ 2 条真实录播端到端（M2/M3/M6）。

为什么需要它：真实验收必须由**你本人**完成一次统一身份认证（本工具不碰密码、
不做验证码识别），而登录这件事什么时候发生不由程序决定。所以把「等」也交给程序：

    1. 探测登录态（`GET /oauth2/token`）；
    2. 无效就打开可见浏览器等你登录（默认最多等 4 小时，可 `--login-wait` 调整）；
    3. 登录成功后自动跑 `verify_gui_real.py`（真实 GUI 拉真实清单）；
    4. 再跑 `accept_real.py`（真实录播 → 音频 → ASR → txt/srt/md）；
    5. 汇总每一步的结果。

用法::

    .venv\\Scripts\\python scripts\\run_acceptance.py
    .venv\\Scripts\\python scripts\\run_acceptance.py --picks 381401,395443 --login-wait 7200

输入 **Ctrl+C** 可随时中断；已下载的音频与已完成的转写都会保留（断点续跑）。
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

PY = sys.executable
BASE = "https://courses.ecnu.edu.cn"
API = "/jy-application-resourcemanage"


def session_ok() -> tuple[bool, str]:
    """用保存的 Cookie 换一次 jwt-token，判断登录态是否还有效。"""
    import httpx

    from ecnu_transcribe.client import load_session_state

    state = load_session_state()
    if state.is_empty():
        return False, "没有登录态文件（storage_state.json 不存在）"
    try:
        with httpx.Client(
            cookies=state.cookies, timeout=30.0, verify=False,
            follow_redirects=False, trust_env=False,
        ) as c:
            r = c.get(f"{BASE}{API}/oauth2/token")
    except Exception as exc:  # noqa: BLE001
        return False, f"请求失败：{type(exc).__name__}: {str(exc)[:120]}"
    if r.status_code in (301, 302, 303, 307, 308):
        return False, "被重定向到登录（登录态已过期）"
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code}"
    try:
        token = str(((r.json() or {}).get("result") or {}).get("jwt_token") or "")
    except ValueError:
        return False, "返回的不是 JSON（多半是登录态失效）"
    if not token:
        return False, "响应里没有 jwt_token"
    return True, f"jwt-token {len(token)} 字符"


def run_step(title: str, cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str]:
    """跑一步子命令，**实时**把输出透传出来（长任务要能看进度）。"""
    print("\n" + "=" * 90)
    print(f"▶ {title}")
    print("  " + " ".join(cmd))
    print("=" * 90, flush=True)
    merged = dict(os.environ)
    if env:
        merged.update(env)
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), env=merged,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        lines.append(line)
        # 过滤 ffmpeg 拉流逐秒刷屏，保留其它输出
        if "拉流中 " in line and "s" in line:
            if len(lines) % 40 == 0:
                print("   …", line.strip()[-40:], flush=True)
            continue
        print("  " + line[:200], flush=True)
    proc.wait()
    return proc.returncode, "\n".join(lines)


def summarize(text: str, patterns: tuple[str, ...] = ("结果：", "完成：", "✅", "⛔")) -> str:
    hits = [ln.strip() for ln in text.splitlines() if any(p in ln for p in patterns)]
    return " / ".join(hits[-3:])[:200] if hits else "(无摘要)"


def main() -> int:
    ap = argparse.ArgumentParser(description="一键真机验收：等登录 → GUI 拉清单 → 2 条真实录播端到端")
    ap.add_argument("--login-wait", type=int, default=14400, help="等登录的秒数（默认 4 小时）")
    ap.add_argument("--picks", default="381401,395443",
                    help="要跑的 courId（默认两条电平正常的真实录播）")
    ap.add_argument("--asr-base", default="http://127.0.0.1:8418/v1")
    ap.add_argument("--asr-model", default="faster-whisper-small")
    ap.add_argument("--max-segment", type=int, default=300, help="单段 ASR 最长秒数（传给 accept_real）")
    ap.add_argument("--skip-gui", action="store_true", help="跳过真实 GUI 验收")
    ap.add_argument("--skip-e2e", action="store_true", help="跳过 2 条真实录播端到端")
    args = ap.parse_args()

    print("=" * 90)
    print("大夏学堂录播转写助手 —— 一键真机验收")
    print(f"  登录等待上限：{args.login_wait} 秒        courId：{args.picks}")
    print(f"  ASR：{args.asr_base} / {args.asr_model}")
    print("=" * 90, flush=True)

    results: list[tuple[str, int, str]] = []

    # ---------- 1) 登录 ---------- #
    ok, why = session_ok()
    print(f"\n[1] 登录态探测：{'✅ 可用' if ok else '⛔ 不可用'} —— {why}", flush=True)
    if not ok:
        print("\n    接下来会打开一个**可见的浏览器窗口**，请在里面完成学校统一身份认证：")
        print("      · 你的学号 + 密码（如出现验证码/二次验证，请手动完成）")
        print("      · 登录成功后窗口会自动关闭；本工具不保存你的密码，也不绕过任何验证")
        print(f"      · 最多等 {args.login_wait} 秒（{args.login_wait/3600:.1f} 小时）\n", flush=True)
        rc, out = run_step(
            "打开浏览器等待你完成登录",
            [PY, str(ROOT / "scripts" / "login.py"), "--capture",
             "--timeout", str(args.login_wait)],
        )
        ok, why = session_ok()
        results.append(("登录", 0 if ok else 1, why))
        print(f"\n    登录结果：{'✅ 成功' if ok else '⛔ 仍未完成'} —— {why}", flush=True)
        if not ok:
            print("\n⛔ 没有登录态就无法继续真机验收。请重新运行本脚本，或在 GUI 里点「登录」。")
            return 1
    else:
        results.append(("登录", 0, why))

    # ---------- 2) 真实 GUI 拉清单（M4） ---------- #
    if not args.skip_gui:
        env = {"QT_QPA_PLATFORM": "offscreen"}
        rc, out = run_step("真实 GUI（离屏）拉取真实清单", [PY, str(ROOT / "scripts" / "verify_gui_real.py")], env=env)
        results.append(("M4 真实 GUI 清单", rc, summarize(out)))
    else:
        print("\n（跳过真实 GUI 验收）")

    # ---------- 3) 2 条真实录播端到端 ---------- #
    if not args.skip_e2e:
        rc, out = run_step(
            "2 条真实录播端到端（音频 → ASR → txt/srt/md）",
            [PY, str(ROOT / "scripts" / "accept_real.py"),
             "--picks", args.picks, "--max-segment", str(args.max_segment),
             "--asr-base", args.asr_base, "--asr-model", args.asr_model],
        )
        results.append(("真实录播端到端", rc, summarize(out)))
    else:
        print("\n（跳过真实录播端到端）")

    print("\n" + "=" * 90)
    print("验收汇总")
    print("=" * 90)
    for name, rc, note in results:
        print(f"  {'✅' if rc == 0 else '⛔'} {name}：{note}")
    failed = [n for n, rc, _ in results if rc != 0]
    print("=" * 90)
    if failed:
        print("未通过：" + "、".join(failed))
        return 1
    print("全部通过 🎉  产物在 output/ 下，日志见 logs/app.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
