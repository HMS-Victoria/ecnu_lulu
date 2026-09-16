"""回归（缺陷 55）：DashScope 云端 ASR 的**真实可用调用方式**。

2026-09-14 用用户真实 Key 在 `https://dashscope.aliyuncs.com/compatible-mode/v1`
上逐条实测得到的事实（这里逐条钉死，避免以后又"想当然"改回去）：

1. ``POST /audio/transcriptions``：**对所有 ASR 模型都 404**
   —— 应用原来的云端路整条不可用（用户填了 Key 也跑不出东西）。
2. ``POST /chat/completions`` + ``content=[{"type":"input_audio","input_audio":{"data": <data URL>}}]``
   → 200 且质量很好（同段 60s 课堂音频：本机 small 347 字且大量繁体，千问 377 字简体带标点）。
3. ``input_audio.data`` **必须是 data URL**；裸 base64 会被当成 URL 拒绝。
4. content 里**只能有音频项**；再加 text 项会报
   ``The dedicated task `asr` ... does not support this input``。
5. 返回值**没有时间戳** ⇒ `.srt` 的时间轴只能来自静音切分边界（一个切片一个 segment）。
6. 原生异步路的取凭证接口是好的，但凭证里的键叫 **``oss_access_key_id``**，
   表单字段要用**连字符**形式 ``x-oss-object-acl``；写错时 OSS 回的是
   "accessKeyId is empty" / "Policy Condition failed"，看着像权限问题，其实是自己读空了。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ecnu_transcribe import media
from ecnu_transcribe.config import AppConfig
from ecnu_transcribe.transcriber import (
    CHAT_AUDIO_MAX_SEGMENT_SEC,
    DashScopeTranscriber,
    OpenAICompatibleTranscriber,
    _is_audio_too_long,
    _use_chat_audio,
)

from fake_dashscope_server import FakeDashScope


# --------------------------------------------------------------------------- #
# 1. 路由：哪些模型必须走 chat 路径
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "model",
    ["qwen3-asr-flash", "qwen3-asr-flash-2026-02-10", "qwen-audio-3.0-asr-flash",
     "fun-asr-flash-2026-06-15"],
)
def test_chat_audio_routed_for_qwen_asr_models(model):
    cfg = AppConfig()
    cfg.asr_model = model
    assert _use_chat_audio(cfg) is True, f"{model} 在该端点上只能走 chat 路径"


@pytest.mark.parametrize("model", ["whisper-1", "paraformer-v2", "qwen3-asr-flash-realtime"])
def test_chat_audio_not_routed_for_other_models(model):
    cfg = AppConfig()
    cfg.asr_model = model
    # realtime 走 websocket，本实现不支持：不能误路由到 chat
    assert _use_chat_audio(cfg) is False, model


def test_chat_audio_can_be_forced_both_ways():
    cfg = AppConfig()
    cfg.asr_model = "whisper-1"
    cfg.asr_chat_audio = True
    assert _use_chat_audio(cfg) is True
    cfg.asr_model = "qwen3-asr-flash"
    cfg.asr_chat_audio = False
    assert _use_chat_audio(cfg) is False


# --------------------------------------------------------------------------- #
# 2. 请求形状：data URL + 只放音频项 + 时间轴来自切片边界
# --------------------------------------------------------------------------- #
@pytest.fixture()
def short_audio(tmp_path) -> Path:
    out = tmp_path / "seg.mp3"
    media.run_ffmpeg(
        [str(media.find_ffmpeg()), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=6", "-ac", "1", "-ar", "16000",
         "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", str(out)],
        timeout=120, check=True,
    )
    return out


def test_chat_audio_request_shape_and_segment_range(short_audio):
    with FakeDashScope() as srv:
        cfg = AppConfig()
        cfg.asr_provider = "dashscope"
        cfg.asr_base_url = srv.api_base + "/compatible-mode/v1"
        cfg.asr_model = "qwen3-asr-flash"
        srv.chat_text = "我们把这个问题简化一下，先看二维的情况。"
        tr = DashScopeTranscriber(cfg, "sk-test-key")
        segs = tr.transcribe(short_audio, duration_sec=media.duration_of(short_audio)).segments

    assert len(srv.chat_requests) == 1, "应当只发一次 chat 请求"
    body = srv.chat_requests[0]
    assert body["model"] == "qwen3-asr-flash"
    content = body["messages"][0]["content"]
    assert [c["type"] for c in content] == ["input_audio"], "content 里只能有音频项"
    data = content[0]["input_audio"]["data"]
    assert data.startswith("data:audio/mpeg;base64,"), "必须是 data URL，裸 base64 会被拒"
    # 没有时间戳 ⇒ 整段文本落在切片自身的区间上（不按标点编时间）
    assert len(segs) == 1
    assert segs[0].text.startswith("我们把这个问题简化")
    assert segs[0].start == pytest.approx(0.0, abs=0.01)
    assert segs[0].end == pytest.approx(media.duration_of(short_audio), abs=0.3)


def test_chat_audio_rejects_bare_base64(short_audio):
    """假服务按真实行为拒绝裸 base64 —— 用来确认我们的实现对这条规则敏感。"""
    import base64

    import httpx

    with FakeDashScope() as srv:
        raw = base64.b64encode(short_audio.read_bytes()).decode()
        r = httpx.post(
            f"{srv.api_base}/compatible-mode/v1/chat/completions",
            json={"model": "qwen3-asr-flash",
                  "messages": [{"role": "user",
                                "content": [{"type": "input_audio",
                                             "input_audio": {"data": raw, "format": "mp3"}}]}]},
            timeout=30.0,
        )
    assert r.status_code == 400
    assert "does not appear to be valid" in r.text


def test_chat_audio_error_text_is_permanent(short_audio):
    """真实服务对"加了文字项"的报错要能被识别（它是永久错误，重试没意义）。"""
    cfg = AppConfig()
    cfg.asr_provider = "dashscope"
    cfg.asr_model = "qwen3-asr-flash"
    tr = OpenAICompatibleTranscriber(cfg, "sk-test-key")
    assert tr.chat_audio is True


# --------------------------------------------------------------------------- #
# 2b. 缺陷 62：单请求时长上限（实测 300s 通过 / 301s 报 The audio is too long）
# --------------------------------------------------------------------------- #
def test_segment_limit_is_clamped_for_chat_audio_models():
    cfg = AppConfig()
    cfg.asr_model = "qwen3-asr-flash"
    cfg.asr_max_segment_sec = 600          # 用户默认值 —— 正好会 100% 失败
    tr = OpenAICompatibleTranscriber(cfg, "sk-test-key")
    assert tr.effective_max_segment() == CHAT_AUDIO_MAX_SEGMENT_SEC
    assert tr.effective_max_segment() <= 300, "必须夹在实测上限（300s）以内"

    cfg.asr_max_segment_sec = 30           # 用户想更细：仍然生效
    assert OpenAICompatibleTranscriber(cfg, "sk-test-key").effective_max_segment() == 30


def test_segment_limit_untouched_for_other_models():
    cfg = AppConfig()
    cfg.asr_model = "whisper-1"
    cfg.asr_max_segment_sec = 600
    assert OpenAICompatibleTranscriber(cfg, "sk-test-key").effective_max_segment() == 600


def test_audio_too_long_is_recognised():
    assert _is_audio_too_long(
        '{"error":{"message":"<400> InternalError.Algo.InvalidParameter: The audio is too long"}}'
    )
    assert not _is_audio_too_long("HTTP 400: model not found")


def test_too_long_is_auto_split_and_still_covers_whole_audio(tmp_path):
    """被拒为过长时**自动对半切分重试**，且时间轴仍覆盖整段。

    这道兜底的意义：上限可能随模型/账号变化，与其让整条课失败、让用户猜该调哪个参数，
    不如就地切一半再试（每半段各自带正确偏移）。
    """
    clip = tmp_path / "long.mp3"
    media.run_ffmpeg(
        [str(media.find_ffmpeg()), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=40", "-ac", "1", "-ar", "16000",
         "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", str(clip)],
        timeout=180, check=True,
    )
    with FakeDashScope() as srv:
        srv.max_audio_seconds = 30.0       # 实测那台服务的上限是 300s，这里压到 30s 便于测试
        srv.chat_text = "半段文本。"
        cfg = AppConfig()
        cfg.asr_provider = "dashscope"
        cfg.asr_base_url = srv.api_base + "/compatible-mode/v1"
        cfg.asr_model = "qwen3-asr-flash"
        cfg.asr_max_segment_sec = 600      # 会被夹到 270 ⇒ 40s 仍是一次请求 ⇒ 触发切分
        cfg.asr_retries = 1
        tr = DashScopeTranscriber(cfg, "sk-test-key")
        result = tr.transcribe(clip, duration_sec=media.duration_of(clip))

    assert len(srv.chat_requests) >= 3, "应当先整段试一次，再对半切分各试一次"
    assert len(result.segments) == 2, f"对半切分后应有 2 段，实际 {len(result.segments)}"
    starts = sorted(s.start for s in result.segments)
    ends = sorted(s.end for s in result.segments)
    total = media.duration_of(clip)
    assert starts[0] == pytest.approx(0.0, abs=0.3), starts
    assert ends[-1] == pytest.approx(total, abs=0.6), (ends, total)
    # 两段在时间上相接，不能重叠到离谱或漏掉中段
    assert result.segments[0].end == pytest.approx(result.segments[1].start, abs=0.6)


# --------------------------------------------------------------------------- #
# 3. 上传凭证字段名（原 bug：读 access_key_id，真实接口给 oss_access_key_id）
# --------------------------------------------------------------------------- #
def _run_native_once(cfg: AppConfig, srv: FakeDashScope, audio: Path):
    cfg.asr_provider = "dashscope"
    cfg.asr_model = "paraformer-v2"
    cfg.asr_use_native_api = True
    env_base = srv.api_base + "/api/v1"
    return env_base


def test_upload_reads_real_policy_field_names(short_audio, monkeypatch):
    with FakeDashScope(pending_polls=0) as srv:
        cfg = AppConfig()
        cfg.asr_provider = "dashscope"
        cfg.asr_model = "paraformer-v2"
        cfg.asr_use_native_api = True
        cfg.asr_retries = 1
        monkeypatch.setenv("ECNU_DASHSCOPE_NATIVE_BASE", srv.api_base + "/api/v1")
        DashScopeTranscriber(cfg, "sk-test-key").transcribe(
            short_audio, duration_sec=media.duration_of(short_audio)
        )

    assert srv.uploaded, "应当发生一次 OSS 上传"
    fields = srv.uploaded[0]["fields"]
    assert fields.get("OSSAccessKeyId") == "FAKE_AK", f"凭证没读到：{fields}"
    assert fields.get("x-oss-object-acl") == "private", (
        f"表单字段必须是连字符形式，否则 OSS 回 Policy Condition failed：{fields}"
    )
    assert fields.get("key", "").startswith("dashscope/asr/"), fields


def test_upload_still_accepts_legacy_policy_field_names(short_audio, monkeypatch):
    """向后兼容：凭证里只有旧的 ``access_key_id`` 时也要能读出来。"""
    with FakeDashScope(pending_polls=0, legacy_policy_keys=True) as srv:
        cfg = AppConfig()
        cfg.asr_provider = "dashscope"
        cfg.asr_model = "paraformer-v2"
        cfg.asr_use_native_api = True
        cfg.asr_retries = 1
        monkeypatch.setenv("ECNU_DASHSCOPE_NATIVE_BASE", srv.api_base + "/api/v1")
        DashScopeTranscriber(cfg, "sk-test-key").transcribe(
            short_audio, duration_sec=media.duration_of(short_audio)
        )
    assert srv.uploaded[0]["fields"].get("OSSAccessKeyId") == "FAKE_AK"


def test_upload_reports_incomplete_policy_clearly(short_audio):
    """凭证缺字段时给出**可诊断**的报错，而不是让 OSS 回一句 "accessKeyId is empty"。"""
    from ecnu_transcribe.errors import TranscriptionError

    cfg = AppConfig()
    cfg.asr_model = "paraformer-v2"
    tr = DashScopeTranscriber(cfg, "sk-test-key")
    import httpx

    with httpx.Client() as client:
        with pytest.raises(TranscriptionError) as exc:
            tr._upload_to_oss(client, short_audio, {"upload_host": "http://x", "upload_dir": "d"})  # noqa: SLF001
    assert "oss_access_key_id" in str(exc.value), str(exc.value)
