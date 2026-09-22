"""菜单声明正式收件流程。

把 :mod:`menu_review.validation` 的评估结论落成审核员可感知的最终状态，
并保证四件事：

1. **批量保序、相互隔离**——一次拖入多份文件，结果严格按拖入顺序返回，
   任何一份坏文件都不影响其它文件收件；
2. **幂等去重**——完全相同的再次上传直接返回既有归档号，不产生第二份
   归档、不产生第二次通知；
3. **只增不改**——同一记录号内容发生变化时开启复核，历史归档原样保留；
4. **停机可恢复**——归档与通知之间采用 outbox：归档与待发通知在同一
   事务边界落库，恢复任务只补做尚未完成的通知，且通知按归档号幂等去重，
   绝不重复发送。

未来版本的声明不在此解读，原文逐字节封存入等待升级区。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable, Protocol

from .contracts import DomainRecord
from .validation import (
    CURRENT,
    MIGRATED,
    PENDING_UPGRADE,
    REJECTED,
    RecordEvaluation,
    evaluate_text,
)

# 收件最终状态
ACCEPTED = "accepted"
ACCEPTED_DUPLICATE = "accepted_duplicate"
REVIEW = "review"
MIGRATED_STATUS = "migrated"
REJECTED_STATUS = "rejected"
PENDING_STATUS = "pending_upgrade"


@dataclass(frozen=True)
class SubmissionResult:
    """单份文件收件的最终状态（界面直接渲染 :meth:`describe`）。"""

    sequence: int
    source_name: str
    status: str
    issue: object | None = None
    record_id: str | None = None
    archive_id: str | None = None
    warnings: tuple[object, ...] = ()
    raw_preserved: bool = False
    review_id: str | None = None
    pending_id: str | None = None
    notified: bool = False
    version: int | None = None

    @property
    def ok(self) -> bool:
        """该份文件是否完成收件（含重复沿用与进入复核）。"""
        return self.status in (
            ACCEPTED,
            ACCEPTED_DUPLICATE,
            MIGRATED_STATUS,
            REVIEW,
        )

    def describe(self) -> str:
        """供审核员界面展示的一行中文结论。"""
        label = {
            ACCEPTED: "正式归档",
            ACCEPTED_DUPLICATE: "重复申报，沿用既有归档号",
            MIGRATED_STATUS: "旧版声明已迁移为当前版本后归档",
            REVIEW: "内容变化，已开启复核（历史记录未改写）",
            REJECTED_STATUS: "不予收件",
            PENDING_STATUS: "来自未来版本，原文已保全，等待系统升级",
        }[self.status]
        parts = [f"{self.sequence}. {self.source_name}：{label}"]
        if self.archive_id:
            parts[0] += f"（归档号 {self.archive_id}）"
        if self.issue is not None:
            parts.append(self.issue.describe())
        parts.extend(w.describe() for w in self.warnings)
        if self.review_id:
            parts.append(f"复核单号 {self.review_id}")
        if self.pending_id:
            parts.append(f"等待升级封存件 {self.pending_id}")
        if self.notified:
            parts.append("通知已送达")
        elif self.ok:
            parts.append("通知待发送（停机恢复时自动补做）")
        return "；".join(parts)


class Notifier(Protocol):
    """外部通知通道（审核员/申报方）。发送失败可抛出任意异常。"""

    def send(self, event_type: str, reference: str, detail: str) -> None: ...


def canonical_fingerprint(record: DomainRecord) -> str:
    """当前记录的规范化指纹，用于"完全相同"判定。"""
    payload = {
        "schema_version": record.schema_version,
        "record_id": record.record_id,
        "domain": record.domain,
        "occurred_at": record.occurred_at,
        "revision": record.revision,
        "source": record.source,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def raw_fingerprint(text: str) -> str:
    """原文指纹，用于等待升级区去重。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class _ArchiveEntry:
    archive_id: str
    record_id: str
    record: DomainRecord
    fingerprint: str


@dataclass
class _OutboxItem:
    event_type: str  # "archived" | "review_opened"
    reference: str
    detail: str
    sent: bool = False


