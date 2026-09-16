"""模拟平台服务器：用于在**无网络**情况下端到端验证清单客户端（M1）。

真实课程平台校外不可达（被学校 webVPN 网关拦下），但 `EcnuClient` 的 HTTP 逻辑
——分页、业务码、鉴权失效、候选路径探测、播放地址解析——必须能被验证。
这里起一个本地 HTTP 服务，按平台常见的 ``{code, msg, data}`` 包装返回数据，
然后用**同一个** `EcnuClient` 完整跑一遍 ``fetch_catalog()``。

服务行为（可用 URL 前缀切换）：
    /api/...                正常：课程 3 门 / 资源 7 条，分页 total 生效
    /empty/api/...          课程列表为空（total=0）
    /autherr/api/...        业务码 A0230（登录已过期）
    /http401/api/...        HTTP 401
    /redirect/api/...       302 跳登录页
    /slow/api/...           正确路径只在第二个候选上（验证候选探测）
    /weird/api/...          响应结构异常（验证 ApiChangedError）

用法::

    .venv\\Scripts\\python scripts\\mock_platform.py            # 起服务并跑完整验证
    .venv\\Scripts\\python scripts\\mock_platform.py --serve    # 只起服务，供手工调试
"""

from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Any
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# --------------------------------------------------------------------------- #
# 假数据：3 门课、7 条录播，字段命名故意混用多种风格（贴近真实平台的混乱）
# --------------------------------------------------------------------------- #
RECORD_BASE = "https://media.example.edu/hls"

#: 播放地址前缀。``serve()`` 会把 ``/media/...`` 的真实媒体服务地址填进来，
#: 于是「清单 → 播放地址 → 下载 → 转写」整条链路可以在本地完整跑通。
PLAY_BASE = RECORD_BASE


def _play_url(resource_id: str, *, file: str = "index.m3u8", sign: bool = True) -> str:
    url = f"{PLAY_BASE}/{resource_id}/{file}"
    return f"{url}?sign=sig-{resource_id}&expires=1790000000" if sign else url

COURSES = [
    {
        "courseId": "C-1001",
        "courseName": "数据结构与算法",
        "teacherName": "张老师",
        "term": "2025-2026-1",
    },
    {
        "courseId": "C-1002",
        "course_name": "编译原理",
        "teacher": "李老师",
    },
    {
        "courseId": "C-1003",
        "name": "操作系统",
        "lecturer": "王老师",
    },
]

def _resources() -> dict[str, list[dict[str, Any]]]:
    """每次调用都按当前 PLAY_BASE 生成播放地址（便于切到本地媒体服务）。"""
    return {        "C-1001": [
            {
                "resourceId": "R-10011",
                "title": "第1讲 绪论与算法复杂度",
                "duration": 2712,
                "recordTime": "2025-09-08 08:00:00",
                "playUrl": _play_url("R-10011"),
                "fileSize": 524288000,
                "contentType": "application/vnd.apple.mpegurl",
            },
            {
                "resourceId": "R-10012",
                "resourceName": "第2讲 线性表",
                "durationSec": "01:02:05",          # 字符串时长
                "startTime": 1757000000000,          # 毫秒时间戳
                "videoUrl": _play_url("R-10012", sign=False),
                "size": "498000000",
            },
            {
                "resourceId": "R-10013",
                "title": "第3讲 栈与队列",
                "duration": 2500,
                "recordTime": "2025-09-22 08:00:00",
                "playUrl": _play_url("R-10013", sign=False),
            },
        ],
        "C-1002": [
            {
                "id": "R-10021",
                "name": "第1讲 词法分析",
                "timeLength": "45:30",
                "createTime": "2025-09-10T13:05:00",
                "url": _play_url("R-10021", file="master.m3u8", sign=False),
            },
            {
                "id": "R-10022",
                "name": "第2讲 语法分析",
                "timeLength": "50:00",
                "createTime": "2025-09-17T13:05:00",
                "url": _play_url("R-10022", file="master.m3u8", sign=False),
            },
        ],
        "C-1003": [
            {
                "videoId": "R-10031",
                "videoName": "第1讲 进程与线程",
                "videoDuration": 3005,
                "publishTime": "2025-09-12 10:00:00",
                # 故意不给 play_url，用来验证 resolve_play_url 走播放接口
            },
            {
                "videoId": "R-10032",
                "videoName": "第2讲 内存管理",
                "videoDuration": 2890,
                "publishTime": "2025-09-19 10:00:00",
            },
        ],
    }


