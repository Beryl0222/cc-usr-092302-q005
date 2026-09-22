"""声明文本的严格读取：先判断是否为完整 JSON 对象，再逐项报告字段问题。

读取分两个阶段：
1. 结构阶段——空输入、语法错误、顶层非对象、重复键、尾随内容、
   缺失标识与非法 schema_version 都在这一阶段报告；
2. 字段阶段——按声明自身的版本选择已知字段集合，分别报告未知字段、
   缺失字段、无时区时间与非法修订号。

仍受支持的旧版本经显式迁移生成当前记录；来自未来版本的声明不做字段级
解释，原文原样保全并进入等待升级状态。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .contracts import DomainRecord
from .issues import (
    DUPLICATE_KEY,
    EMPTY_INPUT,
    INVALID_JSON,
    INVALID_REVISION,
    INVALID_SCHEMA_VERSION,
    INVALID_TIMESTAMP,
    INVALID_VALUE,
    MISSING_FIELD,
    MISSING_IDENTIFIER,
    MISSING_TIMEZONE,
    NOT_JSON_OBJECT,
    TRAILING_CONTENT,
    UNKNOWN_FIELD,
    Issue,
)
from .migration import KNOWN_V0, MIGRATIONS, REQUIRED_V0

CURRENT_SCHEMA_VERSION = 1

REQUIRED_CURRENT = ("record_id", "domain", "occurred_at", "revision", "source")
KNOWN_CURRENT = frozenset(REQUIRED_CURRENT) | {"schema_version"}

STATUS_VALID = "valid"
STATUS_INVALID = "invalid"
STATUS_PENDING_UPGRADE = "pending_upgrade"


@dataclass(frozen=True)
class Reading:
    """一次读取的结果：有效（可能经迁移）、无效（附问题清单）或等待升级。"""

    status: str
    record: DomainRecord | None = None
    migrated_from: int | None = None
    issues: tuple[Issue, ...] = ()
    schema_version: int | None = None
    record_id: str | None = None
    payload: dict[str, Any] | None = None
    raw_text: str = ""


def read_declaration(text: str) -> Reading:
    """读取单份声明文本；解析问题以 Issue 列表返回，绝不抛出解析异常。"""
    stripped = text.strip()
    if not stripped:
        return _invalid([Issue(EMPTY_INPUT, "输入为空，不是完整的 JSON 对象")], text)

    duplicates: list[str] = []

    def _collect(pairs):
        obj: dict[str, Any] = {}
        for key, value in pairs:
            if key in obj:
                duplicates.append(key)
            obj[key] = value
        return obj

    decoder = json.JSONDecoder(object_pairs_hook=_collect)
    try:
        value, end = decoder.raw_decode(stripped)
    except json.JSONDecodeError as exc:
        return _invalid([Issue(INVALID_JSON, f"不是完整的 JSON 对象：{exc}")], text)

    issues: list[Issue] = []
    for key in sorted(set(duplicates)):
        issues.append(Issue(DUPLICATE_KEY, f"键 {key!r} 在对象中重复出现", field=key))
    trailing = stripped[end:]
    if trailing:
        issues.append(Issue(TRAILING_CONTENT, f"JSON 对象之后存在尾随内容：{trailing[:20]!r}"))
    if not isinstance(value, dict):
        issues.append(Issue(NOT_JSON_OBJECT, "顶层必须是 JSON 对象"))
        return _invalid(issues, text)

    payload: dict[str, Any] = value
    record_id = payload.get("record_id")
    if not isinstance(record_id, str) or not record_id.strip():
        issues.append(Issue(MISSING_IDENTIFIER, "缺少有效的 record_id 标识", field="record_id"))
        record_id = None

    version = payload.get("schema_version")
    version_ok = True
    if version is None:
        issues.append(Issue(MISSING_FIELD, "缺少必填字段 'schema_version'", field="schema_version"))
        version_ok = False
    elif isinstance(version, bool) or not isinstance(version, int) or version < 0:
        issues.append(
            Issue(INVALID_SCHEMA_VERSION, f"schema_version 必须是非负整数，收到 {version!r}", field="schema_version")
        )
        version_ok = False

    if issues:
        return _invalid(issues, text, schema_version=version if version_ok else None, record_id=record_id)

    if version > CURRENT_SCHEMA_VERSION:
        # 来自未来版本：原文原样保全，等待系统升级，不做字段级解释。
        return Reading(
            status=STATUS_PENDING_UPGRADE,
            schema_version=version,
            record_id=record_id,
            payload=payload,
            raw_text=text,
        )

    if version in MIGRATIONS:
        _validate_payload(payload, required=REQUIRED_V0, known=KNOWN_V0, time_field="declared_at",
                          check_revision=False, issues=issues)
        if issues:
            return _invalid(issues, text, schema_version=version, record_id=record_id)
        migrated = MIGRATIONS[version](payload)
        record = DomainRecord(**migrated)
        return Reading(
            status=STATUS_VALID,
            record=record,
            migrated_from=version,
            schema_version=CURRENT_SCHEMA_VERSION,
            record_id=record.record_id,
            payload=migrated,
            raw_text=text,
        )

    # version == CURRENT_SCHEMA_VERSION
    _validate_payload(payload, required=REQUIRED_CURRENT, known=KNOWN_CURRENT, time_field="occurred_at",
                      check_revision=True, issues=issues)
    if issues:
        return _invalid(issues, text, schema_version=version, record_id=record_id)
    record = DomainRecord(**payload)
    return Reading(
        status=STATUS_VALID,
        record=record,
        migrated_from=None,
        schema_version=version,
        record_id=record.record_id,
        payload=payload,
        raw_text=text,
    )


def _invalid(issues, raw_text: str, *, schema_version=None, record_id=None) -> Reading:
    return Reading(
        status=STATUS_INVALID,
        issues=tuple(issues),
        schema_version=schema_version,
        record_id=record_id,
        raw_text=raw_text,
    )


def _validate_payload(payload, *, required, known, time_field, check_revision, issues) -> None:
    for key in sorted(set(payload) - known):
        issues.append(Issue(UNKNOWN_FIELD, f"未知字段 {key!r}，当前合同不支持", field=key))
    for name in required:
        if name == "record_id":
            continue  # 标识已在结构阶段检查
        if name not in payload:
            issues.append(Issue(MISSING_FIELD, f"缺少必填字段 {name!r}", field=name))
    if "domain" in payload:
        _check_text(payload["domain"], "domain", issues)
    if time_field in payload:
        _check_timestamp(payload[time_field], time_field, issues)
    if check_revision and "revision" in payload:
        _check_revision(payload["revision"], issues)
    if "source" in payload:
        _check_text(payload["source"], "source", issues)


def _check_text(value, field, issues) -> None:
    if not isinstance(value, str) or not value.strip():
        issues.append(Issue(INVALID_VALUE, f"字段 {field!r} 必须是非空字符串，收到 {value!r}", field=field))


def _check_timestamp(value, field, issues) -> None:
    if not isinstance(value, str):
        issues.append(Issue(INVALID_TIMESTAMP, f"字段 {field!r} 必须是 ISO 8601 时间字符串，收到 {value!r}", field=field))
        return
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        issues.append(Issue(INVALID_TIMESTAMP, f"字段 {field!r} 不是合法的 ISO 8601 时间：{value!r}", field=field))
        return
    if moment.tzinfo is None or moment.utcoffset() is None:
        issues.append(Issue(MISSING_TIMEZONE, f"字段 {field!r} 缺少时区偏移：{value!r}", field=field))


def _check_revision(value, issues) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        issues.append(Issue(INVALID_REVISION, f"revision 必须是正整数，收到 {value!r}", field="revision"))
