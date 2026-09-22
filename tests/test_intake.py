import tempfile
import unittest

import helpers
from menu_review import ArchiveStore, IntakeService, RecordingNotifier, format_batch

FIXED_CLOCK = lambda: "2026-09-22T09:00:00+08:00"  # noqa: E731


class IntakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ArchiveStore(self.tmp.name)
        self.notifier = RecordingNotifier()
        self.service = IntakeService(self.store, self.notifier, clock=FIXED_CLOCK)

    def test_batch_order_and_isolation(self):
        items = [
            ("01-正常.json", helpers.GOOD_V1),
            ("02-截断.json", '{"schema_version": 1,'),
            ("03-旧版.json", helpers.LEGACY_V0),
            ("04-负修订.json", helpers.claim(record_id="claim-003", revision=-2)),
            ("05-未来.json", helpers.FUTURE_V2),
        ]
        outcomes = self.service.intake_batch(items)

        # 界面按原顺序显示每份结果
        self.assertEqual([o.source_name for o in outcomes], [name for name, _ in items])
        self.assertEqual(
            [o.status for o in outcomes],
            ["archived", "rejected", "archived", "rejected", "pending_upgrade"],
        )
        # 坏文件不牵连好文件：好文件拿到连续归档号
        self.assertEqual(outcomes[0].archive_id, "AR-000001")
        self.assertEqual(outcomes[2].archive_id, "AR-000002")
        self.assertEqual(outcomes[4].archive_id, "AR-000003")
        # 旧版经显式迁移归档
        self.assertEqual(outcomes[2].migrated_from, 0)
        # 每份结果都告诉审核员该补材料还是升级系统
        self.assertEqual(outcomes[1].action, "fix_submission")
        self.assertEqual(outcomes[3].action, "fix_submission")
        self.assertIn("invalid_revision", [i.code for i in outcomes[3].issues])
        self.assertEqual(outcomes[4].action, "upgrade_system")
        # 三份受理各发一条通知
        self.assertEqual(len(self.notifier.delivered), 3)

        report = format_batch(outcomes)
        self.assertEqual(len(report.splitlines()), 5)
        positions = [report.index(name) for name, _ in items]
        self.assertEqual(positions, sorted(positions))

    def test_identical_reupload_returns_existing_archive(self):
        first = self.service.intake_one("a.json", helpers.GOOD_V1)
        second = self.service.intake_one("a-再次.json", helpers.GOOD_V1)
        self.assertEqual(first.status, "archived")
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(second.archive_id, first.archive_id)
        self.assertEqual(len(self.notifier.delivered), 1)  # 不重复通知

    def test_migrated_and_current_same_content_dedup(self):
        legacy = self.service.intake_one("legacy.json", helpers.LEGACY_V0)
        # 同一声明的现行版本（与迁移结果一致）再次上传 → 仍是重复
        current = helpers.claim(record_id="claim-000", occurred_at="2026-09-15T09:30:00+08:00")
        again = self.service.intake_one("current.json", current)
        self.assertEqual(again.status, "duplicate")
        self.assertEqual(again.archive_id, legacy.archive_id)

    def test_conflict_opens_review_without_rewriting_history(self):
        original = self.service.intake_one("v1.json", helpers.claim(revision=1))
        changed = self.service.intake_one("v2.json", helpers.claim(revision=2))
        self.assertEqual(changed.status, "conflict_review")
        self.assertEqual(changed.action, "review")
        self.assertIsNotNone(changed.review_id)

        # 历史不改写：归档里仍是 revision=1
        entry = self.store.find_record("claim-001")
        self.assertEqual(entry["payload"]["revision"], 1)
        self.assertEqual(entry["archive_id"], original.archive_id)

        # 复核案件登记了冲突候选
        review = self.store.find_review("claim-001")
        self.assertEqual(review["status"], "open")
        self.assertEqual(len(review["candidates"]), 1)
        self.assertEqual(review["candidates"][0]["payload"]["revision"], 2)

        # 相同冲突内容再次上传不重复建案
        again = self.service.intake_one("v2-again.json", helpers.claim(revision=2))
        self.assertEqual(again.status, "conflict_review")
        self.assertEqual(again.review_id, changed.review_id)
        self.assertEqual(len(self.store.find_review("claim-001")["candidates"]), 1)

        # 与归档原文一致的上传仍是重复，直接返回既有归档号
        dup = self.service.intake_one("v1-again.json", helpers.claim(revision=1))
        self.assertEqual(dup.status, "duplicate")
        self.assertEqual(dup.archive_id, original.archive_id)

    def test_future_version_preserved_verbatim(self):
        outcome = self.service.intake_one("future.json", helpers.FUTURE_V2)
        self.assertEqual(outcome.status, "pending_upgrade")
        entry = self.store.find_record("claim-002")
        self.assertEqual(entry["status"], "pending_upgrade")
        self.assertEqual(entry["schema_version"], 2)
        self.assertEqual(entry["raw_text"], helpers.FUTURE_V2)  # 原文原样保全

    def test_unknown_field_rejected_without_exception(self):
        outcome = self.service.intake_one("note.json", helpers.claim(supplier_note="供应商备注"))
        self.assertEqual(outcome.status, "rejected")
        self.assertIn("unknown_field", [i.code for i in outcome.issues])
        self.assertIsNone(self.store.find_record("claim-001"))  # 未写入归档

    def test_negative_revision_rejected_not_archived(self):
        outcome = self.service.intake_one("neg.json", helpers.claim(revision=-5))
        self.assertEqual(outcome.status, "rejected")
        self.assertIn("invalid_revision", [i.code for i in outcome.issues])
        self.assertIsNone(self.store.find_record("claim-001"))


if __name__ == "__main__":
    unittest.main()
