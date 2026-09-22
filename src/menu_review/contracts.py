"""读取项目已确认的最小数据合同，不包含业务流程实现。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .issues import (
    ACTION_UPGRADE_SYSTEM,
    FUTURE_SCHEMA_VERSION,
    DeclarationError,
    Issue,
)


@dataclass(frozen=True)
class DomainRecord:
    schema_version: int
    record_id: str
    domain: str
    occurred_at: str
    revision: int
    source: str


def load_record(path: Path) -> DomainRecord:
    """严格读取单份声明文件并返回当前合同记录。

    旧版本声明经显式迁移后返回；无效输入抛出携带逐项问题的
    DeclarationError；来自未来版本的声明无法构造当前记录，抛出带
    upgrade_system 动作提示的 DeclarationError。
    """
    # 延迟导入：reading 依赖本模块的 DomainRecord，避免循环依赖。
    from .reading import CURRENT_SCHEMA_VERSION, STATUS_PENDING_UPGRADE, STATUS_VALID, read_declaration

    reading = read_declaration(Path(path).read_text(encoding="utf-8"))
    if reading.status == STATUS_VALID and reading.record is not None:
        return reading.record
    if reading.status == STATUS_PENDING_UPGRADE:
        raise DeclarationError([
            Issue(
                FUTURE_SCHEMA_VERSION,
                f"声明来自更新的 schema_version={reading.schema_version}，"
                f"当前系统最高支持 {CURRENT_SCHEMA_VERSION}，请升级系统",
                field="schema_version",
                action=ACTION_UPGRADE_SYSTEM,
            )
        ])
    raise DeclarationError(reading.issues)
