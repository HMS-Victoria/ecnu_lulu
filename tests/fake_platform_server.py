"""仿「真实平台 v1 接口」的本地假服务器（供 tests/test_platform_api.py 使用）。

它只模仿**实测到的契约**，用来把契约钉死，防止以后重构时悄悄改坏：

    GET  /jy-application-resourcemanage/oauth2/token        → result.jwt_token
    头   jwt-token: <token>                                   （真实接口的鉴权头名）
    GET  /v1/list/termYear                                  → 学期列表
    GET  /v1/myself/curriculum?acteId=&page.pageIndex=&…     → data.records（分页字段名必须是
                                                               page.pageIndex/page.pageSize）
    GET  /v1/course_vod_urls_new?courseId=                   → data.courseVodViewList[].url

另外支持几种故障模式：
    mode="no_token"   换 token 时 302 跳登录（模拟登录态失效）
    mode="bad_paging" 课表接口只接受 page.pageIndex 形式，否则回「分页不能为空」
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
from urllib.parse import parse_qs, urlparse

TOKEN = "FAKE-JWT-TOKEN-" + "x" * 40

TERMS = [
    {"id": 1, "acteTerm": 2, "acyeCode": "2024-2025", "currentTerm": False},
    {"id": 2, "acteTerm": 1, "acyeCode": "2025-2026", "currentTerm": True},
]

#: 两门课、三节课；其中 线性代数 那节课有两个机位（时长不同，客户端应取最长的）
CURRICULUM = {
    2: [
        {
            "id": 351351, "subjName": "线性代数", "teclId": 11949, "teacNames": ["杨争峰"],
            "courBeginTime": "2025-12-11 10:40:00", "clroName": "教书院319",
            "courVodOpen": 1, "courTime": 3301,
        },
        {
            "id": 382198, "subjName": "数据结构与算法", "teclId": 22909, "teacNames": ["杜育根"],
            "courBeginTime": "2026-03-03 13:00:00", "clroName": "教书院116",
            "courVodOpen": 1, "courTime": 3200,
        },
        {
            "id": 999999, "subjName": "没有录像的课", "teclId": 1, "teacNames": ["某人"],
            "courBeginTime": "2026-03-04 08:00:00", "clroName": "某教室",
            "courVodOpen": 0, "courTime": 3000,
        },
    ]
}

VIDEOS = {
    351351: [
        {"vodId": 701815, "vodTime": 3299, "viewNum": 5, "url": "https://media.example/vod/701815.mp4?auth_key=a"},
        {"vodId": 701828, "vodTime": 3301, "viewNum": 1, "url": "https://media.example/vod/701828.mp4?auth_key=b"},
    ],
    382198: [
        {"vodId": 800001, "vodTime": 3200, "viewNum": 2, "url": "https://media.example/vod/800001.mp4?auth_key=c"},
    ],
}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "normal"
    #: 记录服务端看到的请求，便于断言「鉴权头有没有带」「分页参数名对不对」
    seen: list[dict] = []

    def log_message(self, *args):  # noqa: A003
        pass

    def _send(self, code: int, payload, *, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _record(self, path: str, query: dict) -> None:
        type(self).seen.append(
            {
                "path": path,
                "query": {k: v[0] for k, v in query.items()},
                "jwt": self.headers.get("jwt-token", ""),
                "cookie": self.headers.get("Cookie", ""),
            }
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        self._record(path, query)
        api = "/jy-application-resourcemanage"

        if path == f"{api}/oauth2/token":
            if type(self).mode == "no_token":
                # 模拟登录态失效：跳登录页
                self.send_response(302)
                self.send_header("Location", "https://sso.ecnu.edu.cn/login")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(200, {"code": "0", "result": {
                "access_token": "short-access", "jwt_token": TOKEN,
                "expires_in": 86400, "username": "20261234567",
            }})
            return

        if path == f"{api}/v1/list/termYear":
            if not self.headers.get("jwt-token"):
                self._send(200, {"ok": False, "dataOk": False, "code": "401", "message": "未登录"})
                return
            self._send(200, {"code": None, "data": TERMS, "dataOk": True, "ok": True})
            return

        if path == f"{api}/v1/myself/curriculum":
            if "page.pageIndex" not in query:
                self._send(200, {"code": "2011", "data": None, "dataOk": False,
                                 "message": "分页不能为空", "ok": False})
                return
            acte = int(query.get("acteId", ["0"])[0])
            rows = CURRICULUM.get(acte, [])
            self._send(200, {"code": None, "data": {
                "records": rows, "rowCount": len(rows), "pageCount": 1,
                "pageIndex": int(query["page.pageIndex"][0]),
            }, "dataOk": True, "ok": True})
            return

        if path == f"{api}/v1/course_vod_urls_new":
            cour = int(query.get("courseId", ["0"])[0])
            self._send(200, {"code": None, "data": {
                "courName": "线性代数" if cour == 351351 else "其它课",
                "courseVodViewList": VIDEOS.get(cour, []),
            }, "dataOk": True, "ok": True})
            return

        self._send(404, {"timestamp": 0, "status": 404, "error": "Not Found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        body = {}
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            pass
        self._record(parsed.path, parse_qs(parsed.query))
        type(self).seen[-1]["body"] = body
        api = "/jy-application-resourcemanage"

        if parsed.path == f"{api}/v1/statistics/teaching-class/user/course-list":
            # 真实平台要求字段名是 teachingClassId
            if not body.get("teachingClassId"):
                self._send(200, {"code": None, "data": None, "dataOk": False,
                                 "message": "教学班id不为空", "ok": False})
                return
            self._send(200, {"code": None, "data": CURRICULUM[2], "dataOk": True, "ok": True})
            return

        self._send(404, {"timestamp": 0, "status": 404, "error": "Not Found", "path": parsed.path})


class FakePlatform:
    def __init__(self, mode: str = "normal") -> None:
        self.mode = mode
        self.seen: list[dict] = []

    def __enter__(self) -> "FakePlatform":
        handler = type("_Handler", (Handler,), {"mode": self.mode, "seen": self.seen})
        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