COURSE_PAGE_SIZE = 2  # 强制翻页：3 门课 → 2 页
RESOURCE_PAGE_SIZE = 2  # 强制翻页：C-1001 有 3 条 → 2 页


def _ok(data: Any) -> dict[str, Any]:
    return {"code": 200, "msg": "success", "data": data}


def _paginate(rows: list[Any], body: dict[str, Any], *, page_size: int) -> dict[str, Any]:
    page = int(body.get("pageNum") or body.get("current") or 1)
    size = int(body.get("pageSize") or body.get("size") or page_size)
    start = (page - 1) * size
    return {"total": len(rows), "rows": rows[start : start + size], "pageNum": page, "pageSize": size}


# --------------------------------------------------------------------------- #
class MockPlatform(http.server.BaseHTTPRequestHandler):
    """模拟平台。

    **模式通过端口区分**（每个模式一个 server 实例），而不是 URL 前缀 ——
    因为客户端用 ``urljoin(api_base, "/api/...")`` 拼路径，绝对路径会把
    URL 前缀整段替换掉，前缀式模式根本传不到服务端（踩过一次）。
    """

    protocol_version = "HTTP/1.1"
    server_version = "MockECNUPlatform/1.0"

    def log_message(self, fmt, *args):  # noqa: A002
        if getattr(self.server, "verbose", False):
            print(f"[mock] {fmt % args}", flush=True)

    # -- 工具 ------------------------------------------------------------- #
    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    @property
    def mode(self) -> str:
        return str(getattr(self.server, "mode", "normal"))

    @staticmethod
    def _path() -> str:
        return "/" + "/".join(p for p in MockPlatform._raw_path().split("?")[0].split("/") if p)

    @staticmethod
    def _raw_path() -> str:
        return ""

    # -- 路由 ------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        path = self._norm_path()
        if path in ("/health", "/"):
            self._json(200, {"status": "ok", "mode": self.mode})
            return
        # 媒体文件：把本地生成的 HLS 直接发出去，让「下载 → 转写」也能离线跑通
        media_dir = getattr(self.server, "media_dir", None)
        if media_dir:
            rel = path.lstrip("/")
            target = (Path(media_dir) / rel).resolve()
            try:
                inside = target.is_relative_to(Path(media_dir).resolve())
            except AttributeError:  # Python < 3.9 兜底（本项目要求 3.11+，仅为稳妥）
                inside = str(target).startswith(str(Path(media_dir).resolve()))
            if inside and target.is_file():
                data = target.read_bytes()
                ctype = {
                    ".m3u8": "application/vnd.apple.mpegurl",
                    ".ts": "video/mp2t",
                    ".key": "application/octet-stream",
                    ".mp3": "audio/mpeg",
                    ".m4a": "audio/mp4",
                    ".wav": "audio/wav",
                }.get(target.suffix.lower(), "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        self._json(404, {"code": 404, "msg": f"未知路径 {path}"})

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _norm_path(self) -> str:
        return "/" + "/".join(p for p in self.path.split("?")[0].split("/") if p)

    def do_POST(self) -> None:  # noqa: N802
        mode = self.mode
        path = self._norm_path()
        body = self._read_body()

        # ---- 故障模式 ----
        if mode == "http401":
            self._json(401, {"code": 401, "msg": "未登录"})
            return
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "/users/sign_in")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "autherr":
            self._json(200, {"code": "A0230", "msg": "登录已过期，请重新登录", "data": None})
            return
        if mode == "weird":
            self._json(200, {"code": 200, "msg": "ok", "data": {"somethingElse": {"a": 1}}})
            return

        # ---- 正常模式 ----
        if path.endswith("/course/list"):
            if mode == "empty":
                self._json(200, _ok({"total": 0, "rows": []}))
                return
            if mode == "slow":
                # 模拟「改名后的接口」：第一个候选路径已下线（业务错误），
                # 第二个候选才可用 → 验证客户端的候选探测能力。
                count = int(getattr(self.server, "course_hits", 0)) + 1
                self.server.course_hits = count  # type: ignore[attr-defined]
                if count == 1:
                    self._json(200, {"code": 500, "msg": "接口不存在", "data": None})
                    return
            self._json(200, _ok(_paginate(COURSES, body, page_size=COURSE_PAGE_SIZE)))
            return

        if path.endswith("/resource/list"):
            cid = str(body.get("courseId") or body.get("id") or "")
            rows = _resources().get(cid, [])
            self._json(200, _ok(_paginate(rows, body, page_size=RESOURCE_PAGE_SIZE)))
            return

        if path.endswith("/resource/play"):
            rid = str(body.get("resourceId") or body.get("id") or "")
            # 模拟「带时效签名」的播放地址
            self._json(
                200,
                _ok(
                    {
                        "playUrl": _play_url(rid),
                        "duration": 3005,
                        "qualities": [
                            {"name": "标清", "url": _play_url(rid, file="360p.m3u8", sign=False)},
                            {"name": "高清", "url": _play_url(rid, file="720p.m3u8", sign=False)},
                        ],
                    }
                ),
            )
            return

        self._json(404, {"code": 404, "msg": f"未知接口 {path}"})


