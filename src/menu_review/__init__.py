"""多语菜单安全译审——菜单声明读取与正式收件。

向后兼容：``DomainRecord`` 与 ``load_record`` 的签名和行为保持不变，
现有业务样例与调用方无需改动。正式收件请使用 :class:`IngestionService`
与 :func:`evaluate_text`。
"""

from .contracts import DomainRecord, load_record
from .ingest import (
    ArchiveRepository,
    IngestionService,
    SubmissionResult,
    canonical_fingerprint,
)
from .issues import Issue
from .validation import RecordEvaluation, evaluate_text, migrate_v0_to_v1

__all__ = [
    "DomainRecord",
    "load_record",
    "Issue",
    "RecordEvaluation",
    "evaluate_text",
    "migrate_v0_to_v1",
    "IngestionService",
    "ArchiveRepository",
    "SubmissionResult",
    "canonical_fingerprint",
]
