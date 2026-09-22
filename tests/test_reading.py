import json
import unittest

import helpers
from menu_review import read_declaration


def codes(reading):
    return [issue.code for issue in reading.issues]


class StructuralStageTest(unittest.TestCase):
    """先识别输入是不是完整的 JSON 对象。"""

    def test_empty_input(self):
        reading = read_declaration("   \n")
        self.assertEqual(reading.status, "invalid")
        self.assertEqual(codes(reading), ["empty_input"])

    def test_truncated_json(self):
        reading = read_declaration('{"schema_version": 1, "record_id":')
        self.assertEqual(reading.status, "invalid")
        self.assertIn("invalid_json", codes(reading))

    def test_top_level_array_is_not_object(self):
        reading = read_declaration('[{"record_id": "x"}]')
        self.assertEqual(reading.status, "invalid")
        self.assertIn("not_json_object", codes(reading))

    def test_trailing_content(self):
        reading = read_declaration(helpers.claim() + "\n garbage")
        self.assertEqual(reading.status, "invalid")
        self.assertIn("trailing_content", codes(reading))

    def test_two_concatenated_objects(self):
        reading = read_declaration(helpers.claim() + helpers.claim())
        self.assertEqual(reading.status, "invalid")
        self.assertIn("trailing_content", codes(reading))

    def test_duplicate_key(self):
        text = '{"schema_version": 1, "record_id": "a", "record_id": "b"}'
        reading = read_declaration(text)
        self.assertEqual(reading.status, "invalid")
        self.assertIn("duplicate_key", codes(reading))
        self.assertIn("record_id", [i.field for i in reading.issues])

    def test_missing_schema_version(self):
        payload = helpers.claim_dict()
        del payload["schema_version"]
        reading = read_declaration(json.dumps(payload, ensure_ascii=False))
        self.assertEqual(reading.status, "invalid")
        self.assertIn("missing_field", codes(reading))

    def test_invalid_schema_version(self):
        for bad in (1.5, -1, "1", True):
            reading = read_declaration(helpers.claim(schema_version=bad))
            self.assertEqual(reading.status, "invalid")
            self.assertIn("invalid_schema_version", codes(reading), msg=f"schema_version={bad!r}")


class FieldStageTest(unittest.TestCase):
    """再分别报告未知字段、无时区时间、缺失标识和非法修订号。"""

    def test_unknown_field_supplier_note(self):
        reading = read_declaration(helpers.claim(supplier_note="多加葱"))
        self.assertEqual(reading.status, "invalid")
        self.assertIn("unknown_field", codes(reading))
        issue = next(i for i in reading.issues if i.code == "unknown_field")
        self.assertEqual(issue.field, "supplier_note")
        self.assertEqual(issue.action, "fix_submission")

    def test_missing_timezone(self):
        reading = read_declaration(helpers.claim(occurred_at="2026-09-21T08:00:00"))
        self.assertEqual(reading.status, "invalid")
        self.assertIn("missing_timezone", codes(reading))

    def test_invalid_timestamp(self):
        reading = read_declaration(helpers.claim(occurred_at="昨天上午"))
        self.assertEqual(reading.status, "invalid")
        self.assertIn("invalid_timestamp", codes(reading))

    def test_missing_identifier(self):
        payload = helpers.claim_dict()
        del payload["record_id"]
        reading = read_declaration(json.dumps(payload, ensure_ascii=False))
        self.assertEqual(reading.status, "invalid")
        self.assertIn("missing_identifier", codes(reading))

    def test_blank_identifier(self):
        reading = read_declaration(helpers.claim(record_id="   "))
        self.assertEqual(reading.status, "invalid")
        self.assertIn("missing_identifier", codes(reading))

    def test_illegal_revision(self):
        for bad in (-3, 0, "2", 1.5, True):
            reading = read_declaration(helpers.claim(revision=bad))
            self.assertEqual(reading.status, "invalid")
            self.assertIn("invalid_revision", codes(reading), msg=f"revision={bad!r}")

    def test_multiple_issues_reported_together(self):
        reading = read_declaration(helpers.claim(
            supplier_note="x",
            occurred_at="2026-09-21T08:00:00",
            revision=-1,
        ))
        self.assertEqual(reading.status, "invalid")
        self.assertEqual(
            set(codes(reading)),
            {"unknown_field", "missing_timezone", "invalid_revision"},
        )


class VersionTest(unittest.TestCase):
    def test_current_valid(self):
        reading = read_declaration(helpers.GOOD_V1)
        self.assertEqual(reading.status, "valid")
        self.assertIsNone(reading.migrated_from)
        self.assertEqual(reading.record.record_id, "claim-001")
        self.assertEqual(reading.record.revision, 1)

    def test_legacy_v0_migrated_explicitly(self):
        reading = read_declaration(helpers.LEGACY_V0)
        self.assertEqual(reading.status, "valid")
        self.assertEqual(reading.migrated_from, 0)
        record = reading.record
        self.assertEqual(record.schema_version, 1)
        self.assertEqual(record.revision, 1)  # 迁移补默认值
        self.assertEqual(record.occurred_at, "2026-09-15T09:30:00+08:00")  # 时间含义不变
        self.assertEqual(record.record_id, "claim-000")  # 标识不变

    def test_legacy_v0_field_set_is_version_aware(self):
        payload = json.loads(helpers.LEGACY_V0)
        payload["occurred_at"] = payload["declared_at"]  # v0 合同中不存在该字段
        reading = read_declaration(json.dumps(payload, ensure_ascii=False))
        self.assertEqual(reading.status, "invalid")
        self.assertIn("unknown_field", codes(reading))

    def test_future_version_pending_upgrade_raw_preserved(self):
        reading = read_declaration(helpers.FUTURE_V2)
        self.assertEqual(reading.status, "pending_upgrade")
        self.assertEqual(reading.schema_version, 2)
        self.assertEqual(reading.record_id, "claim-002")
        self.assertEqual(reading.raw_text, helpers.FUTURE_V2)  # 原文原样保全

    def test_future_version_still_needs_identifier(self):
        payload = json.loads(helpers.FUTURE_V2)
        del payload["record_id"]
        reading = read_declaration(json.dumps(payload, ensure_ascii=False))
        self.assertEqual(reading.status, "invalid")
        self.assertIn("missing_identifier", codes(reading))

    def test_future_version_with_trailing_content_rejected(self):
        reading = read_declaration(helpers.FUTURE_V2 + " extra")
        self.assertEqual(reading.status, "invalid")
        self.assertIn("trailing_content", codes(reading))


if __name__ == "__main__":
    unittest.main()
