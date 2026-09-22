import tempfile
import unittest
from pathlib import Path

import helpers
from menu_review import DeclarationError, load_record


class LoadRecordCompatTest(unittest.TestCase):
    """现有 load_record 用法和业务样例必须继续工作。"""

    def test_business_sample_still_loads(self):
        record = load_record(helpers.FIXTURES / "menu_claim.json")
        self.assertEqual(record.domain, "menu_review")
        self.assertEqual(record.record_id, "sample-010")
        self.assertGreater(record.revision, 0)

    def test_legacy_sample_migrates_to_current(self):
        record = load_record(helpers.FIXTURES / "menu_claim_legacy_v0.json")
        self.assertEqual(record.schema_version, 1)
        self.assertEqual(record.revision, 1)
        self.assertEqual(record.occurred_at, "2026-09-18T10:30:00+08:00")
        self.assertEqual(record.record_id, "sample-009")

    def test_unknown_field_raises_structured_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.json"
            path.write_text(helpers.claim(supplier_note="供应商备注"), encoding="utf-8")
            with self.assertRaises(DeclarationError) as ctx:
                load_record(path)
        self.assertIn("unknown_field", [i.code for i in ctx.exception.issues])

    def test_negative_revision_raises_structured_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "neg.json"
            path.write_text(helpers.claim(revision=-1), encoding="utf-8")
            with self.assertRaises(DeclarationError) as ctx:
                load_record(path)
        self.assertIn("invalid_revision", [i.code for i in ctx.exception.issues])

    def test_future_version_tells_upgrade_system(self):
        with self.assertRaises(DeclarationError) as ctx:
            load_record(helpers.FIXTURES / "menu_claim_future_v2.json")
        issue = ctx.exception.issues[0]
        self.assertEqual(issue.code, "future_schema_version")
        self.assertEqual(issue.action, "upgrade_system")


if __name__ == "__main__":
    unittest.main()
