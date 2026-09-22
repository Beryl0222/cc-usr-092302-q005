"""测试共享的样例声明与路径设置。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

FIXTURES = Path(__file__).parents[1] / "fixtures"


def claim_dict(**overrides):
    payload = {
        "schema_version": 1,
        "record_id": "claim-001",
        "domain": "menu_review",
        "occurred_at": "2026-09-21T08:00:00+08:00",
        "revision": 1,
        "source": "门店申报",
    }
    payload.update(overrides)
    return payload


def claim(**overrides):
    return json.dumps(claim_dict(**overrides), ensure_ascii=False)


GOOD_V1 = claim()

LEGACY_V0 = json.dumps({
    "schema_version": 0,
    "record_id": "claim-000",
    "domain": "menu_review",
    "declared_at": "2026-09-15T09:30:00+08:00",
    "source": "门店申报",
}, ensure_ascii=False)

FUTURE_V2 = json.dumps({
    "schema_version": 2,
    "record_id": "claim-002",
    "domain": "menu_review",
    "occurred_at": "2026-09-21T09:00:00+08:00",
    "revision": 1,
    "source": "门店申报",
    "supplier_note": "未来版本引入的新字段",
}, ensure_ascii=False)
