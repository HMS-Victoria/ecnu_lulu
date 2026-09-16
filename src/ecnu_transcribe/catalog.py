"""清单数据模型：课程 / 资源 / 播放信息。

字段命名与 ``data/catalog.json`` 一一对应（M1 验收要求）::

    course_id, course_name, teacher, resource_id, title,
    duration_sec, record_time, mime, size_hint, play_url

平台返回的字段名在不同版本里会变，因此 :func:`Resource.from_api` 用**候选键**
的方式做「宽容解析」：命中第一组就用，并把原始对象完整保留在 ``raw`` 里，
便于接口变更时快速定位。解析不到关键字段时抛 :class:`ApiChangedError`，
绝不静默产出空清单。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .errors import ApiChangedError

# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str, *, max_len: int = 120, fallback: str = "untitled") -> str:
    """把标题变成 Windows 合法文件名（保留中文，去掉非法字符与结尾空格点）。"""
    s = (name or "").strip()
    s = _ILLEGAL_FS.sub("_", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        s = fallback
    if s.upper().split(".")[0] in _WIN_RESERVED:
        s = "_" + s
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    return s or fallback


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def pick(obj: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """从字典里按候选键取第一个「有值」的字段（支持 ``a.b`` 形式的浅层路径）。

    注意：``0`` 与 ``False`` 是**有效值**（例如 ``total=0``、``duration=0``），
    只有 ``None`` / 空白字符串 / 空容器才算「没值」。
    """
    for key in keys:
        if "." in key:
            cur: Any = obj
            ok = True
            for part in key.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            if ok and not _is_blank(cur):
                return cur
            continue
        if key in obj and not _is_blank(obj[key]):
            return obj[key]
    return default


def _to_float(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    # "01:02:05" / "1:02:05.5" / "625"
    if ":" in s:
        parts = s.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return 0.0
        secs = 0.0
        for n in nums:
            secs = secs * 60 + n
        return secs
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else 0.0


def _to_int(value: Any) -> int:
    return int(_to_float(value))


def normalize_time(value: Any) -> str:
    """把录制时间统一成 ``YYYY-MM-DD HH:MM:SS``（拿不到就原样返回字符串）。"""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # 毫秒
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return str(value)
    s = str(value).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}:{m.group(6) or '00'}"
    return s


def guess_mime(url: str = "", explicit: str = "") -> str:
    if explicit:
        return explicit
    low = (url or "").lower().split("?")[0]
    if low.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if low.endswith(".mp4"):
        return "video/mp4"
    if low.endswith(".mp3"):
        return "audio/mpeg"
    if low.endswith(".m4a"):
        return "audio/mp4"
    if low.endswith(".flac"):
        return "audio/flac"
    return ""


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #
@dataclass
class Resource:
    """一条录播/回放资源。"""

    resource_id: str = ""
    title: str = ""
    course_id: str = ""
    course_name: str = ""
    teacher: str = ""
    duration_sec: float = 0.0
    record_time: str = ""
    mime: str = ""
    size_hint: int = 0
    play_url: str = ""
    chapter: str = ""
    resource_type: str = ""
    #: 平台原始 JSON（脱敏后使用，便于接口变更排查）
    raw: dict[str, Any] = field(default_factory=dict)

    # -- 解析 --------------------------------------------------------------- #
    ID_KEYS = (
        "resourceId", "resource_id", "id", "courseResourceId", "coursewareId",
        "wareId", "videoId", "mediaId", "materialId", "fileId", "bizId", "uuid",
    )
    TITLE_KEYS = (
        "title", "resourceName", "resource_name", "name", "wareName", "videoName",
        "coursewareName", "fileName", "materialName", "nodeName", "subTitle", "caption",
    )
    URL_KEYS = (
        "playUrl", "play_url", "playurl", "url", "videoUrl", "video_url", "fileUrl",
        "file_url", "mediaUrl", "resourceUrl", "downloadUrl", "hlsUrl", "m3u8",
        "m3u8Url", "playPath", "path", "src", "content",
    )
    DUR_KEYS = (
        "duration", "durationSec", "duration_sec", "durationSeconds", "videoDuration",
        "length", "timeLength", "playTime", "totalTime", "mediaDuration",
    )
    TIME_KEYS = (
        "recordTime", "record_time", "startTime", "start_time", "createTime", "create_time",
        "beginTime", "begin_time", "publishTime", "publish_time", "updateTime", "classTime",
        "liveStartTime", "date", "gmtCreate", "occTime",
    )
    SIZE_KEYS = ("size", "fileSize", "file_size", "sizeHint", "bytes", "contentLength")
    MIME_KEYS = ("mime", "mimeType", "contentType", "mediaType", "fileType", "format", "type")
    TYPE_KEYS = ("resourceType", "resource_type", "type", "bizType", "category", "wareType")
    CHAPTER_KEYS = ("chapterName", "chapter", "sectionName", "catalogName", "parentName", "unitName")

    @classmethod
    def from_api(
        cls,
        obj: dict[str, Any],
        *,
        course_id: str = "",
        course_name: str = "",
        teacher: str = "",
        strict: bool = True,
    ) -> "Resource":
        if not isinstance(obj, dict):
            raise ApiChangedError(
                f"资源条目不是 JSON 对象，而是 {type(obj).__name__}", sample=str(obj)[:300]
            )

        rid = str(pick(obj, *cls.ID_KEYS, default="") or "")
        title = str(pick(obj, *cls.TITLE_KEYS, default="") or "")
        url = str(pick(obj, *cls.URL_KEYS, default="") or "")

        if strict and not rid and not title:
            raise ApiChangedError(
                "资源条目缺少可识别的 id/title 字段，平台接口可能已变更",
                sample=json.dumps(obj, ensure_ascii=False)[:500],
            )

        mime_explicit = str(pick(obj, *cls.MIME_KEYS, default="") or "")
        return cls(
            resource_id=rid or title,
            title=title or rid or "未命名录播",
            course_id=str(pick(obj, "courseId", "course_id", default=course_id) or course_id),
            course_name=str(pick(obj, "courseName", "course_name", default=course_name) or course_name),
            teacher=str(pick(obj, "teacherName", "teacher", "lecturer", "ownerName", default=teacher) or teacher),
            duration_sec=_to_float(pick(obj, *cls.DUR_KEYS, default=0)),
            record_time=normalize_time(pick(obj, *cls.TIME_KEYS, default="")),
            mime=guess_mime(url, mime_explicit),
            size_hint=_to_int(pick(obj, *cls.SIZE_KEYS, default=0)),
            play_url=url,
            chapter=str(pick(obj, *cls.CHAPTER_KEYS, default="") or ""),
            resource_type=str(pick(obj, *cls.TYPE_KEYS, default="") or ""),
            raw=obj,
        )

    @property
    def is_video_like(self) -> bool:
        return bool(self.play_url) or self.mime.startswith(("video/", "application/vnd.apple"))

    @property
    def duration_human(self) -> str:
        from .media import human_duration

        return human_duration(self.duration_sec)

    @property
    def unique_key(self) -> str:
        return f"{self.course_id}::{self.resource_id}"

    def with_runtime(self, *, title: str = "", course: str = "") -> "Resource":
        """返回一份带运行期覆写（用户在队列里改过的标题 / 课程名）的副本。"""
        from dataclasses import replace

        changes: dict[str, Any] = {}
        if title and title != self.title:
            changes["title"] = title
        if course and course != self.course_name:
            changes["course_name"] = course
        return replace(self, **changes) if changes else self

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d


@dataclass
class Course:
    course_id: str = ""
    course_name: str = ""
    teacher: str = ""
    term: str = ""
    resources: list[Resource] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    COURSE_ID_KEYS = (
        "courseId", "course_id", "id", "classId", "teachingClassId", "clazzId",
        "sectionId", "bizId", "uuid",
    )
    COURSE_NAME_KEYS = (
        "courseName", "course_name", "name", "title", "clazzName", "className",
        "teachingClassName", "subjectName", "displayName",
    )
    TEACHER_KEYS = ("teacherName", "teacher", "lecturer", "ownerName", "headTeacher", "userName")

    @classmethod
    def from_api(
        cls,
        obj: dict[str, Any],
        *,
        strict: bool = True,
        resource_containers: Iterable[str] = (
            "resources", "resourceList", "records", "list", "items", "children",
            "wares", "wareList", "coursewares", "videos", "videoList", "medias",
            "materialList", "nodeList", "data",
        ),
    ) -> "Course":
        if not isinstance(obj, dict):
            raise ApiChangedError(
                f"课程条目不是 JSON 对象，而是 {type(obj).__name__}", sample=str(obj)[:300]
            )
        cid = str(pick(obj, *cls.COURSE_ID_KEYS, default="") or "")
        cname = str(pick(obj, *cls.COURSE_NAME_KEYS, default="") or "")
        if strict and not cid and not cname:
            raise ApiChangedError(
                "课程条目缺少可识别的 id/name 字段，平台接口可能已变更",
                sample=json.dumps(obj, ensure_ascii=False)[:500],
            )
        teacher = str(pick(obj, *cls.TEACHER_KEYS, default="") or "")
        course = cls(
            course_id=cid or cname,
            course_name=cname or cid or "未命名课程",
            teacher=teacher,
            term=str(pick(obj, "term", "termName", "semester", "xnxq", "schoolYear", default="") or ""),
            raw=obj,
        )

        # 有些接口把资源直接内嵌在课程对象里
        for container in resource_containers:
            val = obj.get(container)
            if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
                for item in val:
                    try:
                        course.resources.append(
                            Resource.from_api(
                                item,
                                course_id=course.course_id,
                                course_name=course.course_name,
                                teacher=course.teacher,
                                strict=False,
                            )
                        )
                    except ApiChangedError:
                        continue
                break
        return course

    @property
    def total_duration(self) -> float:
        return sum(r.duration_sec for r in self.resources)

    def to_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "course_name": self.course_name,
            "teacher": self.teacher,
            "term": self.term,
            "resource_count": len(self.resources),
            "total_duration_sec": self.total_duration,
            "resources": [r.to_dict() for r in self.resources],
        }


@dataclass
class Catalog:
    """全量清单。"""

    courses: list[Course] = field(default_factory=list)
    fetched_at: str = ""
    student_id: str = ""
    source: str = ""

    def __iter__(self) -> Iterator[Course]:
        return iter(self.courses)

    def __len__(self) -> int:
        return len(self.courses)

    @property
    def resources(self) -> list[Resource]:
        return [r for c in self.courses for r in c.resources]

    @property
    def resource_count(self) -> int:
        return sum(len(c.resources) for c in self.courses)

    @property
    def total_duration(self) -> float:
        return sum(r.duration_sec for r in self.resources)

    def summary(self) -> str:
        from .media import human_duration

        return (
            f"课程数 {len(self.courses)} / 资源总数 {self.resource_count} / "
            f"时长合计 {human_duration(self.total_duration)}（{self.total_duration:.1f}s）"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at,
            "student_id": self.student_id,
            "source": self.source,
            "course_count": len(self.courses),
            "resource_count": self.resource_count,
            "total_duration_sec": self.total_duration,
            "summary": self.summary(),
            "courses": [c.to_dict() for c in self.courses],
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        cat = cls(
            fetched_at=str(data.get("fetched_at", "")),
            student_id=str(data.get("student_id", "")),
            source=str(data.get("source", "")),
        )
        for c in data.get("courses", []):
            course = Course(
                course_id=str(c.get("course_id", "")),
                course_name=str(c.get("course_name", "")),
                teacher=str(c.get("teacher", "")),
                term=str(c.get("term", "")),
            )
            for r in c.get("resources", []):
                course.resources.append(
                    Resource(
                        resource_id=str(r.get("resource_id", "")),
                        title=str(r.get("title", "")),
                        course_id=str(r.get("course_id", course.course_id)),
                        course_name=str(r.get("course_name", course.course_name)),
                        teacher=str(r.get("teacher", course.teacher)),
                        duration_sec=float(r.get("duration_sec") or 0),
                        record_time=str(r.get("record_time", "")),
                        mime=str(r.get("mime", "")),
                        size_hint=int(r.get("size_hint") or 0),
                        play_url=str(r.get("play_url", "")),
                        chapter=str(r.get("chapter", "")),
                        resource_type=str(r.get("resource_type", "")),
                    )
                )
            cat.courses.append(course)
        return cat
