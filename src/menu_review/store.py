"""归档存储：一份记录一个文件，通知状态与记录同文件原子写入。

记录文件一旦写入即不可变（历史不改写），唯一允许的变化是通知状态从
pending 推进到 sent；复核案件单独存放，候选冲突只增不减。停机后只需
用同一目录重新构造 ArchiveStore，即可从磁盘恢复全部状态。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


@dataclass(frozen=True)
class NotificationRef:
    """一条待发送通知的引用，notification_id 即幂等键。"""

    notification_id: str
    kind: str
    owner_type: str  # "record" | "review"
    owner: str  # record_id
    payload: dict


class ArchiveStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.records_dir = self.root / "records"
        self.reviews_dir = self.root / "reviews"
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.reviews_dir.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, dict] = {}
        self._reviews: dict[str, dict] = {}
        self._load()

    # ---- 查询 ----

    def find_record(self, record_id: str) -> dict | None:
        return self._records.get(record_id)

    def find_review(self, record_id: str) -> dict | None:
        return self._reviews.get(record_id)

    def pending_notifications(self) -> list[NotificationRef]:
        refs = [self._ref_for(entry, "record") for entry in self._records.values()
                if entry["notification"]["state"] != "sent"]
        refs += [self._ref_for(review, "review") for review in self._reviews.values()
                 if review["notification"]["state"] != "sent"]
        return sorted(refs, key=lambda ref: ref.notification_id)

    def notification_ref(self, owner_type: str, record_id: str) -> NotificationRef | None:
        owner = self._records.get(record_id) if owner_type == "record" else self._reviews.get(record_id)
        if owner is None:
            return None
        return self._ref_for(owner, owner_type)

    # ---- 变更 ----

    def add_record(self, *, record_id, status, schema_version, payload, raw_text,
                   content_hash, source_name, migrated_from, received_at) -> dict:
        entry = {
            "archive_id": self._next_id("AR", [e["archive_id"] for e in self._records.values()]),
            "record_id": record_id,
            "status": status,
            "schema_version": schema_version,
            "migrated_from": migrated_from,
            "content_hash": content_hash,
            "payload": payload,
            "raw_text": raw_text,
            "received_from": source_name,
            "received_at": received_at,
            "notification": {"id": self._next_notification_id(), "kind": f"record_{status}", "state": "pending"},
        }
        self._store(self._records, self.records_dir, entry)
        return entry

    def open_or_update_review(self, *, record_id, archived_hash, candidate, received_at) -> tuple[dict, bool]:
        """同记录号内容变化时开启（或追加）复核；返回 (复核案件, 是否有新变化)。"""
        review = self._reviews.get(record_id)
        if review is not None and any(c["content_hash"] == candidate["content_hash"] for c in review["candidates"]):
            return review, False  # 相同冲突内容已登记，不重复建案
        created = review is None
        if created:
            review = {
                "review_id": self._next_id("RV", [r["review_id"] for r in self._reviews.values()]),
                "record_id": record_id,
                "status": "open",
                "archived_hash": archived_hash,
                "candidates": [],
            }
        review["candidates"].append(candidate)
        review["updated_at"] = received_at
        review["notification"] = {
            "id": self._next_notification_id(),
            "kind": "review_opened" if created else "review_updated",
            "state": "pending",
        }
        self._store(self._reviews, self.reviews_dir, review)
        return review, True

    def mark_notification_sent(self, ref: NotificationRef) -> None:
        index = self._records if ref.owner_type == "record" else self._reviews
        directory = self.records_dir if ref.owner_type == "record" else self.reviews_dir
        owner = index.get(ref.owner)
        if owner is None or owner["notification"]["id"] != ref.notification_id:
            return  # 通知已被更新的通知取代，无需标记
        owner["notification"]["state"] = "sent"
        self._store(index, directory, owner)

    # ---- 内部 ----

    def _load(self) -> None:
        for path in sorted(self.records_dir.glob("*.json")):
            entry = json.loads(path.read_text(encoding="utf-8"))
            self._records[entry["record_id"]] = entry
        for path in sorted(self.reviews_dir.glob("*.json")):
            review = json.loads(path.read_text(encoding="utf-8"))
            self._reviews[review["record_id"]] = review

    def _store(self, index, directory, entry) -> None:
        self._write_atomic(directory / f"{quote(entry['record_id'], safe='')}.json", entry)
        index[entry["record_id"]] = entry

    @staticmethod
    def _write_atomic(path: Path, data: dict) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _ref_for(owner: dict, owner_type: str) -> NotificationRef:
        notification = owner["notification"]
        if owner_type == "record":
            payload = {"archive_id": owner["archive_id"], "record_id": owner["record_id"], "status": owner["status"]}
        else:
            payload = {
                "review_id": owner["review_id"],
                "record_id": owner["record_id"],
                "candidate_count": len(owner["candidates"]),
            }
        return NotificationRef(notification["id"], notification["kind"], owner_type, owner["record_id"], payload)

    def _next_notification_id(self) -> str:
        ids = [e["notification"]["id"] for e in self._records.values()]
        ids += [r["notification"]["id"] for r in self._reviews.values()]
        return self._next_id("NT", ids)

    @staticmethod
    def _next_id(prefix: str, existing) -> str:
        seqs = [int(item.rsplit("-", 1)[1]) for item in existing]
        return f"{prefix}-{(max(seqs, default=0) + 1):06d}"