class ArchiveRepository:
    """归档存储。

    生产环境中这些结构是持久化表/对象存储；这里给出内存实现，但严格遵守
    同一边界：:meth:`archive_with_outbox` 把归档写入与待发通知作为一个
    追加动作完成，之后通知发送即使失败/停机也不会丢动作，且不会重复归档。
    所有历史只追加、不改写。
    """

    def __init__(self) -> None:
        self._entries: list[_ArchiveEntry] = []
        self._by_fingerprint: dict[str, str] = {}
        self._by_record: dict[str, list[str]] = {}
        self._reviews: dict[str, dict] = {}
        self._review_by_fingerprint: dict[str, str] = {}
        self._pending_vault: dict[str, dict] = {}
        self._pending_by_raw: dict[str, str] = {}
        self._outbox: dict[str, _OutboxItem] = {}
        self._archive_seq = 0
        self._review_seq = 0
        self._pending_seq = 0

    # ---- 查询 -------------------------------------------------------------

    def find_by_fingerprint(self, fingerprint: str) -> _ArchiveEntry | None:
        archive_id = self._by_fingerprint.get(fingerprint)
        return self._entry(archive_id) if archive_id else None

    def latest_by_record_id(self, record_id: str) -> _ArchiveEntry | None:
        ids = self._by_record.get(record_id)
        return self._entry(ids[-1]) if ids else None

    def find_review_by_fingerprint(self, fingerprint: str) -> str | None:
        """内容完全相同的变更此前是否已开过复核单。"""
        return self._review_by_fingerprint.get(fingerprint)

    def _entry(self, archive_id: str) -> _ArchiveEntry:
        return next(entry for entry in self._entries if entry.archive_id == archive_id)

    # ---- 写入（均为只追加） -----------------------------------------------

    def archive_with_outbox(
        self, record: DomainRecord, fingerprint: str, migrated: bool
    ) -> _ArchiveEntry:
        """归档 + 同事务写入通知 outbox。返回新建归档条目。"""
        existing = self._by_fingerprint.get(fingerprint)
        if existing:
            return self._entry(existing)
        self._archive_seq += 1
        archive_id = f"ARC-{self._archive_seq:04d}"
        entry = _ArchiveEntry(archive_id, record.record_id, record, fingerprint)
        self._entries.append(entry)
        self._by_fingerprint[fingerprint] = archive_id
        self._by_record.setdefault(record.record_id, []).append(archive_id)
        kind = "（由 v0 迁移）" if migrated else ""
        self._outbox[archive_id] = _OutboxItem(
            "archived",
            archive_id,
            f"记录 {record.record_id} 已归档{kind}",
        )
        return entry

    def open_review_with_outbox(
        self, new_record: DomainRecord, prior_archive_id: str, raw: str
    ) -> str:
        """开启复核单并保存新原文；历史归档不动。同时写入通知 outbox。"""
        self._review_seq += 1
        review_id = f"REV-{self._review_seq:04d}"
        self._reviews[review_id] = {
            "record_id": new_record.record_id,
            "new_record": new_record,
            "new_raw": raw,
            "prior_archive_id": prior_archive_id,
        }
        self._review_by_fingerprint[
            canonical_fingerprint(new_record)
        ] = review_id
        self._outbox[review_id] = _OutboxItem(
            "review_opened",
            review_id,
            f"记录 {new_record.record_id} 与归档 {prior_archive_id} 内容不一致，待复核",
        )
        return review_id

    def preserve_future_raw(self, version: int, raw: str) -> str:
        """未来版本原文逐字节封存（同一原文只存一份）。"""
        digest = raw_fingerprint(raw)
        existing = self._pending_by_raw.get(digest)
        if existing:
            return existing
        self._pending_seq += 1
        pending_id = f"PEND-{self._pending_seq:04d}"
        self._pending_vault[pending_id] = {"version": version, "raw": raw, "digest": digest}
        self._pending_by_raw[digest] = pending_id
        return pending_id

    def preserved_raw(self, pending_id: str) -> str:
        return self._pending_vault[pending_id]["raw"]

    # ---- outbox / 恢复 ----------------------------------------------------

    def pending_outbox(self) -> list[_OutboxItem]:
        """按引用号排序返回所有尚未完成的动作。"""
        return [item for item in self._outbox.values() if not item.sent]

    def mark_sent(self, reference: str) -> None:
        if reference in self._outbox:
            self._outbox[reference].sent = True


