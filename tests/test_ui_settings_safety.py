"""设置表单重排后的配置兼容性和连接检查取消语义。"""
import copy
from dataclasses import asdict

from PySide6.QtWidgets import QDialog, QToolButton

from test_settings_dialog import env, qapp
from test_chunked_pipeline import FakeASR
from test_ui_flow import until
from app.ui.settings_dialog import SettingsDialog, ASR_PRESETS


def test_cancel_does_not_change_config_or_keys(env):
    dlg, cfg, cm, _ = env
    before = asdict(cfg)
    disk = cm.config_file.read_bytes()
    secrets = cm.secrets_file.read_bytes()
    dlg.edit_model.setText("not-saved")
    dlg.edit_key.setText("not-saved-key")
    dlg.reject()
    assert asdict(cfg) == before
    assert cm.config_file.read_bytes() == disk
    assert cm.secrets_file.read_bytes() == secrets


def test_save_untouched_fields_preserves_nonstandard_values(env):
    _, cfg, cm, _ = env
    cfg.asr_retries = 27  # 超出表单范围，未编辑时仍必须保留。
    cfg.audio_sample_rate = 32000
    cfg.asr_language = "custom-language"
    cfg.extra = {"future-option": {"nested": [1, 2]}}
    cm.save(cfg)
    before = asdict(cfg)
    dlg = SettingsDialog(cfg, cm)
    dlg.edit_model.setText("new-model")
    dlg.on_save()
    after = asdict(cm.cfg)
    before["asr_model"] = "new-model"
    assert after == before
    assert cm.load().extra == {"future-option": {"nested": [1, 2]}}
    dlg.close()


def test_preset_switch_keeps_secrets_and_cancel_keeps_original(env):
    dlg, cfg, cm, _ = env
    before = cm.secret("asr_api_key")
    for i in range(len(ASR_PRESETS)):
        dlg.cmb_preset.setCurrentIndex(i)
        assert dlg.edit_key.text() == before
    dlg.reject()
    assert cm.secret("asr_api_key") == before
    assert cm.cfg.asr_model == cfg.asr_model


def test_connection_check_then_cancel_never_persists_edits(env, qapp):
    dlg, cfg, cm, _ = env
    before = asdict(cm.cfg)
    disk = cm.config_file.read_bytes()
    secrets = cm.secrets_file.read_bytes()
    with FakeASR() as server:
        dlg.edit_base.setText(server.base_url)
        dlg.edit_proxy.setText("")
        dlg.edit_key.setText("probe-only-key")
        dlg.on_probe()
        until(qapp, lambda: not dlg._probe_worker.isRunning())
        assert "检查" in dlg.lbl_probe.text()
        assert dlg._probe_worker.cm.secret("asr_api_key") == "probe-only-key"
        dlg.reject()
    assert cm.config_file.read_bytes() == disk
    assert cm.secrets_file.read_bytes() == secrets
    assert asdict(cm.cfg) == before


def test_cancel_during_probe_waits_for_worker_without_saving(env, qapp, monkeypatch):
    import ecnu_transcribe.transcriber as transcriber
    import threading
    dlg, _, cm, _ = env
    entered = threading.Event()
    release = threading.Event()
    def probe(*args, **kwargs):
        entered.set()
        release.wait(4)
        return True, "模拟检查通过"
    monkeypatch.setattr(transcriber, "probe_asr_endpoint", probe)
    before = cm.config_file.read_bytes()
    dlg.show()
    dlg.on_probe()
    until(qapp, entered.is_set)
    dlg.reject()
    assert dlg.isVisible()
    release.set()
    until(qapp, lambda: not dlg._probe_worker.isRunning())
    until(qapp, lambda: not dlg.isVisible())
    assert dlg.result() == QDialog.Rejected
    assert cm.config_file.read_bytes() == before


def test_settings_navigation_and_collapsed_advanced(env):
    dlg, _, _, _ = env
    assert dlg.navigation.count() == dlg.tabs.count() == 4
    toggle = dlg.asr_advanced.findChild(QToolButton)
    assert not toggle.isChecked()
    toggle.click()
    assert toggle.isChecked()
    dlg.navigation.setCurrentRow(2)
    assert dlg.tabs.currentIndex() == 2
    dlg.chk_llm.setChecked(False)
    assert dlg.llm_options.isHidden()
