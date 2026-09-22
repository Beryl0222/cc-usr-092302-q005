"""领域数据合同与菜单声明收件能力。"""

from .contracts import DomainRecord, load_record
from .intake import IntakeOutcome, IntakeService, RecoveryReport
from .issues import DeclarationError, Issue
from .notifier import Notifier, RecordingNotifier
from .reading import CURRENT_SCHEMA_VERSION, Reading, read_declaration
from .report import format_batch, format_outcome
from .store import ArchiveStore, NotificationRef

__all__ = [
    "ArchiveStore",
    "CURRENT_SCHEMA_VERSION",
    "DeclarationError",
    "DomainRecord",
    "IntakeOutcome",
    "IntakeService",
    "Issue",
    "NotificationRef",
    "Notifier",
    "Reading",
    "RecordingNotifier",
    "RecoveryReport",
    "format_batch",
    "format_outcome",
    "load_record",
    "read_declaration",
]
