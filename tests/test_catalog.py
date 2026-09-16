"""清单解析测试（M6）：用 fixture 覆盖多种平台字段命名。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecnu_transcribe.catalog import Catalog, Course, Resource, normalize_time, pick, safe_filename
from ecnu_transcribe.errors import ApiChangedError


def test_pick_candidates_and_nested():
    obj = {"a": None, "b": "  ", "c": 0, "nested": {"x": "v"}}
    assert pick(obj, "a", "b", "c") == 0          # 0 是有效值，应命中
    assert pick(obj, "a", "b") is None            # 候选键都无有效值时返回 None
    assert pick(obj, "a", "missing", default="d") == "d"
    assert pick(obj, "nested.x") == "v"
    assert pick(obj, "nested.y", "c") == 0


@pytest.mark.parametrize(
    "raw,expected",
    [
        (2712, "2025-09-08 08:00:00"),  # 占位：只验证数字时间戳与字符串解析不炸
    ],
)
def test_normalize_time_numeric_and_string(raw, expected):
    assert normalize_time("2025-09-08 08:00:00") == "2025-09-08 08:00:00"
    assert normalize_time("2025-09-08T08:00") == "2025-09-08 08:00:00"
    assert normalize_time("") == ""
    # 毫秒时间戳会被转成本地时间字符串（不校验具体值，只校验形状）
    out = normalize_time(1757000000000)
    assert len(out) == 19 and out[4] == "-" and out[13] == ":"


def test_resource_from_api_full(sample_catalog_json):
    row = sample_catalog_json["course_list"]["data"]["rows"][0]["resources"][0]
    res = Resource.from_api(row, course_id="C-1001", course_name="数据结构与算法")
    assert res.resource_id == "R-10011"
    assert res.title.startswith("第1讲")
    assert res.duration_sec == 2712
    assert res.record_time == "2025-09-08 08:00:00"
    assert res.mime == "application/vnd.apple.mpegurl"
    assert res.size_hint == 524288000
    assert res.play_url.startswith("https://media.example.edu/")


def test_resource_from_api_alternate_field_names(sample_catalog_json):
    row = sample_catalog_json["course_list"]["data"]["rows"][0]["resources"][1]
    res = Resource.from_api(row, course_id="C-1001")
    assert res.resource_id == "R-10012"
    assert res.duration_sec == pytest.approx(3725.0)      # "01:02:05"
    assert res.mime == "application/vnd.apple.mpegurl"
    assert res.size_hint == 498000000


def test_resource_from_api_snake_case(sample_catalog_json):
    row = sample_catalog_json["resource_list"]["data"]["records"][0]
    res = Resource.from_api(row, course_id="C-1", course_name="X")
    assert res.resource_id == "R-20001"
    assert res.title.startswith("第3讲")
    assert res.chapter == "第三章"
    assert res.size_hint == 402000000


def test_resource_from_api_rejects_junk():
    with pytest.raises(ApiChangedError):
        Resource.from_api({"foo": "bar"}, strict=True)
    # strict=False 时不抛，退化成占位标题
    res = Resource.from_api({"foo": "bar"}, strict=False)
    assert res.title == "未命名录播"


def test_course_from_api_nested_containers(sample_catalog_json):
    rows = sample_catalog_json["course_list"]["data"]["rows"]
    expected = [2, 1, 1]  # 第 1 门内嵌 resources 两条；第 2 门 resourceList；第 3 门 videoList
    for row, want in zip(rows, expected):
        course = Course.from_api(row)
        assert course.course_id
        assert course.course_name
        assert len(course.resources) == want
        for res in course.resources:
            assert res.resource_id
            assert res.title
            assert res.course_id == course.course_id
            assert res.course_name == course.course_name


def test_course_total_duration(sample_catalog_json):
    course = Course.from_api(sample_catalog_json["course_list"]["data"]["rows"][0])
    assert course.total_duration == pytest.approx(2712 + 3725)


def test_extract_list_variants():
    from ecnu_transcribe.client import EcnuClient

    rows, total = EcnuClient.extract_list({"data": {"rows": [1, 2, 3], "total": 9}})
    assert rows == [1, 2, 3] and total == 9

    rows, total = EcnuClient.extract_list({"code": 0, "data": {"records": [{"a": 1}], "totalCount": 1}})
    assert rows == [{"a": 1}] and total == 1

    rows, total = EcnuClient.extract_list([{"x": 1}, {"x": 2}])
    assert len(rows) == 2 and total == 2

    rows, total = EcnuClient.extract_list({"result": {"pageData": {"list": [1], "totalCount": 5}}})
    assert rows == [1] and total == 5

    with pytest.raises(ApiChangedError):
        EcnuClient.extract_list({"nope": {"a": 1}}, "http://x")


def test_business_code_auth_detection():
    from ecnu_transcribe.client import EcnuClient
    from ecnu_transcribe.errors import AuthExpiredError

    class Dummy(EcnuClient):
        def __init__(self):  # noqa: D107
            pass

    d = Dummy()
    with pytest.raises(AuthExpiredError):
        d._check_business({"code": "A0230", "msg": "登录已过期"}, "http://x")
    with pytest.raises(AuthExpiredError):
        d._check_business({"code": 401, "message": "未登录"}, "http://x")
    # 正常包装会被解包
    assert d._check_business({"code": 200, "data": {"rows": []}}, "http://x") == {"rows": []}
    # 业务失败但不是鉴权问题 → ApiBusinessError
    from ecnu_transcribe.errors import ApiBusinessError

    with pytest.raises(ApiBusinessError):
        d._check_business({"code": 500, "msg": "服务器错误"}, "http://x")


def test_safe_filename_windows_rules():
    assert safe_filename('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"
    assert safe_filename("  末尾空格和点 . ") == "末尾空格和点"
    assert safe_filename("CON") == "_CON"
    assert safe_filename("") == "untitled"
    long = safe_filename("长" * 500)
    assert len(long) <= 120
    # 中文保留
    assert safe_filename("第1讲 绪论") == "第1讲 绪论"


def test_catalog_roundtrip(tmp_path, sample_catalog_json):
    cat = Catalog(fetched_at="2026-01-01 00:00:00", student_id="20261234567", source="x")
    for row in sample_catalog_json["course_list"]["data"]["rows"]:
        cat.courses.append(Course.from_api(row))
    assert cat.resource_count == 4

    path = cat.save(tmp_path / "catalog.json")
    assert path.is_file()
    loaded = Catalog.load(path)
    assert loaded.resource_count == cat.resource_count
    assert loaded.courses[0].resources[0].resource_id == cat.courses[0].resources[0].resource_id
    assert loaded.total_duration == pytest.approx(cat.total_duration)
    assert "课程数" in loaded.summary()


def test_real_fixture_file_is_valid_json(fixtures_dir: Path):
    data = json.loads((fixtures_dir / "catalog_sample.json").read_text(encoding="utf-8"))
    assert "course_list" in data and "asr_verbose_json" in data