class IngestionService:
    """收件应用服务：评估 → 归档/复核/封存 → 通知。"""

    def __init__(self, notifier: Notifier | None = None) -> None:
        self.repo = ArchiveRepository()
        self._notifier = notifier

    def ingest_batch(
        self, files: Iterable[tuple[str, str]]
    ) -> list[SubmissionResult]:
        """按拖入顺序逐份收件；单份异常不牵连其它文件。"""
        results: list[SubmissionResult] = []
        for index, (name, text) in enumerate(files, start=1):
            results.append(self._ingest_one(index, name, text))
        return results

    def ingest_one(self, name: str, text: str, sequence: int = 1) -> SubmissionResult:
        return self._ingest_one(sequence, name, text)

    def _ingest_one(self, sequence: int, name: str, text: str) -> SubmissionResult:
        # 评估阶段本身不抛异常；这里再兜一层，保证坏文件绝不拖垮整批。
        try:
            evaluation = evaluate_text(text)
        except Exception as exc:  # pragma: no cover - 纯防御
            return SubmissionResult(
                sequence=sequence,
                source_name=name,
                status=REJECTED_STATUS,
                issue=_internal_error(exc),
            )

        if evaluation.outcome == REJECTED:
            return SubmissionResult(
                sequence=sequence,
                source_name=name,
                status=REJECTED_STATUS,
                issue=evaluation.primary_issue,
                version=evaluation.version,
            )

        if evaluation.outcome == PENDING_UPGRADE:
            pending_id = self.repo.preserve_future_raw(evaluation.version, text)
            return SubmissionResult(
                sequence=sequence,
                source_name=name,
                status=PENDING_STATUS,
                issue=evaluation.primary_issue,
                raw_preserved=True,
                pending_id=pending_id,
                version=evaluation.version,
            )

        return self._accept(sequence, name, text, evaluation)

    def _accept(
        self,
        sequence: int,
        name: str,
        text: str,
        evaluation: RecordEvaluation,
    ) -> SubmissionResult:
        record = evaluation.record
        fingerprint = canonical_fingerprint(record)
        migrated = evaluation.outcome == MIGRATED
        warnings = evaluation.warnings

        # 1) 完全相同 → 直接沿用既有归档号，不新增归档、不新增通知。
        duplicate = self.repo.find_by_fingerprint(fingerprint)
        if duplicate is not None:
            already_sent = not self._notification_pending(duplicate.archive_id)
            return SubmissionResult(
                sequence=sequence,
                source_name=name,
                status=ACCEPTED_DUPLICATE,
                record_id=record.record_id,
                archive_id=duplicate.archive_id,
                warnings=tuple(warnings),
                notified=already_sent,
                version=1,
            )

        # 2) 同记录号但内容变化 → 开复核，历史不改写；
        #    完全相同的变更此前已开过复核单的，直接沿用，不重复开单。
        prior = self.repo.latest_by_record_id(record.record_id)
        if prior is not None:
            existing_review = self.repo.find_review_by_fingerprint(fingerprint)
            if existing_review is not None:
                already_sent = not self._notification_pending(existing_review)
                return SubmissionResult(
                    sequence=sequence,
                    source_name=name,
                    status=REVIEW,
                    record_id=record.record_id,
                    archive_id=prior.archive_id,
                    review_id=existing_review,
                    notified=already_sent,
                    version=1,
                )
            review_id = self.repo.open_review_with_outbox(
                record, prior.archive_id, text
            )
            notified = self._dispatch(review_id)
            return SubmissionResult(
                sequence=sequence,
                source_name=name,
                status=REVIEW,
                record_id=record.record_id,
                archive_id=prior.archive_id,
                review_id=review_id,
                notified=notified,
                version=1,
            )

        # 3) 正常归档（归档与待发通知在同一事务边界落库）。
        entry = self.repo.archive_with_outbox(record, fingerprint, migrated)
        notified = self._dispatch(entry.archive_id)
        return SubmissionResult(
            sequence=sequence,
            source_name=name,
            status=MIGRATED_STATUS if migrated else ACCEPTED,
            record_id=record.record_id,
            archive_id=entry.archive_id,
            warnings=tuple(warnings),
            notified=notified,
            version=1,
        )

    # ---- 通知与恢复 -------------------------------------------------------

    def _notification_pending(self, reference: str) -> bool:
        return any(item.reference == reference for item in self.repo.pending_outbox())

    def _dispatch(self, reference: str) -> bool:
        """尝试发送一份 outbox 通知；失败（含停机）时保持未完成状态。

        返回是否已确认送达。幂等性由 outbox（按归档号/复核单号去重）保证：
        恢复时只补做未完成项，已完成动作绝不重复。
        """
        item = next(
            (item for item in self.repo.pending_outbox() if item.reference == reference),
            None,
        )
        if item is None:
            return True
        if self._notifier is None:
            # 未配置外部通道时视为本地收件场景：直接标记完成。
            self.repo.mark_sent(reference)
            return True
        try:
            self._notifier.send(item.event_type, item.reference, item.detail)
        except Exception:
            # 停机/通道故障：动作留在 outbox，等待 recover_pending_actions。
            return False
        self.repo.mark_sent(reference)
        return True

    def recover_pending_actions(self) -> list[str]:
        """停机恢复：只补做未完成的通知动作，返回本次实际补做的引用号。

        可安全重复调用：已送达的动作不会再次发送。
        """
        recovered: list[str] = []
        for item in self.repo.pending_outbox():
            if self._dispatch(item.reference):
                recovered.append(item.reference)
        return recovered


def _internal_error(exc: Exception) -> object:
    from .issues import ESCALATE, Issue

    return Issue(
        "INTERNAL_ERROR",
        f"读取过程中发生系统错误：{exc!r}",
        ESCALATE,
    )
