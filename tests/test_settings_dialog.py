"""设置对话框的**往返保真度**测试。

`SettingsDialog` 是写入全部设置与**两个 API Key** 的唯一入口，但此前零测试。
这里要防的是「静默丢设置 / 静默改设置」这类问题：

    * 界面 `_load()` 把配置填进控件 —— 填错（漏字段、类型转换错）用户会以为设置没保存；
    * 控件 `on_save()` 把值写回配置 —— 写错（漏字段、单位错、把 kbps 当 bps）
      会让转写参数悄悄变样，而用户看不出来；
    * 凭据必须落到 `ConfigManager.set_secret()`（DPAPI 加密），不能进 config.json。

做法：构造一份**全部字段都非默认**的配置 → 打开对话框 → 断言每个控件显示正确 →
改控件 → 保存 → 断言配置与凭据都正确落盘，且 config.json 里没有明文 Key。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from ecnu_transcribe.config import AppConfig, ConfigManager  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app


def non_default_config(tmp_path: Path) -> AppConfig:
    """一份**每个字段都偏离默认值**的配置（便于发现「漏字段」）。"""
    cfg = AppConfig()
    cfg.asr_provider = "openai_compatible"
    cfg.asr_base_url = "https://asr.example.com/v1"
    cfg.asr_model = "my-asr-model"
    cfg.asr_language = "ja"
    cfg.asr_use_native_api = True
    cfg.asr_timestamps = False
    cfg.asr_speaker_diarization = True
    cfg.asr_vocabulary = "拓扑排序、傅里叶"
    cfg.asr_max_upload_mb = 42.5
    cfg.asr_max_segment_sec = 321
    cfg.asr_overlap_sec = 2.5
    cfg.asr_chunk_strategy = "fixed"
    cfg.asr_retries = 7

    cfg.llm_enabled = True
    cfg.llm_base_url = "https://llm.example.com/v1"
    cfg.llm_model = "some-llm"
    cfg.llm_fix_text = False
    cfg.llm_resegment = False
    cfg.llm_summary = True
    cfg.llm_max_chars_per_call = 4321

    cfg.output_dir = str(tmp_path / "out-custom")
    cfg.emit_txt = False
    cfg.emit_srt = True
    cfg.emit_md = False
    cfg.emit_utf8_bom = False   # 非默认（默认 True）

    cfg.ffmpeg_path = r"C:\tools\ffmpeg.exe"
    cfg.concurrency = 1
    cfg.audio_format = "wav"
    cfg.audio_bitrate = "96k"
    cfg.audio_sample_rate = 44100
    cfg.audio_channels = 2
    cfg.cache_enabled = False
    cfg.write_audio_cache = True
    cfg.keep_video = True
    cfg.download_retries = 6
    cfg.speed_limit_kib = 512

    cfg.proxy = "http://127.0.0.1:7890"
    cfg.verify_tls = False
    cfg.request_timeout = 45.0
    cfg.jitter_min = 0.1
    cfg.jitter_max = 0.9
    cfg.portal_url = "https://portal.example.com/#/home"
    cfg.api_base = "https://api.example.com"
    cfg.student_id = "20261234567"
    return cfg


@pytest.fixture()
def env(tmp_path, qapp):
    from app.ui.settings_dialog import SettingsDialog

    cfg = non_default_config(tmp_path)
    cm = ConfigManager(config_file=tmp_path / "config.json", secrets_file=tmp_path / "secrets.json")
    cm.load()
    cm.save(cfg)
    cm.set_secret("asr_api_key", "sk-asr-SECRET-123456")
    cm.set_secret("llm_api_key", "sk-llm-SECRET-654321")

    dlg = SettingsDialog(cfg, cm)
    yield dlg, cfg, cm, tmp_path
    dlg.close()


# --------------------------------------------------------------------------- #
# 1. 加载：配置 → 控件
# --------------------------------------------------------------------------- #
def test_load_fills_every_control(env):
    dlg, cfg, _cm, _tmp = env

    # ASR
    assert dlg.cmb_provider.currentData() == "openai_compatible"
    assert dlg.edit_base.text() == "https://asr.example.com/v1"
    assert dlg.edit_model.text() == "my-asr-model"
    assert dlg.cmb_lang.currentData() == "ja"
    assert dlg.chk_native.isChecked() is True
    assert dlg.chk_timestamps.isChecked() is False
    assert dlg.chk_diarization.isChecked() is True
    assert dlg.edit_vocab.text() == "拓扑排序、傅里叶"
    assert dlg.spin_max_mb.value() == pytest.approx(42.5)
    assert dlg.spin_max_seg.value() == 321
    assert dlg.spin_overlap.value() == pytest.approx(2.5)
    assert dlg.cmb_chunk.currentData() == "fixed"
    assert dlg.spin_asr_retries.value() == 7
    # 凭据：从 DPAPI 解回来的明文要出现在输入框里
    assert dlg.edit_key.text() == "sk-asr-SECRET-123456"

    # LLM
    assert dlg.chk_llm.isChecked() is True
    assert dlg.edit_llm_base.text() == "https://llm.example.com/v1"
    assert dlg.edit_llm_model.text() == "some-llm"
    assert dlg.chk_fix.isChecked() is False
    assert dlg.chk_reseg.isChecked() is False
    assert dlg.chk_summary.isChecked() is True
    assert dlg.spin_llm_chars.value() == 4321
    assert dlg.edit_llm_key.text() == "sk-llm-SECRET-654321"

    # 输出
    assert dlg.edit_out.text() == cfg.output_dir
    assert dlg.chk_txt.isChecked() is False
    assert dlg.chk_srt.isChecked() is True
    assert dlg.chk_md.isChecked() is False
    assert dlg.chk_bom.isChecked() is False

    # 媒体
    assert dlg.edit_ffmpeg.text() == r"C:\tools\ffmpeg.exe"
    assert dlg.spin_concurrency.value() == 1
    assert dlg.cmb_fmt.currentData() == "wav"
    assert dlg.spin_bitrate.value() == 96                # "96k" → 96（不能是 96000）
    assert dlg.cmb_sr.currentData() == 44100
    assert dlg.cmb_ch.currentData() == 2
    assert dlg.chk_cache.isChecked() is False
    assert dlg.chk_keep_audio.isChecked() is True
    assert dlg.chk_keep_video.isChecked() is True
    assert dlg.spin_retries.value() == 6
    assert dlg.spin_speed.value() == 512

    # 网络
    assert dlg.edit_proxy.text() == "http://127.0.0.1:7890"
    assert dlg.chk_verify.isChecked() is False
    assert dlg.spin_timeout.value() == pytest.approx(45.0)
    assert dlg.spin_jitter_min.value() == pytest.approx(0.1)
    assert dlg.spin_jitter_max.value() == pytest.approx(0.9)
    assert dlg.edit_portal.text() == cfg.portal_url
    assert dlg.edit_api_base.text() == cfg.api_base
    assert dlg.edit_student.text() == "20261234567"


# --------------------------------------------------------------------------- #
# 2. 保存：控件 → 配置
# --------------------------------------------------------------------------- #
def test_save_writes_every_control_back(env):
    dlg, cfg, cm, tmp_path = env

    # 改一批值（覆盖字符串 / 数字 / 开关 / 下拉 / 凭据）
    dlg.edit_base.setText("https://changed.example.com/v1")
    dlg.edit_model.setText("changed-model")
    dlg._select_combo(dlg.cmb_lang, "en")
    dlg.chk_native.setChecked(False)
    dlg.chk_timestamps.setChecked(True)
    dlg.spin_max_seg.setValue(600)
    dlg.spin_overlap.setValue(1.0)
    dlg._select_combo(dlg.cmb_chunk, "silence")
    dlg.spin_asr_retries.setValue(3)

    dlg.chk_llm.setChecked(False)
    dlg.edit_llm_model.setText("changed-llm")
    dlg.spin_llm_chars.setValue(8000)

    new_out = str(tmp_path / "out-changed")
    dlg.edit_out.setText(new_out)
    dlg.chk_txt.setChecked(True)
    dlg.chk_md.setChecked(True)
    dlg.chk_bom.setChecked(True)

    dlg.spin_bitrate.setValue(128)
    dlg._select_combo(dlg.cmb_fmt, "m4a")
    dlg._select_combo(dlg.cmb_sr, 16000)
    dlg._select_combo(dlg.cmb_ch, 1)
    dlg.spin_concurrency.setValue(2)
    dlg.spin_speed.setValue(0)

    dlg.edit_proxy.setText("")
    dlg.chk_verify.setChecked(True)
    dlg.spin_timeout.setValue(60.0)
    dlg.spin_jitter_min.setValue(0.3)
    dlg.spin_jitter_max.setValue(0.8)   # >= min

    dlg.edit_key.setText("sk-asr-NEW-987654")
    dlg.edit_llm_key.setText("")        # 清空 LLM Key

    dlg.on_save()

    # 配置已落盘并可读回
    cm2 = ConfigManager(config_file=tmp_path / "config.json", secrets_file=tmp_path / "secrets.json")
    c2 = cm2.load()
    assert c2.asr_base_url == "https://changed.example.com/v1"
    assert c2.asr_model == "changed-model"
    assert c2.asr_language == "en"
    assert c2.asr_use_native_api is False
    assert c2.asr_timestamps is True
    assert c2.asr_max_segment_sec == 600
    assert c2.asr_overlap_sec == pytest.approx(1.0)
    assert c2.asr_chunk_strategy == "silence"
    assert c2.asr_retries == 3

    assert c2.llm_enabled is False
    assert c2.llm_model == "changed-llm"
    assert c2.llm_max_chars_per_call == 8000

    assert c2.output_dir == new_out
    assert c2.emit_txt is True and c2.emit_md is True
    assert c2.emit_utf8_bom is True

    assert c2.audio_bitrate == "128k"          # 数字 → "128k"
    assert c2.audio_format == "m4a"
    assert c2.audio_sample_rate == 16000
    assert c2.audio_channels == 1
    assert c2.concurrency == 2
    assert c2.speed_limit_kib == 0

    assert c2.proxy == ""
    assert c2.verify_tls is True
    assert c2.request_timeout == pytest.approx(60.0)
    assert c2.jitter_min == pytest.approx(0.3)
    assert c2.jitter_max == pytest.approx(0.8)

    # 凭据
    assert cm2.secret("asr_api_key") == "sk-asr-NEW-987654"
    assert cm2.secret("llm_api_key") == ""


def test_saved_config_has_no_plaintext_keys(env):
    """config.json 里绝不能出现明文 Key（它们只能进 DPAPI 加密的 secrets.json）。"""
    dlg, _cfg, _cm, tmp_path = env
    dlg.edit_key.setText("sk-PLAINTEXT-MUST-NOT-APPEAR-0001")
    dlg.edit_llm_key.setText("sk-ANOTHER-MUST-NOT-APPEAR-0002")
    dlg.on_save()

    config_text = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert "sk-PLAINTEXT-MUST-NOT-APPEAR-0001" not in config_text
    assert "sk-ANOTHER-MUST-NOT-APPEAR-0002" not in config_text
    payload = json.loads(config_text)
    assert not any("key" in k and "api" in k for k in payload), sorted(payload)

    secrets_file = tmp_path / "secrets.json"
    if secrets_file.is_file():
        secrets_text = secrets_file.read_text(encoding="utf-8")
        assert "sk-PLAINTEXT-MUST-NOT-APPEAR-0001" not in secrets_text
        assert "dpapi:" in secrets_text


def test_jitter_max_is_clamped_to_min(env):
    """抖动上限小于下限时要被纠正，避免下载时 sleep 到负区间。"""
    dlg, cfg, _cm, _tmp = env
    dlg.spin_jitter_min.setValue(2.0)
    dlg.spin_jitter_max.setValue(0.5)
    dlg.on_save()
    assert cfg.jitter_max >= cfg.jitter_min


def test_bitrate_roundtrip_is_stable(env):
    """码率往返多轮不能漂移（"64k" ↔ 64）。"""
    dlg, cfg, _cm, _tmp = env
    for value in (32, 64, 128, 192, 320):
        dlg.spin_bitrate.setValue(value)
        dlg.on_save()
        assert cfg.audio_bitrate == f"{value}k", cfg.audio_bitrate


def test_dialog_accepts_on_save(env):
    """on_save 必须 accept 对话框（否则调用方按 Cancel 处理，设置看起来「没保存」）。"""
    dlg, _cfg, _cm, _tmp = env
    dlg.on_save()
    assert dlg.result() == QDialog.Accepted, dlg.result()


# --------------------------------------------------------------------------- #
# 3. 预设选择
# --------------------------------------------------------------------------- #
def test_asr_presets_fill_base_and_model(env):
    """选预设要同时填 provider / base_url / model —— 少填一项用户就配不起来。"""
    dlg, _cfg, _cm, _tmp = env
    from app.ui.settings_dialog import ASR_PRESETS

    for index, (label, provider, base, model) in enumerate(ASR_PRESETS):
        # 先切到「自定义」逼出一次真正的 index 变化（QComboBox 不会重复发同一 index）
        dlg.cmb_preset.setCurrentIndex(len(ASR_PRESETS))
        dlg.cmb_preset.setCurrentIndex(index)
        assert dlg.cmb_provider.currentData() == provider, f"[{label}] provider 没跟上"
        if base:
            assert dlg.edit_base.text() == base, f"[{label}] base_url 没跟上"
        if model:
            assert dlg.edit_model.text() == model, f"[{label}] model 没跟上"
        assert dlg.chk_native.isChecked() is False, f"[{label}] 应关掉原生模式"


def test_preset_matches_config_by_triple_not_provider(env, tmp_path):
    """回归：预设下拉必须按 (provider, base_url, model) 三元组反查。

    预设 ①③④⑥ 的 provider 都是 ``openai_compatible``；只按 provider 反查会把
    「本地服务」显示成「OpenAI 官方」，用户以为自己配错了。
    """
    from app.ui.settings_dialog import ASR_PRESETS, CUSTOM_PRESET_LABEL, SettingsDialog

    def index_for(provider: str, base: str, model: str) -> int:
        cfg = AppConfig()
        cfg.asr_provider = provider
        cfg.asr_base_url = base
        cfg.asr_model = model
        cm = ConfigManager(config_file=tmp_path / "c.json", secrets_file=tmp_path / "s.json")
        cm.load()
        d = SettingsDialog(cfg, cm)
        idx = d.cmb_preset.currentIndex()
        d.close()
        return idx

    local_idx = index_for("openai_compatible", "http://127.0.0.1:8000/v1", "faster-whisper-small")
    assert ASR_PRESETS[local_idx][2] == "http://127.0.0.1:8000/v1", "应命中「本地」预设"

    openai_idx = index_for("openai_compatible", "https://api.openai.com/v1", "whisper-1")
    assert ASR_PRESETS[openai_idx][3] == "whisper-1", "应命中「OpenAI 官方」预设"
    assert openai_idx != local_idx, "两个同为 openai_compatible 的预设不能混淆"

    # 都不匹配 → 落到「（自定义 / 当前配置）」
    custom_idx = index_for("openai_compatible", "https://my-own.example.com/v1", "my-model")
    assert custom_idx == len(ASR_PRESETS), "不匹配任何预设时应选中「自定义」"
    assert "自定义" in CUSTOM_PRESET_LABEL


def test_custom_preset_selection_does_not_clobber_fields(env):
    """选中「（自定义）」时不得覆写用户已填的 base_url / model。"""
    dlg, _cfg, _cm, _tmp = env
    from app.ui.settings_dialog import ASR_PRESETS

    dlg.edit_base.setText("https://keep-me.example.com/v1")
    dlg.edit_model.setText("keep-model")
    dlg.cmb_preset.setCurrentIndex(len(ASR_PRESETS))     # 「（自定义）」
    assert dlg.edit_base.text() == "https://keep-me.example.com/v1"
    assert dlg.edit_model.text() == "keep-model"


def test_local_preset_points_at_localhost(env):
    """零成本预设必须指向 127.0.0.1，否则用户以为配好了却连不上。"""
    dlg, _cfg, _cm, _tmp = env
    from app.ui.settings_dialog import ASR_PRESETS

    local = next(p for p in ASR_PRESETS if "本地" in p[0] and "127.0.0.1" in p[2])
    assert "127.0.0.1" in local[2]
    assert local[1] == "openai_compatible", local


def test_dashscope_preset_defaults_match_docs(env):
    """DashScope 预设的地址/模型要与 README 里写的一致（否则文档骗人）。"""
    dlg, _cfg, _cm, _tmp = env
    from app.ui.settings_dialog import ASR_PRESETS

    ds = next(p for p in ASR_PRESETS if p[1] == "dashscope")
    assert ds[2] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert ds[3] == "qwen3-asr-flash", f"DashScope 预设的模型名应为实测可用的千问 ASR：{ds[3]}"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert ds[2] in readme, "README 里应写明同一个 Base URL"
    assert ds[3] in readme, "README 里应写明同一个模型名"
