"""回归（缺陷 50）：**续跑不能盲目信任阶段标记**。

事故现场（2026-09-14 真实账号验收）：

    1. 09:48 那次拉流被中断，缓存里留下 1900.5s 的半份音频（清单标注 3301.0s）；
    2. 旧代码只在日志里写了句「时长偏差较大 42.4%」，然后记 `audio_ready`、照常转写；
    3. 12:00 重新跑验收时，状态库把任务恢复成 `audio_ready`，流水线看到
       「有文件、且 > 1024 字节」就直接 `复用已有音频（不重下）`
       —— 于是**那半份音频又被送去转写**，用户拿到的稿子缺了 42% 却毫无标记。

结论：`audio_ready` 只说明「当时以为完成了」，不能当作产物完整的证据。
复用之前必须拿**实际时长**和清单时长对一遍（见 ``Pipeline._run_inner``）。

另外这里顺带锁住第二个坑：任务在 `audio_ready` 时，`audio_ready → probing`
是**非法迁移**，`update_stage` 会拒绝并**连带丢掉 play_url / duration 等字段**
（界面上于是显示旧地址、旧时长）。所以流水线必须按阶段合法性分流。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ecnu_transcribe import media
from ecnu_transcribe.catalog import Resource
from ecnu_transcribe.config import AppConfig, ConfigManager
from ecnu_transcribe.downloader import AudioResult
from ecnu_transcribe.pipeline import Pipeline, PipelineHooks
from ecnu_transcribe.store import Stage, StateStore, TaskRecord
from ecnu_transcribe.transcriber import Segment, Transcript

MANIFEST_SEC = 60.0


def _make_mp3(path: Path, seconds: float) -> Path:
    exe = media.find_ffmpeg()
    media.run_ffmpeg(
        [str(exe), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", "-ac", "1", "-ar", "16000",
         "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", str(path)],
        timeout=120, check=True,
    )
    return path


class _FakeTranscriber:
    name = "fake-asr"

    def __init__(self, *_: object, **__: object) -> None:
        self.calls: list[tuple[str, float]] = []

    def transcribe(self, audio_path: Path, *, duration_sec: float = 0.0) -> Transcript:
        self.calls.append((str(audio_path), duration_sec))
        dur = duration_sec or media.duration_of(Path(audio_path))
        return Transcript(
            segments=[Segment(0.0, dur / 2, "第一段。"), Segment(dur / 2, dur, "第二段。")],
            language="zh",
            duration_sec=dur,
            model="fake-asr-v1",
            provider=self.name,
            meta={"fake": True},
        )


class _FakeDownloader:
    """假下载器：记录被调用的次数，并交回一份**完整**音频。"""

    calls: list[str] = []
    kwargs: list[dict] = []
    complete_audio: Path | None = None

    def __init__(self, cfg: AppConfig, *_: object, **__: object) -> None:
        self.cfg = cfg

    def reuse_http_client(self, *_a: object) -> None:
        return None

    def fetch(self, resource: Resource, **kw: object) -> AudioResult:
        _FakeDownloader.calls.append(resource.resource_id)
        _FakeDownloader.kwargs.append(dict(kw))
        path = _FakeDownloader.complete_audio
        assert path is not None
        return AudioResult(
            path=path,
            sha256=media.sha256_file(path),
            duration_sec=media.duration_of(path),
            from_cache=False,
            source_url="file://fake",
        )


@pytest.fixture()
def work_env(tmp_path, monkeypatch):
    """隔离出「应用主目录 + 输出目录」，并装好假下载器/假 ASR。"""
    from ecnu_transcribe import paths

    home = tmp_path / "home"
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    for name in ("cache_dir", "media_cache_dir", "data_dir", "logs_dir"):
        if hasattr(paths, name):
            monkeypatch.setattr(paths, name, (lambda h=home, n=name: h / n), raising=False)

    import ecnu_transcribe.pipeline as pipeline_mod

    _FakeDownloader.calls = []
    monkeypatch.setattr(pipeline_mod, "AudioDownloader", _FakeDownloader)
    fake = _FakeTranscriber()
    monkeypatch.setattr(pipeline_mod, "create_transcriber", lambda *a, **kw: fake)
    return {"home": home, "out": out, "transcriber": fake}


def _resource(target: Path) -> Resource:
    return Resource(
        resource_id="VOD-1",
        title="回归样例",
        course_name="测试课",
        play_url=str(target),
        duration_sec=MANIFEST_SEC,
    )


def _run(tmp_path, work_env, *, stored_audio: Path, manifest: float = MANIFEST_SEC):
    cfg = AppConfig()
    cfg.output_dir = str(work_env["out"])
    cfg.llm_enabled = False
    cfg.asr_auto_gain = False
    cm = ConfigManager(config_file=tmp_path / "cfg.json", secrets_file=tmp_path / "sec.json")
    store = StateStore(tmp_path / "state.db")
    resource = _resource(stored_audio)
    res = Resource(
        resource_id=resource.resource_id,
        title=resource.title,
        course_name=resource.course_name,
        play_url=resource.play_url,
        duration_sec=manifest,
    )
    task = store.upsert_task(
        TaskRecord(
            course=resource.course_name,
            course_id="C-1",
            resource_id=resource.resource_id,
            title=resource.title,
            output_dir=cfg.output_dir,
            duration_sec=manifest,
            play_url="https://example.invalid/old-signed-url",
            audio_path=str(stored_audio),
            stage=str(Stage.AUDIO_READY),
        )
    )
    pipe = Pipeline(cfg, store, cm=cm, hooks=PipelineHooks())
    try:
        final = pipe.run(task, res)
    finally:
        pipe.close()
        store.close()
    return final


def test_short_cached_audio_is_redownloaded_not_transcribed(tmp_path, work_env):
    """缓存里只有 5s / 清单 60s：必须重下，**不能**拿半份音频去转写。"""
    short = _make_mp3(tmp_path / "short.mp3", 5.0)
    complete = _make_mp3(tmp_path / "complete.mp3", MANIFEST_SEC)
    _FakeDownloader.complete_audio = complete

    final = _run(tmp_path, work_env, stored_audio=short)

    assert _FakeDownloader.calls == ["VOD-1"], "不完整的音频必须触发重新拉取"
    assert final.stage == str(Stage.DONE), f"{final.stage} / {final.error[:200]}"
    used = [p for p, _d in work_env["transcriber"].calls]
    assert used == [str(complete)], f"送去转写的应是补全后的音频，实际 {used}"
    assert not any(str(short) == p for p in used), "半份音频绝不能被送去转写"


def test_complete_cached_audio_is_reused_without_download(tmp_path, work_env):
    """完整音频仍然要能复用（别把修补成「每次都重下」）。"""
    complete = _make_mp3(tmp_path / "complete.mp3", MANIFEST_SEC)
    _FakeDownloader.complete_audio = complete

    final = _run(tmp_path, work_env, stored_audio=complete)

    assert _FakeDownloader.calls == [], "完整音频不该重下"
    assert final.stage == str(Stage.DONE), f"{final.stage} / {final.error[:200]}"
    assert [p for p, _d in work_env["transcriber"].calls] == [str(complete)]


def test_resume_from_audio_ready_still_refreshes_url_and_duration(tmp_path, work_env):
    """`audio_ready → probing` 非法：字段刷新不能被丢掉（否则界面显示旧地址）。"""
    complete = _make_mp3(tmp_path / "complete.mp3", MANIFEST_SEC)
    _FakeDownloader.complete_audio = complete

    final = _run(tmp_path, work_env, stored_audio=complete)
    assert final.play_url == str(complete), "play_url 必须被刷新（而不是被非法迁移吞掉）"
    assert final.duration_sec == pytest.approx(MANIFEST_SEC, abs=0.5)


def test_pipeline_passes_known_duration_to_downloader(tmp_path, work_env):
    """回归（缺陷 61）：流水线必须把**已知时长**传给下载器。

    实测事故：GUI 里那条课的清单 Resource `duration_sec=0`（任务行里其实是 3301），
    而流水线调用 `fetch()` 时没传 `expect_duration` ⇒ 下载器拿到 0 ⇒
    ① 进度百分比永远算不出来（界面停在 3%，用户以为卡住）；
    ② "已下完的残件是否完整"无从判断 ⇒ 完整的 25 MB 音频被当成半成品白重下。
    """
    short = _make_mp3(tmp_path / "short.mp3", 5.0)
    complete = _make_mp3(tmp_path / "complete.mp3", MANIFEST_SEC)
    _FakeDownloader.complete_audio = complete
    _FakeDownloader.kwargs = []

    _run(tmp_path, work_env, stored_audio=short)

    assert _FakeDownloader.kwargs, "应当调用过下载器"
    passed = _FakeDownloader.kwargs[-1].get("expect_duration")
    assert passed == pytest.approx(MANIFEST_SEC, abs=0.5), (
        f"必须把清单时长传下去（实际传了 {passed!r}）—— 传 0/None 会让进度与残件判断全部失效"
    )
