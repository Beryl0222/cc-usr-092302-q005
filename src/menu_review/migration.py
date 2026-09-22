"""旧版本声明到当前合同的显式迁移。

schema_version=0 的声明尚未引入 revision，且时间字段名为 declared_at。
迁移必须保持既有标识和时间含义：只改字段名与补默认值，不改写内容。
"""

from __future__ import annotations

# v0 的必填字段与已知字段集合（用于版本感知的字段校验）
REQUIRED_V0 = ("record_id", "domain", "declared_at", "source")
KNOWN_V0 = frozenset(REQUIRED_V0) | {"schema_version"}


def migrate_v0_to_v1(payload: dict) -> dict:
    """v0 → v1：declared_at 更名为 occurred_at（时间含义不变），补 revision=1。"""
    return {
        "schema_version": 1,
        "record_id": payload["record_id"],
        "domain": payload["domain"],
        "occurred_at": payload["declared_at"],
        "revision": 1,
        "source": payload["source"],
    }


# 仍受支持的旧版本 → 迁移函数
MIGRATIONS = {0: migrate_v0_to_v1}