# --------------------------------------------------------------------------- #
def serve(
    port: int = 0,
    *,
    mode: str = "normal",
    verbose: bool = False,
    media_dir: Path | None = None,
    play_base: str = "",
) -> tuple[str, socketserver.TCPServer]:
    """起一个指定模式的模拟平台，返回 (base_url, server)。

    ``media_dir`` 不为空时会额外提供静态媒体服务（HLS/分片/key），
    让「清单 → 播放地址 → 下载 → 转写」整条链路都能在本地跑通。
    """
    global PLAY_BASE
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), MockPlatform)
    httpd.daemon_threads = True
    httpd.mode = mode  # type: ignore[attr-defined]
    httpd.verbose = verbose  # type: ignore[attr-defined]
    if media_dir is not None:
        httpd.media_dir = Path(media_dir)  # type: ignore[attr-defined]
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    PLAY_BASE = (play_base or base).rstrip("/")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return base, httpd


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="模拟平台服务器 + 清单客户端端到端验证")
    ap.add_argument("--serve", action="store_true", help="只起服务，不跑验证")
    ap.add_argument("--port", type=int, default=0, help="端口（0=随机）")
    ap.add_argument("--verbose", action="store_true", help="打印每个请求")
    args = ap.parse_args()

    base, httpd = serve(args.port, mode="normal", verbose=args.verbose)
    print("=" * 78)
    print(f"模拟平台已启动：{base}")
    print("  接口：POST /api/course/list  /api/resource/list  /api/resource/play")
    print("  故障模式：每个模式一个独立端口（见验证输出里的 URL）")
    print("=" * 78)
    if args.serve:
        print("（--serve 模式，Ctrl+C 退出）")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    # ---------------- 验证 ----------------
    import shutil
    import tempfile

    from ecnu_transcribe import paths
    from ecnu_transcribe.catalog import Catalog, Resource
    from ecnu_transcribe.client import EcnuClient, SessionState
    from ecnu_transcribe.config import AppConfig
    from ecnu_transcribe.errors import ApiChangedError, AuthExpiredError, SiteUnreachableError
    from ecnu_transcribe.logbus import setup_logging

    setup_logging()
    pass_n: list[str] = []
    fail_n: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        (pass_n if ok else fail_n).append(name)
        print(f"  {'✅' if ok else '⛔'} {name}{('  — ' + detail) if detail else ''}")

    work = Path(tempfile.mkdtemp(prefix="mock-platform-"))
    session = SessionState(cookies={"JSESSIONID": "FAKE-SESSION-VALUE"}, saved_at=time.time())

    # 每个模式一个独立 server（端口即模式）
    servers: dict[str, socketserver.TCPServer] = {"normal": httpd}
    bases: dict[str, str] = {"normal": base}
    for mode in ("empty", "autherr", "http401", "redirect", "slow", "weird"):
        b, s = serve(0, mode=mode)
        bases[mode] = b
        servers[mode] = s

    def client_for(mode: str = "normal", **kw) -> EcnuClient:
        cfg = AppConfig()
        cfg.api_base = bases[mode]
        cfg.portal_url = f"{bases[mode]}/#/home"
        cfg.jitter_min = 0.0
        cfg.jitter_max = 0.0
        cfg.request_timeout = 10.0
        return EcnuClient(cfg, session=session, **kw)

    print("\n[1] 全量清单：分页 / 字段宽容解析 / 保存")
    with client_for() as c:
        diag = c.diagnose_access()
        check("可达性诊断", diag.reachable, f"mode={diag.mode}")
        cat = c.fetch_catalog(save=False)
    check("课程数 = 3（翻了 2 页）", len(cat.courses) == 3, f"实际 {len(cat.courses)}")
    check("资源总数 = 7", cat.resource_count == 7, f"实际 {cat.resource_count}")
    expect_total = 2712 + 3725 + 2500 + 2730 + 3000 + 3005 + 2890
    check("时长解析正确（含 \"01:02:05\"/\"45:30\" 字符串形式）",
          abs(cat.total_duration - expect_total) < 1, f"{cat.total_duration:.0f}s vs 期望 {expect_total}s")
    names = {c.course_name for c in cat.courses}
    check("课程名解析（camelCase/snake_case/name 三种风格）",
          names == {"数据结构与算法", "编译原理", "操作系统"}, str(sorted(names)))
    first = cat.courses[0].resources[0]
    check("资源字段宽容解析", first.resource_id == "R-10011" and first.duration_sec == 2712,
          f"{first.resource_id} {first.duration_sec}s")
    check("毫秒时间戳已转成本地时间", len(cat.courses[0].resources[1].record_time) == 19,
          cat.courses[0].resources[1].record_time)
    check("mime 推断（无 contentType 时按 URL 猜）",
          cat.courses[1].resources[0].mime == "application/vnd.apple.mpegurl",
          cat.courses[1].resources[0].mime)

    print("\n[2] catalog.json 落盘与读回")
    path = cat.save(work / "catalog.json")
    check("catalog.json 已写出", path.is_file(), f"{path.stat().st_size} bytes")
    raw = json.loads(path.read_text(encoding="utf-8"))
    check("JSON 顶层统计字段齐全",
          {"course_count", "resource_count", "total_duration_sec", "summary"} <= set(raw),
          raw.get("summary", ""))
    loaded = Catalog.load(path)
    check("读回后资源数一致", loaded.resource_count == cat.resource_count)
    check("读回后时长一致", abs(loaded.total_duration - cat.total_duration) < 1)

    print("\n[3] 播放地址解析（清单未带 URL 时走播放接口）")
    with client_for() as c:
        res = Resource(resource_id="R-10031", title="第1讲 进程与线程", course_id="C-1003")
        url = c.resolve_play_url(res)
    check("拿到播放地址", "/R-10031/index.m3u8" in url, url)
    check("带时效签名参数被保留", "sign=" in url and "expires=" in url)
    check("顺带补全时长", res.duration_sec == 3005, f"{res.duration_sec}")

    print("\n[4] 鉴权失效必须显式报错（不能静默返回空清单）")
    for mode, label in (("autherr", "业务码 A0230"), ("http401", "HTTP 401"), ("redirect", "302 跳登录页")):
        try:
            with client_for(mode) as c:
                c.fetch_catalog(save=False)
            check(f"{label} → AuthExpiredError", False, "没有抛异常（会被误认为「没有录播」）")
        except AuthExpiredError as exc:
            check(f"{label} → AuthExpiredError", True, str(exc)[:60])
        except Exception as exc:  # noqa: BLE001
            check(f"{label} → AuthExpiredError", False, f"抛的是 {type(exc).__name__}: {exc}")

    print("\n[5] 结构异常 → ApiChangedError（显式接口变更检测）")
    try:
        with client_for("weird") as c:
            c.fetch_catalog(save=False)
        check("结构异常 → ApiChangedError", False, "没有抛异常")
    except ApiChangedError as exc:
        check("结构异常 → ApiChangedError", True, str(exc)[:70])
    except Exception as exc:  # noqa: BLE001
        check("结构异常 → ApiChangedError", False, f"抛的是 {type(exc).__name__}")

    print("\n[6] 空清单是合法结果（total=0 不应报错）")
    try:
        with client_for("empty") as c:
            cat_empty = c.fetch_catalog(save=False)
        check("total=0 正常返回空清单", cat_empty.resource_count == 0, cat_empty.summary())
    except Exception as exc:  # noqa: BLE001
        check("total=0 正常返回空清单", False, f"抛了 {type(exc).__name__}: {exc}")

    print("\n[7] 候选路径探测（第一个候选失败时自动切到下一个）")
    with client_for("slow") as c:
        cats = c.fetch_courses()
        cached = dict(c._endpoint_cache)
    check("候选探测自动切换并拿到数据", len(cats) == 3, f"课程 {len(cats)} 门")
    check("命中路径被缓存（且不是默认第一个）",
          bool(cached) and "jy-application-resourcemanage-ui" not in next(iter(cached.values()), ""),
          str(cached))

    print("\n[8] 请求记录已脱敏")
    # 注意：抓包改写到**本脚本自己的目录**，不再写 recon/network.jsonl。
    # 那个文件是「真实平台流量」产物（用于反推接口，见 docs/API.md），
    # 而这里产生的全是 127.0.0.1 模拟流量 —— 混进去只会埋掉真实请求
    # （实测曾累积 644 条模拟记录 vs 5 条真实记录，见缺陷 39）。
    rec_path = work / "network.jsonl"
    with client_for() as c:
        c.fetch_catalog(save=False)
        c.dump_network_log(rec_path)         # 显式路径：模拟流量写到模拟目录
    text = rec_path.read_text(encoding="utf-8") if rec_path.is_file() else ""
    check("抓包文件已生成", len(text) > 100, f"{len(text)} bytes → {rec_path}")
    check("不含 Cookie 明文值",
          "FAKE-SESSION-VALUE" not in text.upper(), "假 Cookie 值未出现在记录里")
    check("请求记录条数 > 0", text.count("\n") >= 1, f"{text.count(chr(10))} 行")
    check("记录了真实接口路径", "course/list" in text or "resource/list" in text)
    real_capture = paths.recon_dir() / "network.jsonl"
    check(
        "模拟流量未污染真实抓包文件",
        (not real_capture.is_file())
        or "127.0.0.1" not in real_capture.read_text(encoding="utf-8", errors="replace"),
        str(real_capture),
    )

    for s in servers.values():
        s.shutdown()
    shutil.rmtree(work, ignore_errors=True)

    print("\n" + "=" * 78)
    print(f"结果：{len(pass_n)} 项通过，{len(fail_n)} 项失败")
    for name in fail_n:
        print("  ⛔ " + name)
    print("=" * 78)
    return 1 if fail_n else 0


if __name__ == "__main__":
    raise SystemExit(main())
