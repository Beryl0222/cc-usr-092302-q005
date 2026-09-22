"""收件服务：批量接收、幂等归档、冲突复核与停机恢复。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from . import reading as rd
from .issues import (
    ACTION_FIX_SUBMISSION,
    ACTION_NONE,
    ACTION_REVIEW,
    ACTION_UPGRADE_SYSTEM,
    INTERNAL_ERROR,
    Issue,
)
from .notifier import Notifier
from .store import ArchiveStore, NotificationRef

STATUS_ARCHIVED = "archived"
STATUS_DUPLICATE = "duplicate"
STATUS_CONFLICT_REVIEW = "conflict_review"
STATUS_PENDING_UPGRADE = "pending_upgrade"
STATUS_REJECTED = "rejected"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class IntakeOutcome:
    """单份文件的收件结果，携带审核员下一步动作提示。"""

    source_name: str
    status: str
    action: str
    record_id: str | None = None
    archive_id: str | None = None
    review_id: str | None = None
    migrated_from: int | None = None
    issues: tuple[Issue, ...] = ()


@dataclass(frozen=True)
class RecoveryReport:
    """恢复任务结果：补做了哪些通知、哪些仍未完成。"""

    completed: tuple[str, ...]
    remaining: tuple[str, ...]


class IntakeService:
    def __init__(self, store: ArchiveStore, notifier: Notifier, clock: Callable[[], str] | None = None):
        self.store = store
        self.notifier = notifier
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    def intake_batch(self, items: Iterable[tuple[str, str]]) -> list[IntakeOutcome]:
        """按拖入顺序逐份处理；单份失败不牵连其余文件。"""
        outcomes = []
        for source_name, text in items:
            try:
                outcomes.append(self.intake_one(source_name, text))
            except Exception as exc:  # 坏文件不牵连好文件
                outcomes.append(IntakeOutcome(
                    source_name=source_name,
                    status=STATUS_ERROR,
                    action=ACTION_NONE,
                    issues=(Issue(INTERNAL_ERROR, f"处理 {source_name} 时出现内部错误：{exc}"),),
                ))
        return outcomes

    def intake_one(self, source_name: str, text: str) -> IntakeOutcome:
        reading = rd.read_declaration(text)
        if reading.status == rd.STATUS_INVALID:
            return IntakeOutcome(
                source_name=source_name,
                status=STATUS_REJECTED,
                action=ACTION_FIX_SUBMISSION,
                record_id=reading.record_id,
                issues=reading.issues,
            )

        content_hash = _content_hash(reading.payload)
        existing = self.store.find_record(reading.record_id)
        if existing is not None:
            if existing["content_hash"] == content_hash:
                # 完全相同的再次上传：直接返回既有归档号。
                return IntakeOutcome(
                    source_name=source_name,
                    status=STATUS_DUPLICATE,
                    action=ACTION_NONE,
                    record_id=reading.record_id,
                    archive_id=existing["archive_id"],
                    migrated_from=reading.migrated_from,
                )
            # 同记录号内容变化：开启复核，不改写历史。
            candidate = {
                "content_hash": content_hash,
                "schema_version": reading.schema_version,
                "payload": reading.payload,
                "raw_text": reading.raw_text,
                "received_from": source_name,
                "received_at": self._clock(),
            }
            review, changed = self.store.open_or_update_review(
                record_id=reading.record_id,
                archived_hash=existing["content_hash"],
                candidate=candidate,
                received_at=self._clock(),
            )
            if changed:
                self._deliver(self.store.notification_ref("review", reading.record_id))
            return IntakeOutcome(
                source_name=source_name,
                status=STATUS_CONFLICT_REVIEW,
                action=ACTION_REVIEW,
                record_id=reading.record_id,
                archive_id=existing["archive_id"],
                review_id=review["review_id"],
                migrated_from=reading.migrated_from,
            )

        status = STATUS_PENDING_UPGRADE if reading.status == rd.STATUS_PENDING_UPGRADE else STATUS_ARCHIVED
        entry = self.store.add_record(
            record_id=reading.record_id,
            status=status,
            schema_version=reading.schema_version,
            payload=reading.payload,
            raw_text=reading.raw_text,
            content_hash=content_hash,
            source_name=source_name,
            migrated_from=reading.migrated_from,
            received_at=self._clock(),
        )
        self._deliver(self.store.notification_ref("record", reading.record_id))
        return IntakeOutcome(
            source_name=source_name,
            status=status,
            action=ACTION_UPGRADE_SYSTEM if status == STATUS_PENDING_UPGRADE else ACTION_NONE,
            record_id=reading.record_id,
            archive_id=entry["archive_id"],
            migrated_from=reading.migrated_from,
        )

    def recover(self) -> RecoveryReport:
        """停机恢复：只补做未完成的通知，不重归档、不重发已完成通知。"""
        completed: list[str] = []
        remaining: list[str] = []
        for ref in self.store.pending_notifications():
            if self._deliver(ref):
                completed.append(ref.notification_id)
            else:
                remaining.append(ref.notification_id)
        return RecoveryReport(completed=tuple(completed), remaining=tuple(remaining))

    def _deliver(self, ref: NotificationRef | None) -> bool:
        if ref is None:
            return False
        try:
            self.notifier.send(ref.notification_id, ref.kind, ref.payload)
        except Exception:
            return False  # 通知未完成，状态保持 pending，等待恢复任务补做
        # 若在发送成功、标记落盘之前停机，恢复时会以同一通知 id 重发，
        # 由幂等消费方去重。
        self.store.mark_notification_sent(ref)
        return True


def _content_hash(payload) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()
