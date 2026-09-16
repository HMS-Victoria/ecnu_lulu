"""DashScope 原生异步 ASR 链路测试（`DashScopeTranscriber._transcribe_native`）。

这是 GUI 里「大文件/长音频更稳」那个开关对应的实现，也是**长课程推荐模式**，
但此前完全没有测试覆盖。整条链路有 5 个容易理解错的环节：

    1. ``GET /api/v1/uploads?action=getPolicy`` 拿上传凭证（字段名：upload_host /
       upload_dir / access_key_id / policy / signature）
    2. 用凭证 ``POST`` 到 OSS（multipart，含 OSSAccessKeyId/policy/Signature/key/file）
    3. ``POST /api/v1/services/audio/asr/transcription`` 提交任务，**必须带
       ``X-DashScope-Async: enable``**，返回 ``output.task_id``
    4. 轮询 ``GET /api/v1/tasks/{id}``，状态机 RUNNING → SUCCEEDED / FAILED
    5. ``GET {transcription_url}`` 取结果，解析 ``transcripts[].sentences[]``
       （时间戳单位是**毫秒**）

任何一处理解错都会让「大文件模式」整体不可用。用 `tests/fake_dashscope_server.py`
在完全离线的情况下把这 5 步全部验证一遍。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from ecnu_transcribe.config import AppConfig
from ecnu_transcribe.errors import AuthExpiredError, TranscriptionError
from ecnu_transcribe.transcriber import DashScopeTranscriber

ROOT = Path(__file__).resolve().parents[1]


def _load_fake():
    path = ROOT / "tests" / "fake_dashscope_server.py"
    spec = importlib.util.spec_from_file_location("fake_dashscope_server", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fake_dashscope_server"] = mod
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


FakeDashScope = _load_fake().FakeDashScope


@pytest.fixture()
def audio(tmp_path) -> Path:
    """一段最简音频（原生链路走上传，不需要真实语音）。"""
    from ecnu_transcribe import media

    out = tmp_path / "tone.wav"
    media.run_ffmpeg(
        [str(media.find_ffmpeg()), "-hide_banner", "-nostdin", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)],
        timeout=60, check=True,
    )
    return out


@pytest.fixture()
def native_base(monkeypatch):
    """把原生 base 指向假服务（用完自动清掉，避免污染其它测试）。"""
    created: list[str] = []

    def _use(url: str) -> None:
        monkeypatch.setenv("ECNU_DASHSCOPE_NATIVE_BASE", url)
        created.append(url)

    yield _use


def cfg_for(**kw) -> AppConfig:
    cfg = AppConfig()
    cfg.asr_provider = "dashscope"
    cfg.asr_model = "paraformer-v2"
    cfg.asr_use_native_api = True
    cfg.asr_language = "zh"
    cfg.asr_timestamps = True
    cfg.request_timeout = 20.0
    cfg.proxy = ""
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


# --------------------------------------------------------------------------- #
# 1. 完整成功链路
# --------------------------------------------------------------------------- #
def test_native_full_flow_succeeds(audio, native_base):
    with FakeDashScope(pending_polls=2) as srv:
        native_base(srv.api_base)
        tr = DashScopeTranscriber(cfg_for(), "sk-dashscope-123456")
        t = tr.transcribe(audio)

    assert t.provider == "dashscope-native"
    assert t.model == "paraformer-v2"
    assert [s.text for s in t.segments] == ["第一句话。", "第二句话。", "第三句话。"]
    assert srv.uploaded, "应完成 OSS 上传"
    assert srv.submitted, "应提交异步任务"
    assert srv.poll_count >= 2, f"应轮询到成功，实际 {srv.poll_count} 次"
    assert srv.result_fetches == 1, "应取一次结果"


def test_native_uses_configurable_base_env(audio, native_base, monkeypatch):
    """原生 base 必须可配置（私有化部署 / 离线测试都需要）。"""
    with FakeDashScope(pending_polls=0) as srv:
        native_base(srv.api_base)
        assert DashScopeTranscriber(cfg_for(), "sk-x-123456").NATIVE_BASE == srv.api_base
    # 环境变量清掉后回到官方默认地址
    monkeypatch.delenv("ECNU_DASHSCOPE_NATIVE_BASE", raising=False)
    assert DashScopeTranscriber(cfg_for(), "sk-x-123456").NATIVE_BASE.startswith(
        "https://dashscope.aliyuncs.com"
    )


def test_native_upload_carries_oss_credentials(audio, native_base):
    """上传必须带上 getPolicy 返回的全部凭证字段（少一个 OSS 就 403）。"""
    with FakeDashScope(pending_polls=0) as srv:
        native_base(srv.api_base)
        DashScopeTranscriber(cfg_for(), "sk-dashscope-123456").transcribe(audio)

    fields = srv.uploaded[0]["fields"]
    for key in ("OSSAccessKeyId", "policy", "Signature", "key"):
        assert fields.get(key), f"上传缺少 {key}：{fields}"
    assert fields["OSSAccessKeyId"] == "FAKE_AK"
    assert fields["key"].startswith("dashscope/asr/"), fields["key"]
    assert srv.uploaded[0]["file_bytes"] > 1000, "应真的上传了音频内容"


def test_native_submit_payload_and_async_header(audio, native_base):
    """提交任务：必须带 X-DashScope-Async 头，且参数按 DashScope 约定组织。"""
    with FakeDashScope(pending_polls=0) as srv:
        native_base(srv.api_base)
        DashScopeTranscriber(
            cfg_for(asr_vocabulary="vocab-123", asr_speaker_diarization=True), "sk-dashscope-123456"
        ).transcribe(audio)

    req = srv.submitted[0]
    assert req["headers"].get("x-dashscope-async") == "enable", req["headers"]
    assert "bearer sk-dashscope-123456" in req["headers"].get("authorization", "").lower()
    body = req["payload"]
    assert body["model"] == "paraformer-v2"
    assert body["input"]["file_urls"] and body["input"]["file_urls"][0].startswith("oss://")
    params = body["parameters"]
    assert params["language_hints"] == ["zh"]
    assert params["enable_timestamp"] is True
    assert params["vocabulary_id"] == "vocab-123"
    assert params["diarization_enabled"] is True


def test_native_result_parsing_millisecond_timestamps(audio, native_base):
    """结果里的 begin_time/end_time 单位是**毫秒**，必须换算成秒。"""
    with FakeDashScope(pending_polls=0, sentences=["甲", "乙"]) as srv:
        native_base(srv.api_base)
        t = DashScopeTranscriber(cfg_for(), "sk-x-123456").transcribe(audio)

    assert len(t.segments) == 2
    assert t.segments[0].start == pytest.approx(0.0)
    assert t.segments[0].end == pytest.approx(2.0)
    assert t.segments[1].start == pytest.approx(2.0)
    assert t.segments[1].end == pytest.approx(4.0)


def test_native_returns_text_when_sentences_missing(audio, native_base):
    """句级结果缺失时退化为整段文本（不能返回空转写）。

    回归：原来只在 ``sentences`` 为**真值**时解析句级结果，空列表会落到 else 分支
    去取 ``transcripts[].text``；但如果接口把 text 放在别处（或为空），
    就会整体返回空转写 —— 用户看到的是「ASR 返回了空结果」这种**误导性报错**，
    其实识别是成功的。现在只要句级没产出内容就一定回退到整段文本。
    """
    with FakeDashScope(pending_polls=0) as srv:
        native_base(srv.api_base)
        srv.omit_sentences = True
        t = DashScopeTranscriber(cfg_for(), "sk-x-123456").transcribe(audio)

    assert t.segments, "应至少产出一个片段"
    assert t.text.strip() == srv.plain_text.strip(), t.text
    assert t.segments[0].text == srv.plain_text


def test_native_sentences_with_empty_texts_fall_back(audio, native_base):
    """句级条目的 text 全是空的 → 同样要回退，而不是产出空转写。"""
    with FakeDashScope(pending_polls=0) as srv:
        native_base(srv.api_base)
        srv.sentences = ["", "   "]
        srv.plain_text = "兜底文本"
        t = DashScopeTranscriber(cfg_for(), "sk-x-123456").transcribe(audio)
    assert t.text.strip() == "兜底文本", t.text


# --------------------------------------------------------------------------- #
# 2. 失败路径必须显式、可诊断
# --------------------------------------------------------------------------- #
def test_native_auth_error_is_explicit(audio, native_base):
    with FakeDashScope(auth_error=401) as srv:
        native_base(srv.api_base)
        with pytest.raises(AuthExpiredError) as exc:
            DashScopeTranscriber(cfg_for(), "sk-bad-123456").transcribe(audio)
    assert "DashScope" in str(exc.value)


def test_native_empty_policy_is_reported(audio, native_base):
    """getPolicy 返回空 data → 明确报错，而不是拿空凭证去上传。"""
    with FakeDashScope(fail_upload_policy=True) as srv:
        native_base(srv.api_base)
        with pytest.raises(TranscriptionError) as exc:
            DashScopeTranscriber(cfg_for(), "sk-x-123456").transcribe(audio)
    assert "上传凭证" in str(exc.value)


def test_native_missing_task_id_is_reported(audio, native_base):
    with FakeDashScope(omit_task_id=True) as srv:
        native_base(srv.api_base)
        with pytest.raises(TranscriptionError) as exc:
            DashScopeTranscriber(cfg_for(), "sk-x-123456").transcribe(audio)
    assert "task_id" in str(exc.value)


def test_native_submit_http_error_is_reported(audio, native_base):
    with FakeDashScope(fail_submit=True) as srv:
        native_base(srv.api_base)
        with pytest.raises(TranscriptionError) as exc:
            DashScopeTranscriber(cfg_for(), "sk-x-123456").transcribe(audio)
    assert "提交失败" in str(exc.value)


def test_native_task_failed_status_gives_actionable_hint(audio, native_base):
    """任务 FAILED → 报错里要带状态与可操作建议。"""
    with FakeDashScope(pending_polls=0, final_status="FAILED") as srv:
        native_base(srv.api_base)
        with pytest.raises(TranscriptionError) as exc:
            DashScopeTranscriber(cfg_for(), "sk-x-123456").transcribe(audio)
    msg = str(exc.value)
    assert "FAILED" in msg
    assert "切分" in msg or "OpenAI 兼容" in msg, msg


def test_native_api_key_required():
    from ecnu_transcribe.errors import AsrNotConfiguredError

    with pytest.raises(AsrNotConfiguredError):
        DashScopeTranscriber(cfg_for(), "")


# --------------------------------------------------------------------------- #
# 3. 兼容模式与非原生模式的分派
# --------------------------------------------------------------------------- #
def test_non_native_mode_uses_openai_compatible(audio, native_base, monkeypatch):
    """``asr_use_native_api=False`` 时应走 OpenAI 兼容端点，不碰原生接口。"""
    with FakeDashScope(pending_polls=0) as srv:
        native_base(srv.api_base)
        cfg = cfg_for(asr_use_native_api=False)
        cfg.asr_base_url = f"{srv.api_base}/compatible-mode/v1"   # 假服务没有这个路由
        tr = DashScopeTranscriber(cfg, "sk-x-123456")
        assert tr.name == "dashscope"
        with pytest.raises(Exception):
            tr.transcribe(audio)
        # 关键断言：没有走原生上传/提交
        assert not srv.uploaded, "非原生模式不该调用 uploads"
        assert not srv.submitted, "非原生模式不该提交异步任务"


def test_transcriber_name_reflects_mode():
    assert DashScopeTranscriber(cfg_for(asr_use_native_api=True), "sk-x-123456").name == "dashscope-native"
    assert DashScopeTranscriber(cfg_for(asr_use_native_api=False), "sk-x-123456").name == "dashscope"
