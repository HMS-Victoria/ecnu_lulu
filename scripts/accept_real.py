"""真实账号端到端验收：2 条真实录播 → 音频 → 本地 ASR → txt/srt/md。

数据来源全部是**实测出来的真实接口**：
    GET  /jy-application-resourcemanage/oauth2/token          → result.jwt_token
    POST /v1/list/recentWatchRecord                            → 我的课程节次(courId/teclId/科目/教师)
    GET  /v1/course_vod_urls_new?courseId=<courId>             → 带签名的 mp4 播放地址 + vodId + vodTime
    （另：GET /v1/getVodCourseVideo?courId= 可列出该节次的所有录播）

然后走本项目自己的流水线：ffmpeg 拉流取音频 → ASR → exporter 产出 `.txt/.srt/.md`。
ASR 用**本机 local_asr_server**（零成本、音频不出本机），不依赖任何云 Key。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import httpx  # noqa: E402

from ecnu_transcribe.catalog import Resource  # noqa: E402
from ecnu_transcribe.client import load_session_state  # noqa: E402
from ecnu_transcribe.config import ConfigManager  # noqa: E402
from ecnu_transcribe.logbus import get_logger, setup_logging  # noqa: E402
from ecnu_transcribe.pipeline import Pipeline, PipelineHooks  # noqa: E402
from ecnu_transcribe.store import Stage, StateStore, TaskRecord  # noqa: E402

BASE = "https://courses.ecnu.edu.cn"
API = "/jy-application-resourcemanage"
OUT_JSON = ROOT / "build" / "real_e2e_result.json"


def api_client(cfg):
    """换 jwt-token 并返回 (client, headers, 学号)。

    登录态过期时**明确报「请重新登录」**，而不是抛 JSONDecodeError ——
    实测踩过：隔夜后 webVPN 会话失效，token 接口返回 302（空 body），
    旧代码直接崩在 `.json()` 上，看起来像程序坏了。
    """
    state = load_session_state()
    c = httpx.Client(
        headers={
            "User-Agent": cfg.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": cfg.portal_url,
            "Origin": BASE,
        },
        cookies=state.cookies, timeout=60.0, verify=cfg.verify_tls,
        follow_redirects=False, trust_env=False,
    )
    resp = c.get(f"{BASE}{API}/oauth2/token")
    if resp.status_code in (301, 302, 303, 307, 308):
        c.close()
        loc = resp.headers.get("location", "")
        raise SystemExit(
            "⛔ 登录态已失效（换 jwt-token 时被重定向到登录）。\n"
            f"   Location: {loc[:120]}\n"
            "   请重新登录一次：\n"
            "     .venv\\Scripts\\python scripts\\login.py --capture\n"
            "   或双击 dist\\大夏学堂转写助手\\大夏学堂转写助手.exe 后点「登录」。\n"
            "   （实测：webVPN 会话隔夜会失效，重新认证后即可继续。）"
        )
    if resp.status_code >= 400:
        c.close()
        raise SystemExit(f"⛔ 换 jwt-token 失败：HTTP {resp.status_code} {resp.text[:200]}")
    try:
        result = (resp.json() or {}).get("result") or {}
    except ValueError:
        c.close()
        raise SystemExit(
            f"⛔ 换 jwt-token 返回的不是 JSON（HTTP {resp.status_code}，{len(resp.content)} 字节）："
            f"{resp.text[:200]}\n   多半是登录态失效，请重新登录。"
        ) from None
    jwt = str(result.get("jwt_token") or "")
    if not jwt:
        c.close()
        raise SystemExit(f"⛔ 响应里没有 jwt_token：{resp.text[:200]}")
    return c, {"jwt-token": jwt}, str(result.get("username") or "")


def collect_targets(c: httpx.Client, h: dict, want: int, *,
                    subjects: str = "", picks: str = "") -> list[dict]:
    """挑 want 个真实节次并取到播放地址。

    ``subjects``：只挑这些科目（顺序即优先级）。
    ``picks``：直接给 courId。

    为什么要能指定：**实测不同录像的音频电平差异极大**
    （峰值从 -2 dB 到 -80 dB），近乎无声的录像会让任何 ASR 返回空文本。
    验收要挑电平正常的录像，否则会把「学校录播设备没录到声音」误判成本工具的问题。
    """
    want_subjects = [s.strip() for s in subjects.split(",") if s.strip()]
    want_cour = [s.strip() for s in picks.split(",") if s.strip()]
    r = c.post(f"{BASE}{API}/v1/list/recentWatchRecord",
               json={"page": {"pageIndex": 1, "pageSize": 50}}, headers=h)
    data = (r.json() or {}).get("data") or {}
    sessions: list[dict] = []
    for row in data.get("list") or []:
        for info in row.get("courseInfoList") or []:
            dto = info.get("courseOfEsDto") or {}
            cour_id = info.get("courId") or dto.get("id")
            if not cour_id:
                continue
            sessions.append({
                "cour_id": cour_id,
                "tecl_id": dto.get("teclId"),
                "subject": dto.get("subjName") or "",
                "teachers": dto.get("teacNames") or [],
                "begin": dto.get("courBeginTime") or "",
                "room": dto.get("clroName") or "",
                "tecl_code": dto.get("teclCode") or "",
            })

    if want_cour:
        # 指定 courId 时，若不在观看记录里，就用课表补齐科目/教师信息
        known = {str(s["cour_id"]): s for s in sessions}
        for cour in want_cour:
            if cour in known:
                continue
            found = False
            for term in (c.get(f"{BASE}{API}/v1/list/termYear", headers=h).json() or []):
                if found:
                    break
                page = 1
                while page <= 20 and not found:
                    payload = c.get(
                        f"{BASE}{API}/v1/myself/curriculum",
                        params={"acteId": term["id"], "page.pageIndex": page, "page.pageSize": 100},
                        headers=h,
                    ).json()
                    data = payload.get("data") or {}
                    rows = data.get("records") or data.get("list") or []
                    for item in rows:
                        if str(item.get("id")) == cour:
                            sessions.append({
                                "cour_id": item.get("id"),
                                "tecl_id": item.get("teclId"),
                                "subject": item.get("subjName") or "",
                                "teachers": item.get("teacNames") or [],
                                "begin": item.get("courBeginTime") or "",
                                "room": item.get("clroName") or "",
                                "tecl_code": item.get("teclCode") or "",
                            })
                            found = True
                            break
                    if not rows or len(rows) < 100:
                        break
                    page += 1
            if not found:
                # 课表里找不到**不等于**没权限：实测 `recentWatchRecord` 返回的 courId
                # 与 `myself/curriculum` 的 `id` **不是同一套 ID 空间**，显式指定
                # `--picks 395443` 时就出现过「不在课表里」，然后这条被**静默丢掉** ——
                # 验收结果于是依赖「最近观看」的波动，不可复现。
                # 这里退化为「直接用 courId 取播放地址」，并从响应里的 courName 补出课程名。
                print(f"  ⚠️ courId={cour} 不在课表里（ID 空间不同或确无权限），"
                      "改为直接用 courId 取播放地址")
                sessions.append({
                    "cour_id": cour,
                    "tecl_id": None,
                    "subject": "",
                    "teachers": [],
                    "begin": "",
                    "room": "",
                    "tecl_code": "",
                })
        order = {cour: i for i, cour in enumerate(want_cour)}
        sessions = [s for s in sessions if str(s["cour_id"]) in order]
        sessions.sort(key=lambda s: order[str(s["cour_id"])])
    elif want_subjects:
        order = {name: i for i, name in enumerate(want_subjects)}
        sessions = [s for s in sessions if s["subject"] in order]
        sessions.sort(key=lambda s: order[s["subject"]])

    print(f"候选节次 {len(sessions)} 个：")
    for s in sessions:
        print(f"  courId={s['cour_id']} {s['subject']} {s['teachers']} {s['begin']} {s['room']}")

    targets: list[dict] = []
    seen_subjects: set[str] = set()
    for s in sessions:
        if len(targets) >= want:
            break
        if not want_cour and s["subject"] in seen_subjects:
            continue
        r = c.get(f"{BASE}{API}/v1/course_vod_urls_new",
                  params={"courseId": s["cour_id"]}, headers=h)
        data = (r.json() or {}).get("data") or {}
        vids = (data.get("courseVodViewList") or data.get("courseVodVideoDtoList")
                or data.get("courseVodVideoList") or [])
        if not vids:
            for val in data.values():
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, dict) and item.get("url"):
                            vids.append(item)
        if not vids:
            print(f"  ⚠️ courId={s['cour_id']} 没有可播放录播，跳过")
            continue
        v = max(vids, key=lambda x: float(x.get("vodTime") or 0))
        # 课程名以播放地址响应里的 courName 为准；课表查不到（ID 空间不同）时，
        # 它是**唯一**能拿到的课程名 —— 拿不到就会产出「 2026-03-16 vod800713」这种
        # 前导空格的标题，并把已有任务的产物改名。
        cour_name = data.get("courName") or s["subject"] or f"cour{s['cour_id']}"
        seen_subjects.add(s["subject"] or cour_name)
        targets.append({
            **s,
            "subject": s["subject"] or cour_name,
            "vod_id": v.get("vodId"),
            "play_url": v.get("url"),
            "vod_time": v.get("vodTime"),
            "all_vods": [{"vodId": x.get("vodId"), "vodTime": x.get("vodTime")} for x in vids],
            "cour_name": cour_name,
        })
    return targets


def main() -> int:
    ap = argparse.ArgumentParser(description="真实录播端到端验收")
    ap.add_argument("--count", type=int, default=2, help="跑几条（默认 2）")
    ap.add_argument("--subjects", default="",
                    help="只挑这些科目（逗号分隔，按顺序）。"
                         "实测不同录像的音频电平差异极大（峰值 -2 dB ~ -80 dB），"
                         "近乎无声的录像会让 ASR 返回空文本 —— 所以验收要挑电平正常的。")
    ap.add_argument("--picks", default="",
                    help="直接指定 courId（逗号分隔），跳过自动挑选")
    ap.add_argument("--asr-base", default="http://127.0.0.1:8418/v1", help="本机 ASR 端点")
    ap.add_argument("--asr-model", default="faster-whisper-small")
    ap.add_argument("--output-dir", default="", help="产物目录（默认用配置里的）")
    ap.add_argument("--max-segment", type=int, default=300, help="单段 ASR 最长秒数")
    args = ap.parse_args()

    setup_logging()
    log = get_logger("real_e2e")
    cm = ConfigManager()
    cfg = cm.load()
    cfg.asr_provider = "openai_compatible"
    cfg.asr_base_url = args.asr_base
    cfg.asr_model = args.asr_model
    cfg.asr_timestamps = True
    cfg.asr_language = "zh"
    cfg.asr_max_segment_sec = args.max_segment
    cfg.asr_chunk_strategy = "silence"
    cfg.concurrency = 1
    cfg.llm_enabled = False
    if args.output_dir:
        cfg.output_dir = args.output_dir
    out_dir = Path(cfg.resolved_output_dir())
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 88)
    print("真实账号端到端验收")
    print(f"  ASR      : {cfg.asr_base_url} / {cfg.asr_model}（本机，零成本）")
    print(f"  输出目录 : {out_dir}")
    print(f"  条数     : {args.count}")
    print("=" * 88)

    c, h, user = api_client(cfg)
    print(f"鉴权成功：学号 {user}，jwt-token {len(h.get('jwt-token', ''))} 字符\n")
    targets = collect_targets(c, h, args.count, subjects=args.subjects, picks=args.picks)
    if not targets:
        print("⛔ 没有可跑的录播")
        return 1
    print(f"\n选中 {len(targets)} 条：")
    for t in targets:
        print(f"  《{t['subject']}》{t['begin']} vodId={t['vod_id']} 时长={t['vod_time']}s "
              f"教师={t['teachers']} 教室={t['room']}")
        print(f"    {str(t['play_url'])[:120]}...")
    c.close()

    store = StateStore()
    results: list[dict] = []
    hooks = PipelineHooks()
    hooks.stage = lambda tid, stage, progress, message="": print(
        f"    [{tid}] {stage:14s} {progress:5.1f}%  {message[:70]}", flush=True
    )
    hooks.log = lambda tid, message: print(f"    [{tid}] {message[:100]}", flush=True)
    pipe = Pipeline(cfg, store, cm=cm, hooks=hooks)
    try:
        for t in targets:
            res = Resource(
                resource_id=f"VOD-{t['vod_id']}",
                title=f"{t['subject']} {str(t['begin'])[:10]} vod{t['vod_id']}",
                course_name=t["cour_name"],
                teacher="、".join(t["teachers"]),
                duration_sec=float(t["vod_time"] or 0),
                record_time=str(t["begin"]),
                play_url=str(t["play_url"]),
            )
            task = store.upsert_task(TaskRecord(
                course_id=str(t["tecl_id"] or ""),
                course=t["cour_name"],
                resource_id=res.resource_id,
                title=res.title,
                output_dir=str(out_dir),
                duration_sec=res.duration_sec,
                play_url=res.play_url,
            ))
            # upsert 对**已存在**的任务是「幂等返回原记录、保留进度」，不会改 output_dir。
            # 于是 `--output-dir` 只是打印出来好看，产物仍然写进旧目录（本轮实测踩到：
            # 想用另一个目录做云端对照，结果它复用了旧稿、还写回了旧目录）。
            if str(task.output_dir or "") != str(out_dir):
                store.update_stage(task.id, task.stage, force=True, output_dir=str(out_dir))
                task = store.get_task(task.id) or task
                print(f"    （该任务原输出目录为 {task.output_dir}，已改为 {out_dir}）", flush=True)
            print(f"\n→ 开始《{res.title}》（task id={task.id}，时长 {res.duration_sec:.0f}s）", flush=True)
            t0 = time.time()
            final = pipe.run(task, res, force=False)
            cost = time.time() - t0
            outputs = [Path(p).name for p in (final.outputs or [])]
            print(f"  结果：stage={final.stage}  耗时 {cost/60:.1f} 分钟  产物={outputs}", flush=True)
            results.append({
                "resource_id": res.resource_id,
                "title": res.title,
                "course": res.course_name,
                "teacher": res.teacher,
                "duration_sec": res.duration_sec,
                "play_url": res.play_url,
                "stage": final.stage,
                "progress": final.progress,
                "outputs": [str(p) for p in (final.outputs or [])],
                "error": final.error,
                "cost_sec": round(cost, 1),
                "audio_path": final.audio_path,
            })
    finally:
        pipe.close()
        store.close()

    OUT_JSON.write_text(json.dumps({"user": user, "results": results}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    ok = [r for r in results if r["stage"] == str(Stage.DONE)]
    print("\n" + "=" * 88)
    print(f"完成：{len(ok)}/{len(results)} 条成功")
    for r in results:
        print(f"  {'✅' if r['stage'] == str(Stage.DONE) else '⛔'} {r['title']}  {r['outputs']}")
    print(f"结果 JSON：{OUT_JSON}")
    print("=" * 88)
    return 0 if len(ok) == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
