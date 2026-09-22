"""声明校验、版本判定与显式迁移。

输入管线：原始文本 → :mod:`menu_review.parsing` 判定是否为完整 JSON 对象
→ 本模块按合同版本逐类报告问题，并把仍受支持的旧版声明**显式迁移**为
当前记录，把未来版本标记为等待升级。

支持的版本：

* ``0`` —— 旧版声明：缺少 ``source`` 字段，其余语义与 v1 相同；
  通过 :func:`migrate_v0_to_v1` 显式迁移，迁移动作留痕；
* ``1`` —— 当前版本。

任何更高版本一律不解读、不转换，原文由收件流程原样保全。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import issues
from .contracts import DomainRecord
from .issues import Issue
from .parsing import ParsedDocument, parse_document

CURRENT_VERSION = 1
MIN_SUPPORTED_VERSION = 0

# 各版本允许出现的字段（未知字段必须显式报告，而不是在构造时抛异常）
_ALLOWED_FIELDS: dict[int, frozenset[str]] = {
    0: frozenset({"schema_version", "record_id", "domain", "occurred_at", "revision"}),
    1: frozenset(
        {"schema_version", "record_id", "domain", "occurred_at", "revision", "source"}
    ),
}

RECORD_ID = "record_id"
OCCURRED_AT = "occurred_at"
REVISION = "revision"

# 收件判定结果
CURRENT = "current"
MIGRATED = "migrated"
REJECTED = "rejected"
PENDING_UPGRADE = "pending_upgrade"


@dataclass(frozen=True)
class RecordEvaluation:
    """一份文本的完整评估结论。"""

    outcome: str
    version: int | None = None
    record: DomainRecord | None = None
    issues: tuple[Issue, ...] = field(default_factory=tuple)
    warnings: tuple[Issue, ...] = field(default_factory=tuple)
    raw: str = ""

    @property
    def primary_issue(self) -> Issue | None:
        return self.issues[0] if self.issues else None


def evaluate_text(text: str) -> RecordEvaluation:
    """完整读取管线：文本 → 评估结论，绝不抛出异常。"""
    parsed = parse_document(text)
    return evaluate_document(parsed)


def evaluate_document(parsed: ParsedDocument) -> RecordEvaluation:
    """对解析产物做结构与内容校验。"""
    if not parsed.ok:
        issue = {
            "empty": issues.EMPTY_INPUT,
            "truncated": issues.TRUNCATED_JSON,
            "syntax": issues.NOT_JSON,
        }.get(parsed.error or "", issues.NOT_JSON)
        return RecordEvaluation(
            outcome=REJECTED, issues=(issue,), raw=parsed.raw
        )

    if not parsed.is_complete_object:
        return RecordEvaluation(
            outcome=REJECTED, issues=(issues.NOT_OBJECT,), raw=parsed.raw
        )

    payload = parsed.value
    problems: list[Issue] = []

    version = _read_version(payload)

    if version is not None and version > CURRENT_VERSION:
        # 未来版本优先：我们无权用 v1 规则解读它的字段，只要它是一个完整的
        # JSON 对象且版本号明确高于当前版本，就原样保全、等待升级。
        return RecordEvaluation(
            outcome=PENDING_UPGRADE,
            version=version,
            issues=(issues.FUTURE_VERSION,),
            raw=parsed.raw,
        )

    # 尾随内容与重复键：结构层面已确认的问题，逐类报告。
    if parsed.trailing:
        problems.append(issues.TRAILING_CONTENT)
    if parsed.duplicates:
        names = "、".join(item.key for item in parsed.duplicates)
        problems.append(
            Issue(
                issues.DUPLICATE_KEY.code,
                f"声明中存在重复键（{names}），系统只能取最后一个值，存在数据歧义",
                issues.DUPLICATE_KEY.action,
            )
        )

    if version is None:
        problems.append(issues.UNKNOWN_VERSION)
        return RecordEvaluation(
            outcome=REJECTED, version=None, issues=tuple(problems), raw=parsed.raw
        )

    if version < MIN_SUPPORTED_VERSION:
        problems.append(issues.UNSUPPORTED_VERSION)
        return RecordEvaluation(
            outcome=REJECTED, version=version, issues=tuple(problems), raw=parsed.raw
        )

    content_problems = _validate_fields(payload, version)
    problems.extend(content_problems)
    if problems:
        return RecordEvaluation(
            outcome=REJECTED, version=version, issues=tuple(problems), raw=parsed.raw
        )

    record = _build_record(payload, version)
    if version < CURRENT_VERSION:
        migrated = migrate_v0_to_v1(record)
        warning = Issue(
            "MIGRATED_V0_TO_V1",
            "v0 旧版声明已按既定迁移规则补齐 source 字段并升级为 v1 记录",
            issues.NOTE,
            issues.SEVERITY_WARNING,
        )
        return RecordEvaluation(
            outcome=MIGRATED,
            version=1,
            record=migrated,
            warnings=(warning,),
            raw=parsed.raw,
        )

    return RecordEvaluation(
        outcome=CURRENT, version=1, record=record, raw=parsed.raw
    )


def _read_version(payload: dict) -> int | None:
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return None
    return version


def _validate_fields(payload: dict, version: int) -> list[Issue]:
    """按字段逐类报告；一次返回全部发现，避免审核员来回补材料。"""
    problems: list[Issue] = []
    allowed = _ALLOWED_FIELDS[version]

    unknown = sorted(key for key in payload if key not in allowed)
    if unknown:
        problems.append(
            Issue(
                issues.UNKNOWN_FIELD.code,
                f"出现当前合同未定义的字段：{ '、'.join(unknown) }",
                issues.UNKNOWN_FIELD.action,
            )
        )

    record_id = payload.get(RECORD_ID)
    if not isinstance(record_id, str) or not record_id.strip():
        problems.append(issues.MISSING_IDENTIFIER)

    occurred_at = payload.get(OCCURRED_AT)
    if not isinstance(occurred_at, str) or not _is_timezone_aware(occurred_at):
        problems.append(issues.TIME_NO_TIMEZONE)

    revision = payload.get(REVISION)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        problems.append(issues.ILLEGAL_REVISION)

    return problems


def _is_timezone_aware(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _build_record(payload: dict, version: int) -> DomainRecord:
    data = dict(payload)
    if version == 0:
        # v0 没有 source；先按旧合同构造，迁移器再补齐。
        data["source"] = ""
    return DomainRecord(
        schema_version=version,
        record_id=data["record_id"],
        domain=data.get("domain", "menu_review"),
        occurred_at=data["occurred_at"],
        revision=data["revision"],
        source=data["source"],
    )


def migrate_v0_to_v1(record: DomainRecord) -> DomainRecord:
    """显式迁移：v0 记录 → 当前 v1 记录。

    v0 申报没有来源渠道信息，迁移时统一标注为旧版迁移来源，使每条当前
    记录的来源可追溯。标识与时间含义保持不变（README 约束）。
    """
    if record.schema_version != 0:
        raise ValueError(f"migrate_v0_to_v1 只接受 v0 记录，收到 v{record.schema_version}")
    return DomainRecord(
        schema_version=1,
        record_id=record.record_id,
        domain=record.domain,
        occurred_at=record.occurred_at,
        revision=record.revision,
        source=record.source or "legacy-v0-migration",
    )
