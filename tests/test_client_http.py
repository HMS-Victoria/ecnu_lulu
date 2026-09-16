"""清单客户端（M1）的离线端到端测试：对着**模拟平台服务器**跑真实 HTTP。

真实课程平台校外不可达（学校 webVPN 网关拦截），但客户端的 HTTP 逻辑必须被验证。
这里复用 `scripts/mock_platform.py` 起的本地服务：分页、业务码、鉴权失效、
候选路径探测、播放地址解析、字段宽容解析、脱敏记录，全部走真实 socket。

每个「故障模式」是一个独立端口的服务实例（URL 前缀会被 ``urljoin`` 吃掉，
所以不能用前缀区分模式 —— 踩过一次）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

from ecnu_transcribe.catalog import Catalog, Resource
from ecnu_transcribe.client import EcnuClient, SessionState
from ecnu_transcribe.config import AppConfig
from ecnu_transcribe.errors import ApiChangedError, AuthExpiredError

ROOT = Path(__file__).resolve().parents[1]


def _load_mock_module():
    path = ROOT / "scripts" / "mock_platform.py"
    spec = importlib.util.spec_from_file_location("mock_platform", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mock_platform"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mock():
    return _load_mock_module()


@pytest.fixture(scope="module")
def servers(mock):
    """每个模式一个独立 server；模块级复用，退出时统一关闭。"""
    out: dict[str, tuple[str, object]] = {}
    base, srv = mock.serve(0, mode="normal")
    out["normal"] = (base, srv)
    for mode in ("empty", "autherr", "http401", "redirect", "slow", "weird"):
        b, s = mock.serve(0, mode=mode)
        out[mode] = (b, s)
    yield out
    for _b, s in out.values():
        s.shutdown()


@pytest.fixture()
def session():
    return SessionState(cookies={"JSESSIONID": "FAKE-SESSION-VALUE"}, saved_at=time.time())


def _client(servers, mode: str, session: SessionState, **kw) -> EcnuClient:
    base = servers[mode][0]
    cfg = AppConfig()
    cfg.api_base = base
    cfg.portal_url = f"{base}/#/home"
    cfg.jitter_min = 0.0
    cfg.jitter_max = 0.0
    cfg.request_timeout = 10.0
    return EcnuClient(cfg, session=session, **kw)


# --------------------------------------------------------------------------- #
# 连通性诊断：把「连不上」细分到能直接照做的原因（缺陷 44）
# --------------------------------------------------------------------------- #
def test_interference_hint_distinguishes_reset_from_dns_and_timeout():
    """实测背景：本机 Clash 变全局代理后出口变香港，学校网关直接重置连接
    （WinError 10054），而界面只说「无法连接」——用户根本想不到是代理分流。"""
    import httpx

    from ecnu_transcribe.client import EcnuClient

    reset = EcnuClient._interference_hint(  # noqa: SLF001
        httpx.ConnectError("[WinError 10054] 远程主机强迫关闭了一个现有的连接。")
    )
    assert "重置" in reset and "ecnu.edu.cn" in reset and "DIRECT" in reset, reset

    dns = EcnuClient._interference_hint(  # noqa: SLF001
        httpx.ConnectError("[Errno -2] Name or service not known")
    )
    assert "解析" in dns and "重置" not in dns, dns

    slow = EcnuClient._interference_hint(httpx.ReadTimeout("timed out"))  # noqa: SLF001
    assert "超时" in slow and "SSL-VPN" in slow, slow

    assert EcnuClient._interference_hint(httpx.ConnectError("whatever")) == ""  # noqa: SLF001


def test_diagnose_error_includes_proxy_evidence(servers, session):
    """诊断报错时要带上「当前是否配了代理」这条证据，便于用户自查。

    注意：代理端口也必须是**关闭**的。第一版测试把代理写成 127.0.0.1:7890，
    结果本机 Clash 恰好在那监听，请求反而「成功」了（返回 502），
    于是 mode 既不是 error 也不是 vpn_required —— 测试自己踩了环境的坑。
    """
    cfg = AppConfig()
    cfg.api_base = "http://127.0.0.1:9"
    cfg.portal_url = "http://127.0.0.1:9/#/home"
    cfg.request_timeout = 3.0
    cfg.proxy = "http://127.0.0.1:9"          # 也是关闭端口，确定性失败
    with EcnuClient(cfg, session=session) as c:
        diag = c.diagnose_access()
    assert diag.mode == "error", diag.to_text()
    assert any("代理" in e for e in diag.evidence), diag.evidence


# --------------------------------------------------------------------------- #
# 全量清单
# --------------------------------------------------------------------------- #
def test_fetch_catalog_full(servers, session):
    with _client(servers, "normal", session) as c:
        cat = c.fetch_catalog(save=False)
    assert len(cat.courses) == 3, "应翻页拿到全部 3 门课"
    assert cat.resource_count == 7, "应拿到全部 7 条录播"
    assert {x.course_name for x in cat.courses} == {"数据结构与算法", "编译原理", "操作系统"}


def test_duration_parsing_including_string_forms(servers, session):
    with _client(servers, "normal", session) as c:
        cat = c.fetch_catalog(save=False)
    total = cat.total_duration
    # 2712 + "01:02:05"(3725) + 2500 + "45:30"(2730) + "50:00"(3000) + 3005 + 2890
    assert total == pytest.approx(2712 + 3725 + 2500 + 2730 + 3000 + 3005 + 2890)


def test_millisecond_timestamp_normalized(servers, session):
    with _client(servers, "normal", session) as c:
        cat = c.fetch_catalog(save=False)
    rec = cat.courses[0].resources[1]
    assert len(rec.record_time) == 19 and rec.record_time[4] == "-"


def test_mime_inferred_from_url(servers, session):
    with _client(servers, "normal", session) as c:
        cat = c.fetch_catalog(save=False)
    assert cat.courses[1].resources[0].mime == "application/vnd.apple.mpegurl"


def test_catalog_json_roundtrip(servers, session, tmp_path):
    with _client(servers, "normal", session) as c:
        cat = c.fetch_catalog(save=False)
    path = cat.save(tmp_path / "catalog.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert {"course_count", "resource_count", "total_duration_sec", "summary"} <= set(raw)
    loaded = Catalog.load(path)
    assert loaded.resource_count == cat.resource_count
    assert loaded.total_duration == pytest.approx(cat.total_duration)


def test_empty_catalog_is_valid(servers, session):
    """total=0 是合法结果，不能当成错误。"""
    with _client(servers, "empty", session) as c:
        cat = c.fetch_catalog(save=False)
    assert cat.resource_count == 0
    assert len(cat.courses) == 0


# --------------------------------------------------------------------------- #
# 鉴权失效必须显式
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["autherr", "http401", "redirect"])
def test_auth_failure_raises_explicitly(servers, session, mode):
    """三种登录失效形态都必须抛 AuthExpiredError，绝不静默返回空清单。"""
    with _client(servers, mode, session) as c:
        with pytest.raises(AuthExpiredError):
            c.fetch_catalog(save=False)


def test_api_changed_raises(servers, session):
    """响应结构异常 → ApiChangedError（显式接口变更检测）。"""
    with _client(servers, "weird", session) as c:
        with pytest.raises(ApiChangedError):
            c.fetch_catalog(save=False)


# --------------------------------------------------------------------------- #
# 候选路径探测
# --------------------------------------------------------------------------- #
def test_endpoint_probing_falls_back(servers, session):
    """第一个候选路径下线时，应自动切到下一个可用路径并缓存命中结果。"""
    with _client(servers, "slow", session) as c:
        courses = c.fetch_courses()
        cached = dict(c._endpoint_cache)
    assert len(courses) == 3
    assert cached, "应记录命中的接口路径"
    assert "jy-application-resourcemanage-ui" not in next(iter(cached.values()))


# --------------------------------------------------------------------------- #
# 播放地址
# --------------------------------------------------------------------------- #
def test_resolve_play_url_from_api(servers, session):
    with _client(servers, "normal", session) as c:
        res = Resource(resource_id="R-10031", title="第1讲 进程与线程", course_id="C-1003")
        url = c.resolve_play_url(res)
    assert "/R-10031/index.m3u8" in url, url
    assert "sign=" in url and "expires=" in url, "时效签名参数必须保留"
    assert res.duration_sec == 3005


def test_resolve_play_url_prefers_existing(servers, session):
    """清单里已带 play_url 时不应再打接口。"""
    with _client(servers, "normal", session) as c:
        res = Resource(resource_id="X", play_url="https://media.example.edu/hls/X/index.m3u8")
        assert c.resolve_play_url(res) == "https://media.example.edu/hls/X/index.m3u8"


def test_relative_play_url_absolutized(servers, session):
    with _client(servers, "normal", session) as c:
        res = Resource(resource_id="X", play_url="/vod/X/master.m3u8")
        url = c.resolve_play_url(res)
    assert url.startswith(servers["normal"][0])
    assert url.endswith("/vod/X/master.m3u8")


# --------------------------------------------------------------------------- #
# 脱敏记录
# --------------------------------------------------------------------------- #
def test_network_log_redacted(servers, session, tmp_path):
    """接口记录必须脱敏，且不能把假 Cookie 值写进去。

    注意：自动 dump 现在带护栏（模拟流量不写真实文件，见下面的缺陷 39 测试），
    所以这里用**显式路径**直接验证「写出来的内容是否脱敏」。
    """
    with _client(servers, "normal", session) as c:
        c.request("POST", f"{servers['normal'][0]}/api/course/list", json_body={"pageNum": 1})
        assert c.network_log, "请求应当被记录"
        target = c.dump_network_log(tmp_path / "network.jsonl")
    text = Path(target).read_text(encoding="utf-8")
    assert len(text) > 100, "应写出请求记录"
    assert "FAKE-SESSION-VALUE" not in text.upper(), "Cookie 值泄漏了"
    assert "course/list" in text


# --------------------------------------------------------------------------- #
# 缺陷 39：真实抓包文件被模拟流量污染
# --------------------------------------------------------------------------- #
def test_loopback_traffic_is_not_written_to_the_real_capture(
    servers, session, tmp_path, monkeypatch
):
    """默认路径 + 全是本机模拟流量 → **不写**真实抓包文件，但记录要留在内存里。

    背景：`recon/network.jsonl` 是「真实平台流量」产物（用于反推接口，见 docs/API.md）。
    而 `fetch_catalog()` 结尾无条件 `dump_network_log()`，于是每跑一次测试就往里灌
    20+ 条 `127.0.0.1` 记录 —— 实测累积到 644 条，把 5 条真实请求彻底埋掉。
    """
    from ecnu_transcribe import paths as real_paths

    target = tmp_path / "recon" / "network.jsonl"
    monkeypatch.setattr(real_paths, "recon_dir", lambda: tmp_path / "recon")
    with _client(servers, "normal", session) as c:
        c.fetch_catalog(save=False)
        assert not target.is_file(), "模拟平台流量不应写进真实抓包文件"
        # 但记录不能丢：调用方仍可用显式路径另存（scripts/mock_platform.py 就是这么做的）
        assert c.network_log, "被跳过的记录应保留在内存中"
        sidecar = tmp_path / "mock-traffic.jsonl"
        c.dump_network_log(sidecar)
    assert sidecar.is_file() and "course/list" in sidecar.read_text(encoding="utf-8")


def test_real_traffic_is_still_written(monkeypatch, tmp_path):
    """真实平台流量必须照写（护栏不能把功能一起砍掉）。"""
    from ecnu_transcribe import paths as real_paths
    from ecnu_transcribe.client import EcnuClient
    from ecnu_transcribe.config import AppConfig

    monkeypatch.setattr(real_paths, "recon_dir", lambda: tmp_path / "recon")
    c = EcnuClient(AppConfig(), session=SessionState())
    c.network_log.append({"url": "https://courses.ecnu.edu.cn/api/course/list", "status": 200})
    c.dump_network_log()
    written = (tmp_path / "recon" / "network.jsonl").read_text(encoding="utf-8")
    assert "courses.ecnu.edu.cn" in written


def test_explicit_path_bypasses_the_guard(monkeypatch, tmp_path):
    """显式给路径时照写 —— 测试与脚本靠这条做隔离。"""
    from ecnu_transcribe import paths as real_paths
    from ecnu_transcribe.client import EcnuClient
    from ecnu_transcribe.config import AppConfig

    monkeypatch.setattr(real_paths, "recon_dir", lambda: tmp_path / "never-used")
    c = EcnuClient(AppConfig(), session=SessionState())
    c.network_log.append({"url": "http://127.0.0.1:8000/api/course/list", "status": 200})
    explicit = tmp_path / "explicit.jsonl"
    c.dump_network_log(explicit)
    assert explicit.is_file() and "127.0.0.1" in explicit.read_text(encoding="utf-8")
    assert not (tmp_path / "never-used").exists()


def test_mixed_traffic_is_written(monkeypatch, tmp_path):
    """只要有一条第真实请求，就整体照写（不能因为夹杂模拟流量就丢真实数据）。"""
    from ecnu_transcribe import paths as real_paths
    from ecnu_transcribe.client import EcnuClient
    from ecnu_transcribe.config import AppConfig

    monkeypatch.setattr(real_paths, "recon_dir", lambda: tmp_path)
    c = EcnuClient(AppConfig(), session=SessionState())
    c.network_log.append({"url": "http://127.0.0.1:8000/api/course/list", "status": 200})
    c.network_log.append({"url": "https://courses.ecnu.edu.cn/api/resource/list", "status": 200})
    c.dump_network_log()
    text = (tmp_path / "network.jsonl").read_text(encoding="utf-8")
    assert "127.0.0.1" in text and "courses.ecnu.edu.cn" in text


def test_workspace_recon_is_untouched_by_the_suite():
    """套件级不变量：conftest 的隔离夹具让 `recon_dir()` 落在临时目录。

    没有这条，测试会悄悄往工作区的 `recon/`、`data/`、`cache/`、`output/` 写东西。
    """
    from ecnu_transcribe import paths

    assert paths.recon_dir() != paths.resource_root() / "recon", (
        "测试期间 recon_dir() 必须被隔离到临时目录，否则会污染真实抓包产物"
    )
    assert paths.app_root() != paths.resource_root()
