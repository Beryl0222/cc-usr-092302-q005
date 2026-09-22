"""面向审核员界面的结果呈现：严格按拖入顺序，每份文件一行。"""

from __future__ import annotations

from .intake import (
    STATUS_ARCHIVED,
    STATUS_CONFLICT_REVIEW,
    STATUS_DUPLICATE,
    STATUS_ERROR,
    STATUS_PENDING_UPGRADE,
    STATUS_REJECTED,
    IntakeOutcome,
)
from .issues import ACTION_FIX_SUBMISSION, ACTION_NONE, ACTION_REVIEW, ACTION_UPGRADE_SYSTEM

_STATUS_LABELS = {
    STATUS_ARCHIVED: "已归档",
    STATUS_DUPLICATE: "重复上传，沿用既有归档号",
    STATUS_CONFLICT_REVIEW: "同记录号内容冲突，已开启复核",
    STATUS_PENDING_UPGRADE: "来自未来版本，原文已保全，等待系统升级",
    STATUS_REJECTED: "拒收",
    STATUS_ERROR: "处理异常",
}

_ACTION_LABELS = {
    ACTION_FIX_SUBMISSION: "请补正材料后重新提交",
    ACTION_UPGRADE_SYSTEM: "请升级系统后再处理",
    ACTION_REVIEW: "请等待复核结论",
    ACTION_NONE: "无需进一步操作",
}


def format_outcome(outcome: IntakeOutcome) -> str:
    parts = [f"{outcome.source_name}：{_STATUS_LABELS[outcome.status]}"]
    if outcome.archive_id:
        parts.append(f"归档号 {outcome.archive_id}")
    if outcome.review_id:
        parts.append(f"复核号 {outcome.review_id}")
    if outcome.migrated_from is not None:
        parts.append(f"已从 v{outcome.migrated_from} 显式迁移")
    line = "，".join(parts)
    if outcome.issues:
        line += "；" + "；".join(issue.message for issue in outcome.issues)
    return f"{line}。{_ACTION_LABELS[outcome.action]}。"


def format_batch(outcomes) -> str:
    """批量结果按拖入顺序逐行展示。"""
    return "\n".join(format_outcome(outcome) for outcome in outcomes)
