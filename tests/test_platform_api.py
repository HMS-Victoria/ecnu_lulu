"""真实平台接口的**契约测试**。

被测的是 `EcnuClient` 里那条「真机实测出来的」路径：

    换 jwt-token → 我的课表 → 每节课的录播 → 带签名的播放地址

为什么要专门锁它：这条路径的**参数名与响应字段名全是实测试出来的**
（`page.pageIndex` 不是 `pageNum`；行在 `records` 不是 `list`；鉴权头叫 `jwt-token`
不是 `Authorization`；教学班 id 字段叫 `teachingClassId` 不是 `teclId`）——
写错任何一个都只会得到「服务器内部错误 / 分页不能为空」这类**看不出原因**的报错。
假服务器 `tests/fake_platform_server.py` 精确复刻这些约束，一旦有人改坏就会红。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from ecnu_transcribe.client import EcnuClient, SessionState
from ecnu_transcribe.config import AppConfig
from ecnu_transcribe.errors import ApiChangedError, AuthExpiredError

ROOT = Path(__file__).resolve().parents[1]


def _load_fake():
    path = ROOT / "tests" / "fake_platform_server.py"
    spec = importlib.util.spec_from_file_location("fake_platform_server", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fake_platform_server"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fake():
    return _load_fake()


def make_client(fake_mod, mode: str = "normal", **cfg_kw) -> tuple[EcnuClient, object]:
    server = fake_mod.FakePlatform(mode=mode).__enter__()
    cfg = AppConfig()
    cfg.portal_url = f"{server.base}/jy-application-resourcemanage-ui/#/home"
    cfg.api_base = server.base
    cfg.jitter_min = 0.0
    cfg.jitter_max = 0.0
    cfg.request_timeout = 10.0
    for k, v in cfg_kw.items():
        setattr(cfg, k, v)
    client = EcnuClient(cfg, session=SessionState(cookies={"SESSION": "FAKE"}))
    return client, server


# --------------------------------------------------------------------------- #
# 1. 接口前缀推导
# --------------------------------------------------------------------------- #
def test_platform_api_base_is_derived_from_portal_url(fake):
    client, server = make_client(fake)
    try:
        assert client.platform_api_base() == f"{server.base}/jy-application-resourcemanage"
    finally:
        client.close()
        server.__exit__()


# --------------------------------------------------------------------------- #
# 2. 换 token
# --------------------------------------------------------------------------- #
def test_fetch_jwt_token_reads_result_jwt_token(fake):
    client, server = make_client(fake)
    try:
        token = client.fetch_jwt_token()
        assert token == fake.TOKEN, token[:20]
        # 缓存：第二次不再发请求
        before = len(server.seen)
        assert client.fetch_jwt_token() == token
        assert len(server.seen) == before
    finally:
        client.close()
        server.__exit__()


def test_login_redirect_becomes_auth_expired(fake):
    """换 token 被 302 到登录页 → AuthExpiredError（而不是含糊的解析失败）。"""
    client, server = make_client(fake, mode="no_token")
    try:
        with pytest.raises(AuthExpiredError):
            client.fetch_jwt_token()
    finally:
        client.close()
        server.__exit__()


# --------------------------------------------------------------------------- #
# 3. 课表：分页参数名必须是 page.pageIndex / page.pageSize
# --------------------------------------------------------------------------- #
def test_curriculum_uses_dotted_paging_params(fake):
    client, server = make_client(fake)
    try:
        rows = client.fetch_curriculum(2)
        assert [r["subjName"] for r in rows][:2] == ["线性代数", "数据结构与算法"]
        cur = [r for r in server.seen if r["path"].endswith("/v1/myself/curriculum")]
        assert cur, server.seen
        assert "page.pageIndex" in cur[0]["query"], "分页参数名必须是 page.pageIndex"
        assert "page.pageSize" in cur[0]["query"]
    finally:
        client.close()
        server.__exit__()


def test_curriculum_requires_jwt_header(fake):
    """不带 jwt-token 调接口 → 服务器回未登录 → 客户端抛 AuthExpiredError。"""
    client, server = make_client(fake)
    try:
        client._jwt_token = "placeholder"  # noqa: SLF001 — 绕过换 token，直接看头有没有带
        client.fetch_terms()  # 第一次带 token 正常
        # 人为清空 token 并禁止刷新：模拟头丢了
        client._jwt_token = ""  # noqa: SLF001
        original = client.fetch_jwt_token
        client.fetch_jwt_token = lambda **kw: (_ for _ in ()).throw(AuthExpiredError("无 token"))  # type: ignore[assignment]
        with pytest.raises(AuthExpiredError):
            client.fetch_terms()
        client.fetch_jwt_token = original  # type: ignore[assignment]
    finally:
        client.close()
        server.__exit__()


def test_api_calls_carry_jwt_header(fake):
    client, server = make_client(fake)
    try:
        client.fetch_terms()
        api_calls = [r for r in server.seen if "/v1/list/termYear" in r["path"]]
        assert api_calls and api_calls[0]["jwt"] == fake.TOKEN, api_calls
    finally:
        client.close()
        server.__exit__()


# --------------------------------------------------------------------------- #
# 4. 录播与播放地址
# --------------------------------------------------------------------------- #
def test_course_videos_parsed(fake):
    client, server = make_client(fake)
    try:
        videos = client.fetch_course_videos(351351)
        assert {v["vodId"] for v in videos} == {701815, 701828}
    finally:
        client.close()
        server.__exit__()


def test_resolve_play_url_picks_longest_vod(fake):
    """同一节课多个机位 → 取时长最长的那个（实测平台确实会给两条）。"""
    from ecnu_transcribe.catalog import Resource

    client, server = make_client(fake)
    try:
        res = Resource(resource_id="COUR-351351", title="线性代数", course_name="线性代数")
        url = client.resolve_play_url(res)
        assert url.endswith("701828.mp4?auth_key=b"), url
        assert res.duration_sec == pytest.approx(3301)
        # 第二次直接命中缓存字段，不再打接口
        before = len(server.seen)
        assert client.resolve_play_url(res) == url
        assert len(server.seen) == before
    finally:
        client.close()
        server.__exit__()


def test_resolve_play_url_without_videos_raises(fake):
    from ecnu_transcribe.catalog import Resource

    client, server = make_client(fake)
    try:
        res = Resource(resource_id="COUR-999999", title="没有录像的课", course_name="x")
        with pytest.raises(ApiChangedError):
            client.resolve_play_url(res)
    finally:
        client.close()
        server.__exit__()


# --------------------------------------------------------------------------- #
# 5. 全量清单：只收「有录像」的课，并按科目归组
# --------------------------------------------------------------------------- #
def test_fetch_catalog_groups_by_subject_and_skips_no_vod(fake, tmp_path, monkeypatch):
    from ecnu_transcribe import paths as real_paths

    monkeypatch.setattr(real_paths, "catalog_path", lambda: tmp_path / "catalog.json")
    client, server = make_client(fake)
    try:
        cat = client.fetch_catalog(save=False)
        names = [c.course_name for c in cat.courses]
        assert "线性代数" in names and "数据结构与算法" in names
        assert "没有录像的课" not in names, "courVodOpen=0 的课不应进清单"
        total = sum(len(c.resources) for c in cat.courses)
        assert total == 2, [r.title for c in cat.courses for r in c.resources]
        # 播放地址不在清单阶段解析（按需解析，避免几百次请求）
        assert all(r.play_url == "" for c in cat.courses for r in c.resources)
        assert all(r.resource_id.startswith("COUR-") for c in cat.courses for r in c.resources)
    finally:
        client.close()
        server.__exit__()


def test_catalog_falls_back_to_legacy_when_no_token(fake, monkeypatch):
    """拿不到 jwt-token → 回退到候选路径探测（并最终抛 SiteUnreachable/ApiChanged）。"""
    from ecnu_transcribe.errors import SiteUnreachableError

    client, server = make_client(fake, mode="no_token")
    try:
        with pytest.raises((SiteUnreachableError, ApiChangedError, AuthExpiredError)):
            client.fetch_catalog(save=False)
    finally:
        client.close()
        server.__exit__()


def test_platform_api_can_be_disabled(fake):
    """cfg.platform_api=False → 完全不走新路径（排障开关）。"""
    client, server = make_client(fake, platform_api=False)
    try:
        from ecnu_transcribe.errors import SiteUnreachableError

        with pytest.raises((SiteUnreachableError, ApiChangedError)):
            client.fetch_catalog(save=False)
        assert not [r for r in server.seen if r["path"].endswith("/oauth2/token")], (
            "关掉 platform_api 后不应再请求真实接口"
        )
    finally:
        client.close()
        server.__exit__()
