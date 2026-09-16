"""最小 httpx 脚本：**不带浏览器**拉回真实课程/资源 JSON（M0 验收 / M1 验收）。

用法::

    .venv\\Scripts\\python scripts\\fetch_catalog.py                 # 拉全量并写 data/catalog.json
    .venv\\Scripts\\python scripts\\fetch_catalog.py --diagnose      # 只做站点可达性诊断
    .venv\\Scripts\\python scripts\\fetch_catalog.py --raw           # 打印原始响应片段（排障用）
    .venv\\Scripts\\python scripts\\fetch_catalog.py --page-size 50  # 指定分页大小
    .venv\\Scripts\\python scripts\\fetch_catalog.py --dump-recon    # 转存接口记录到 recon/

验收要点：登录态失效时必须以明确的 ``AuthExpiredError`` 失败退出（退出码 2），
**不允许**静默返回空清单。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecnu_transcribe import paths  # noqa: E402
from ecnu_transcribe.client import EcnuClient, load_session_state  # noqa: E402
from ecnu_transcribe.config import ConfigManager  # noqa: E402
from ecnu_transcribe.errors import (  # noqa: E402
    ApiChangedError,
    AuthExpiredError,
    SiteUnreachableError,
)
from ecnu_transcribe.logbus import get_logger, setup_logging  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="拉取大夏学堂录播清单（纯 httpx，无浏览器）")
    ap.add_argument("--diagnose", action="store_true", help="只诊断站点可达性")
    ap.add_argument("--raw", action="store_true", help="打印原始响应片段")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--no-save", action="store_true", help="不写 data/catalog.json")
    ap.add_argument("--dump-recon", action="store_true", help="把接口记录写入 recon/network.jsonl")
    ap.add_argument("--json", action="store_true", help="以 JSON 打印摘要（便于脚本消费）")
    args = ap.parse_args()

    setup_logging()
    log = get_logger("scripts.fetch_catalog")

    cm = ConfigManager()
    cfg = cm.load()
    state = load_session_state()
    print("登录态：" + (state.summary() if not state.is_empty() else "（空，请先运行 scripts/login.py）"))
    print("登录态文件：" + str(paths.storage_state_path()))

    with EcnuClient(cfg, config_manager=cm, session=state) as client:
        if args.diagnose:
            diag = client.diagnose_access()
            print(diag.to_text())
            return 0 if diag.reachable else 3

        if state.is_empty():
            print("⛔ 尚未登录，请先运行： .venv\\Scripts\\python scripts\\login.py", file=sys.stderr)
            return 2

        try:
            if args.raw:
                for path in list(client.COURSE_ENDPOINTS)[:3]:
                    try:
                        payload = client.request("POST", path, json_body={"pageNum": 1, "pageSize": 5})
                        print(f"\n=== {path} ===")
                        print(json.dumps(payload, ensure_ascii=False)[:2000])
                    except Exception as exc:  # noqa: BLE001
                        print(f"\n=== {path} === 失败：{type(exc).__name__}: {exc}")

            catalog = client.fetch_catalog(on_progress=lambda m: print("  " + m), save=not args.no_save)
        except AuthExpiredError as exc:
            print(f"\n⛔ 登录态失效（AuthExpiredError）：{exc}", file=sys.stderr)
            print("   请重新运行： .venv\\Scripts\\python scripts\\login.py", file=sys.stderr)
            return 2
        except SiteUnreachableError as exc:
            print(f"\n⛔ 站点不可达：{exc}", file=sys.stderr)
            return 3
        except ApiChangedError as exc:
            print(f"\n⛔ 平台接口可能已变更：{exc}", file=sys.stderr)
            return 4
        finally:
            if args.dump_recon:
                print("接口记录：" + str(client.dump_network_log()))

    print()
    print(catalog.summary())
    print()
    print("前 3 条：")
    for i, res in enumerate(catalog.resources[:3], 1):
        print(
            f"  {i}. [{res.course_name}] {res.title} | "
            f"{res.duration_sec:.0f}s | {res.record_time} | {res.mime or '-'} | "
            f"id={res.resource_id} | url={'有' if res.play_url else '无'}"
        )
    if not args.no_save:
        print("\n清单已写入：" + str(paths.catalog_path()))

    if args.json:
        print(json.dumps(catalog.to_dict(), ensure_ascii=False, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
