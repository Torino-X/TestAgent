"""Artifact 契约 — Phase 2.9A.17.

集中管理 Graph State 中 `artifact` 字段的规范化与校验:

  * 推荐入口: :func:`normalize_export_artifact` — 把任意 ``data`` / ORM / dict 转成
    JSON 可序列化、字段齐备的 canonical dict,供 ``export_word_node`` 写入
    Graph State;
  * 错误码定义: :data:`ExportArtifactErrorCode` — 6 个互斥 error code,
    让 export / format-check / 上层路由基于语义分类而非字符串 hard-code;
  * 强约束: 不在 Graph State 序列化路径上引入 ORM / Path / bytes 等非 JSON-safe
    类型(满足 2.9B Checkpoint / Postgres JSON 列要求)。

设计目标(严格遵守):
  1. 唯一事实源 — Tool / Adapter / Node / FormatCheck 调同一个函数得到一致视图;
  2. 错误可观测 — 失败时同时返回 ``code`` 与 ``details``(字段级),不做"全垒
     FORMAT_NO_ARTIFACT";
  3. 向后兼容 — 缺失字段视为 None / 空串,而非抛错;Decision 取决于调用方。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# ── 错误码 ────────────────────────────────────────────────────────────────


class ExportArtifactErrorCode:
    """Export Artifact 错误码 — Phase 2.9A.17 收口后的分层分类。

    命名规则: ``EXPORT_ARTIFACT_<PHASE>_<DETAIL>`` — 高内聚,便于 grep。

    ``EXPORT_ARTIFACT_STATE_INVALID`` 是"成功伪装的失败" — 文件已生成但
    返回结构不完整,留给调用方决定是否清空 state.artifact。
    """

    MISSING_PUBLIC_ID = "EXPORT_ARTIFACT_MISSING_PUBLIC_ID"
    MISSING_STORAGE_PATH = "EXPORT_ARTIFACT_MISSING_STORAGE_PATH"
    MISSING_FILE_NAME = "EXPORT_ARTIFACT_MISSING_FILE_NAME"
    UNSUPPORTED_TYPE = "EXPORT_ARTIFACT_UNSUPPORTED_TYPE"
    STATE_INVALID = "EXPORT_ARTIFACT_STATE_INVALID"  # 通用兜底
    SERIALIZATION_FAILED = "EXPORT_ARTIFACT_SERIALIZATION_FAILED"


# ── 规范化函数 ────────────────────────────────────────────────────────────


# 允许出现在 canonical artifact dict 中的字段白名单。其它字段丢弃以避免
# ORM 对象、bytes、Session 等不可序列化 payload 混入 Graph State。
_ALLOWED_TOP_LEVEL_KEYS: frozenset = frozenset({
    "public_id",
    "internal_id",
    "artifact_id",       # alias of public_id (legacy envelope.data 用)
    "artifact_type",
    "file_name",
    "file_ext",
    "mime_type",
    "file_size",
    "page_count",
    "storage_path",
    "download_url",
    "integrity",
    "version",
    "checksum",
    "created_at",
})


@dataclass(frozen=True)
class ArtifactNormalizeResult:
    """Phase 2.9A.17 规范化结果。

    Attributes:
        ok: 是否规范化成功(成功时不返回 errors)。
        artifact: canonical dict;always JSON-safe;失败时为 ``{}``(不抛)。
        errors: 错误列表(可多个 — 比如同时缺 public_id 与 storage_path)。
        warnings: 警告列表(非阻断 — 比如 mime_type 缺失但其它都完整)。
    """

    ok: bool
    artifact: Dict[str, Any]
    errors: List[str]
    warnings: List[str]


def normalize_export_artifact(
    raw: Any,
    *,
    expected_artifact_type: str = "test_plan_word",
) -> ArtifactNormalizeResult:
    """Phase 2.9A.17 唯一规范入口。

    输入容忍形态:
      * ``dict``:正常 envelope.data 或 Graph State artifact。
      * ``None`` / 非 dict:视为空输入。
      * 含 ORM / Path / bytes:转成 ``str`` / ``int`` / 整型常量,失败计入 errors。

    输出:
      * canonical dict — public_id / artifact_type / file_name / mime_type /
        storage_path / file_size 必备;
      * ok=True 仅在 errors 为空 + 必填字段齐备时;
      * 失败 artifact={} — 让调用方走 ``EXPORT_ARTIFACT_STATE_INVALID`` 路径。
    """
    errors: List[str] = []
    warnings: List[str] = []
    artifact: Dict[str, Any] = {}

    if raw is None or not isinstance(raw, dict):
        errors.append(ExportArtifactErrorCode.STATE_INVALID)
        return ArtifactNormalizeResult(
            ok=False, artifact={}, errors=errors, warnings=warnings
        )

    canonical: Dict[str, Any] = {}

    # 1. 字段白名单 + 逐字段类型规整
    for key in _ALLOWED_TOP_LEVEL_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        # None 视同"未提供",让后续 alias / 默认值策略接管;
        # ORM / 非序列化对象探测 — 失败则丢弃并警告。
        if value is None:
            continue
        if _is_non_serializable(value):
            warnings.append(f"drop_non_serializable:{key}")
            continue
        canonical[key] = _coerce_value(value)

    # 2. public_id 双别名兜底:WordExportTool envelope.data 用 artifact_id,
    #    Graph State artifact 字段约定用 public_id。这里双向兼容。
    if (not canonical.get("public_id")) and canonical.get("artifact_id"):
        canonical["public_id"] = canonical["artifact_id"]
    elif (not canonical.get("artifact_id")) and canonical.get("public_id"):
        canonical["artifact_id"] = canonical["public_id"]


    # 3. artifact_type — 缺失则记错误而非默认;类型不在白名单则报 UNSUPPORTED
    artifact_type = canonical.get("artifact_type")
    if artifact_type is None or artifact_type == "":
        errors.append(ExportArtifactErrorCode.MISSING_PUBLIC_ID)  # 与"无 public_id"等价同错
    elif artifact_type != expected_artifact_type:
        errors.append(ExportArtifactErrorCode.UNSUPPORTED_TYPE)

    # 4. public_id 必备
    if not canonical.get("public_id"):
        errors.append(ExportArtifactErrorCode.MISSING_PUBLIC_ID)

    # 5. storage_path 必备(FormatCheck 强依赖);空 → MISSING_STORAGE_PATH
    storage_path = canonical.get("storage_path")
    if not storage_path or not isinstance(storage_path, str):
        errors.append(ExportArtifactErrorCode.MISSING_STORAGE_PATH)

    # 6. file_name 必备(给前端展示用)
    if not canonical.get("file_name"):
        errors.append(ExportArtifactErrorCode.MISSING_FILE_NAME)

    # 7. mime_type / file_size 是软字段;缺则告警但不算失败
    if not canonical.get("mime_type"):
        warnings.append("missing_mime_type")
    if not isinstance(canonical.get("file_size"), int):
        if canonical.get("file_size") is not None:
            warnings.append("file_size_not_int")
        canonical["file_size"] = int(canonical.get("file_size") or 0)
    if canonical.get("page_count") is not None and not isinstance(canonical.get("page_count"), int):
        try:
            canonical["page_count"] = int(canonical.get("page_count") or 0)
        except (TypeError, ValueError):
            warnings.append("page_count_not_int")
            canonical.pop("page_count", None)

    # 8. 全 JSON-safe final check(防止 bytes / Path / set 漏网)
    try:
        import json as _json
        _json.dumps(canonical, default=str)
    except (TypeError, ValueError) as exc:
        errors.append(ExportArtifactErrorCode.SERIALIZATION_FAILED)
        canonical = {}

    ok = not errors
    return ArtifactNormalizeResult(
        ok=ok, artifact=canonical if ok else {}, errors=errors, warnings=warnings
    )


def is_artifact_present(artifact: Optional[Dict[str, Any]]) -> bool:
    """纯字段级判定 — ``storage_path`` + ``public_id`` 双有即视为 Artifact 存在。

    Phase 2.9A.17:这是 FormatCheck 与 Graph State 恢复链路的最低契约检查点;
    不依赖 ORM Session / file system —— 仅看 dict。
    """
    if not isinstance(artifact, dict):
        return False
    return bool(artifact.get("storage_path")) and bool(artifact.get("public_id"))


# ── 内部辅助 ──────────────────────────────────────────────────────────────


def _is_non_serializable(value: Any) -> bool:
    """粗粒度过滤掉 Path / bytes / Session / 自定义 ORM 标记对象。"""
    if value is None or isinstance(value, (str, int, float, bool, list, tuple, dict)):
        return False
    if isinstance(value, (bytes, bytearray)):
        return True
    # pathlib.Path / 任意带私有方法的 ORM 对象
    cls_name = type(value).__name__
    if cls_name in ("PosixPath", "WindowsPath", "PurePath", "Path"):
        return True
    # SQLAlchemy ORM session
    if cls_name.endswith("Session") or cls_name.endswith("AsyncSession"):
        return True
    # SQLAlchemy DeclarativeBase 实例
    if hasattr(value, "_sa_instance_state"):
        return True
    return False


def _coerce_value(value: Any) -> Any:
    """尽力把强类型值转成 JSON-safe — datetime → ISO str。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_coerce_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _coerce_value(v) for k, v in value.items()}
    # datetime/date → ISO 8601
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:  # noqa: BLE001 - non-fatal
            return str(value)
    return str(value)


__all__ = [
    "ArtifactNormalizeResult",
    "ExportArtifactErrorCode",
    "is_artifact_present",
    "normalize_export_artifact",
]


# 子包契约:Artifact(产物)的标准化 + 校验
# 包含:
#   - ExportArtifactErrorCode 枚举
#   - is_artifact_present / normalize_export_artifact
#   - 字段白名单:public_id / file_name / file_size / storage_path(仅
#     后端内部使用,不对外暴露) 等
#
# 调用方:
#   - graph: nodes_post_confirm.export_word_node(写入前规范化)
#   - graph: nodes_review_format.check_docx_format_node(读)
#   - api/v1/artifacts.py(返回前端前的过滤)
#
# 不允许:在外部 API 直接暴露 storage_path 给前端。
