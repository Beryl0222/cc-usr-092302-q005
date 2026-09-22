"""正式收件能力的可运行测试。

覆盖：
* 各类坏输入的识别与处置指引（补材料 / 升级系统）；
* v0 → v1 显式迁移；未来版本原文保全、等待升级；
* 批量保序、坏文件隔离；
* 完全相同重复上传沿用既有归档号；同记录号内容变化开复核、不改写历史；
* 归档与通知之间停机后的 outbox 恢复（只补做未完成动作、幂等）。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from menu_review import (  # noqa: E402
    DomainRecord,
    IngestionService,
    evaluate_text,
    load_record,
    migrate_v0_to_v1,
)
from menu_review.ingest import (  # noqa: E402
    ACCEPTED,
    ACCEPTED_DUPLICATE,
    MIGRATED_STATUS,
    PENDING_STATUS,
    REJECTED_STATUS,
    REVIEW,
)
from menu_review.issues import ESCALATE, RESUBMIT  # noqa: E402
from menu_review.parsing import parse_document  # noqa: E402

FIXTURES = Path(__file__).parents[1] / "fixtures"


def make_claim(**overrides) -> str:
    payload = {
        "schema_version": 1,
        "record_id": "rec-001",
        "domain": "menu_review",
        "occurred_at": "2026-09-20T09:00:00+08:00",
        "revision": 1,
        "source": "门店申报",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class FakeNotifier:
    """记录发送内容、可按引用号注入故障的通知通道。"""

    def __init__(self, fail_references=()):
        self.sent = []
        self.fail_references = set(fail_references)

    def send(self, event_type, reference, detail):
        if reference in self.fail_references:
            raise RuntimeError("通知通道停机")
        self.sent.append((event_type, reference, detail))


# --------------------------------------------------------------------------
# 第一层：完整 JSON 对象识别
# --------------------------------------------------------------------------


class ParsingTest(unittest.TestCase):
    def test_complete_object(self):
        parsed = parse_document(make_claim())
        self.assertTrue(parsed.ok)
        self.assertTrue(parsed.is_complete_object)
        self.assertEqual(parsed.trailing, "")
        self.assertEqual(parsed.duplicates, ())

    def test_trailing_content_detected(self):
        parsed = parse_document(make_claim() + " 尾随文字")
        self.assertTrue(parsed.ok)
        self.assertTrue(parsed.is_complete_object)
        self.assertEqual(parsed.trailing, "尾随文字")

    def test_duplicate_key_detected_with_lines(self):
        text = '{\n  "revision": 1,\n  "revision": 2\n}'
        parsed = parse_document(text)
        self.assertTrue(parsed.ok)
        self.assertEqual(len(parsed.duplicates), 1)
        dup = parsed.duplicates[0]
        self.assertEqual(dup.key, "revision")
        self.assertEqual((dup.first_line, dup.last_line), (2, 3))
        # 标准覆盖语义：取最后一个值
        self.assertEqual(parsed.value["revision"], 2)

    def test_same_key_in_nested_objects_is_not_duplicate(self):
        text = '{"a": {"x": 1}, "b": {"x": 2}}'
        parsed = parse_document(text)
        self.assertEqual(parsed.duplicates, ())

    def test_truncated_vs_syntax(self):
        self.assertEqual(parse_document(make_claim()[:40]).error, "truncated")
        self.assertEqual(parse_document("").error, "empty")
        self.assertEqual(parse_document("{不是json}").error, "syntax")

    def test_scalar_is_not_object(self):
        parsed = parse_document('"just a string"')
        self.assertTrue(parsed.ok)
        self.assertFalse(parsed.is_complete_object)
        self.assertFalse(parse_document("[1, 2]").is_complete_object)


# --------------------------------------------------------------------------
# 第二层：六类内容问题与处置指引
# --------------------------------------------------------------------------


class ValidationTest(unittest.TestCase):
    def test_valid_current_claim(self):
        result = evaluate_text(make_claim())
        self.assertEqual(result.outcome, "current")
        self.assertEqual(result.issues, ())
        self.assertEqual(result.record.record_id, "rec-001")

    def test_supplier_note_field_is_unknown_not_crash(self):
        # 事故复现：多了供应商备注字段，旧代码在 DomainRecord(**payload)
        # 构造阶段直接抛 TypeError；现在必须报告 UNKNOWN_FIELD。
        text = make_claim(supplier_note="供应商电话 12345")
        result = evaluate_text(text)
        self.assertEqual(result.outcome, "rejected")
        codes = [issue.code for issue in result.issues]
        self.assertIn("UNKNOWN_FIELD", codes)
        self.assertEqual(result.primary_issue.action, RESUBMIT)

    def test_negative_revision_is_illegal(self):
        result = evaluate_text(make_claim(revision=-3))
        self.assertEqual(result.outcome, "rejected")
        self.assertIn("ILLEGAL_REVISION", [i.code for i in result.issues])

    def test_revision_zero_is_legal(self):
        self.assertEqual(evaluate_text(make_claim(revision=0)).outcome, "current")

    def test_revision_float_and_bool_rejected(self):
        self.assertIn(
            "ILLEGAL_REVISION",
            [i.code for i in evaluate_text(make_claim(revision=1.5)).issues],
        )
        self.assertIn(
            "ILLEGAL_REVISION",
            [i.code for i in evaluate_text(make_claim(revision=True)).issues],
        )

    def test_naive_datetime_rejected_aware_accepted(self):
        result = evaluate_text(make_claim(occurred_at="2026-09-20T09:00:00"))
        self.assertIn("TIME_NO_TIMEZONE", [i.code for i in result.issues])
        result_z = evaluate_text(make_claim(occurred_at="2026-09-20T01:00:00Z"))
        self.assertEqual(result_z.outcome, "current")

    def test_missing_and_blank_identifier(self):
        text = json.dumps(
            {k: v for k, v in json.loads(make_claim()).items() if k != "record_id"}
        )
        self.assertIn(
            "MISSING_IDENTIFIER",
            [i.code for i in evaluate_text(text).issues],
        )
        self.assertIn(
            "MISSING_IDENTIFIER",
            [i.code for i in evaluate_text(make_claim(record_id="   ")).issues],
        )

    def test_all_problems_reported_together(self):
        # 一次告诉审核员全部问题，避免来回补材料：
        # 尾随内容、重复键（最终取到的 -1 非法）、未知字段、
        # 无时区时间、缺失标识。
        text = (
            '{"schema_version": 1, '
            '"occurred_at": "2026-09-20T09:00:00", '
            '"revision": 0, "revision": -1, "supplier_note": "x"}尾随'
        )
        codes = {i.code for i in evaluate_text(text).issues}
        self.assertEqual(
            codes,
            {
                "TRAILING_CONTENT",
                "DUPLICATE_KEY",
                "UNKNOWN_FIELD",
                "TIME_NO_TIMEZONE",
                "MISSING_IDENTIFIER",
                "ILLEGAL_REVISION",
            },
        )

    def test_duplicate_key_alone_is_still_reported(self):
        text = (
            '{"schema_version": 1, "record_id": "r", "domain": "menu_review", '
            '"occurred_at": "2026-09-20T09:00:00+08:00", '
            '"revision": -1, "revision": 1, "source": "s"}'
        )
        result = evaluate_text(text)
        self.assertEqual({i.code for i in result.issues}, {"DUPLICATE_KEY"})
        self.assertEqual(result.outcome, "rejected")

    def test_guidance_distinguishes_resubmit_from_escalation(self):
        bad = evaluate_text(make_claim(revision=-1)).primary_issue
        self.assertEqual(bad.action, RESUBMIT)
        self.assertIn("补正材料", bad.describe())

    def test_missing_version(self):
        text = json.dumps(
            {k: v for k, v in json.loads(make_claim()).items() if k != "schema_version"}
        )
        result = evaluate_text(text)
        self.assertIn("UNKNOWN_VERSION", [i.code for i in result.issues])


# --------------------------------------------------------------------------
# 版本迁移与未来版本
# --------------------------------------------------------------------------


class VersionTest(unittest.TestCase):
    V0 = json.dumps(
        {
            "schema_version": 0,
            "record_id": "legacy-7",
            "domain": "menu_review",
            "occurred_at": "2026-09-18T10:30:00+08:00",
            "revision": 2,
        },
        ensure_ascii=False,
    )

    def test_explicit_migration_function(self):
        v0 = evaluate_text(self.V0).record
        # 直接通过 v0 合同构造的记录迁移
        v1 = migrate_v0_to_v1(
            DomainRecord(0, "legacy-7", "menu_review",
                         "2026-09-18T10:30:00+08:00", 2, "")
        )
        self.assertEqual(v1.schema_version, 1)
        self.assertEqual(v1.record_id, "legacy-7")  # 标识含义不变
        self.assertEqual(v1.occurred_at, "2026-09-18T10:30:00+08:00")
        self.assertEqual(v1.source, "legacy-v0-migration")
        with self.assertRaises(ValueError):
            migrate_v0_to_v1(v1)

    def test_evaluate_migrates_v0_with_warning(self):
        result = evaluate_text(self.V0)
        self.assertEqual(result.outcome, "migrated")
        self.assertEqual(result.record.schema_version, 1)
        self.assertEqual(len(result.warnings), 1)
        self.assertEqual(result.warnings[0].code, "MIGRATED_V0_TO_V1")

    def test_v0_unknown_field_still_rejected(self):
        text = self.V0[:-1] + ', "supplier_note": "x"}'
        self.assertEqual(evaluate_text(text).outcome, "rejected")

    def test_future_version_preserved_verbatim(self):
        future_text = json.dumps(
            {
                "schema_version": 9,
                "record_id": "future-1",
                "occurred_at": "2030-01-01T00:00:00Z",
                "new_field_we_do_not_understand": {"nested": [1, 2, 3]},
            },
            ensure_ascii=False,
            indent=2,
        )
        result = evaluate_text(future_text)
        self.assertEqual(result.outcome, "pending_upgrade")
        self.assertEqual(result.version, 9)
        self.assertEqual(result.primary_issue.action, ESCALATE)

        svc = IngestionService()
        out = svc.ingest_one("future.json", future_text)
        self.assertEqual(out.status, PENDING_STATUS)
        self.assertTrue(out.raw_preserved)
        # 原文逐字节保全（不是重新序列化的结果）
        self.assertEqual(svc.repo.preserved_raw(out.pending_id), future_text)

    def test_future_raw_dedup_keeps_single_copy(self):
        future_text = make_claim(schema_version=7)
        svc = IngestionService()
        first = svc.ingest_one("a.json", future_text)
        second = svc.ingest_one("b.json", future_text)
        self.assertEqual(first.pending_id, second.pending_id)
        self.assertEqual(len(svc.repo._pending_vault), 1)


# --------------------------------------------------------------------------
# 批量收件：保序、隔离、去重、复核
# --------------------------------------------------------------------------


class BatchIngestionTest(unittest.TestCase):
    def test_order_preserved_and_bad_file_isolated(self):
        svc = IngestionService()
        files = [
            ("bad.json", "{broken"),
            ("good-1.json", make_claim(record_id="rec-a")),
            ("good-2.json", make_claim(record_id="rec-b")),
        ]
        results = svc.ingest_batch(files)
        self.assertEqual([r.sequence for r in results], [1, 2, 3])
        self.assertEqual([r.source_name for r in results],
                         ["bad.json", "good-1.json", "good-2.json"])
        statuses = [r.status for r in results]
        self.assertEqual(
            statuses, [REJECTED_STATUS, ACCEPTED, ACCEPTED]
        )
        # 坏文件没有挡住好文件
        self.assertIsNotNone(results[1].archive_id)
        self.assertIsNotNone(results[2].archive_id)
        self.assertNotEqual(results[1].archive_id, results[2].archive_id)

    def test_every_bad_input_class_end_to_end(self):
        svc = IngestionService()
        cases = {
            "empty.json": "",
            "truncated.json": make_claim()[:30],
            "not-object.json": "[1,2,3]",
            "trailing.json": make_claim() + "??",
            "dup.json": '{"schema_version": 1, "record_id": "d", '
                        '"occurred_at": "2026-09-20T09:00:00+08:00", '
                        '"revision": 1, "revision": 2, "source": "s"}',
            "unknown.json": make_claim(supplier_note="n"),
            "naive.json": make_claim(occurred_at="2026-09-20T09:00:00"),
            "noid.json": make_claim(record_id=""),
            "negrev.json": make_claim(revision=-1),
        }
        results = svc.ingest_batch(list(cases.items()))
        for result in results:
            self.assertEqual(result.status, REJECTED_STATUS, msg=result.source_name)
            self.assertIsNone(result.archive_id)
            self.assertIsNotNone(result.issue)

    def test_exact_duplicate_returns_existing_archive_id(self):
        notifier = FakeNotifier()
        svc = IngestionService(notifier)
        first = svc.ingest_one("day1.json", make_claim())
        second = svc.ingest_one("day2.json", make_claim())
        self.assertEqual(first.status, ACCEPTED)
        self.assertEqual(second.status, ACCEPTED_DUPLICATE)
        self.assertEqual(first.archive_id, second.archive_id)
        # 只有一份归档、一次通知
        self.assertEqual(len(svc.repo._entries), 1)
        self.assertEqual(len(notifier.sent), 1)

    def test_duplicate_while_notification_pending_sends_once_after_recovery(self):
        notifier = FakeNotifier(fail_references={"ARC-0001"})
        svc = IngestionService(notifier)
        first = svc.ingest_one("day1.json", make_claim())
        self.assertFalse(first.notified)  # 归档成功、通知失败
        self.assertEqual(len(svc.repo._entries), 1)
        # 停机期间申报方原样再次上传
        second = svc.ingest_one("day1-repeat.json", make_claim())
        self.assertEqual(second.archive_id, first.archive_id)
        self.assertEqual(len(svc.repo._entries), 1)
        # 通道恢复：只补发一封
        notifier.fail_references.clear()
        recovered = svc.recover_pending_actions()
        self.assertEqual(recovered, ["ARC-0001"])
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(svc.recover_pending_actions(), [])

    def test_same_record_id_changed_content_opens_review(self):
        notifier = FakeNotifier()
        svc = IngestionService(notifier)
        first = svc.ingest_one("claim.json", make_claim(revision=1, source="初版"))
        changed = svc.ingest_one(
            "claim-again.json", make_claim(revision=2, source="初版")
        )
        self.assertEqual(changed.status, REVIEW)
        self.assertIsNotNone(changed.review_id)
        # 历史归档原样保留、归档号仍指向初版
        self.assertEqual(len(svc.repo._entries), 1)
        historical = svc.repo.latest_by_record_id("rec-001")
        self.assertEqual(historical.archive_id, first.archive_id)
        self.assertEqual(historical.record.revision, 1)
        # 复核单记录了新内容
        review = svc.repo._reviews[changed.review_id]
        self.assertEqual(review["new_record"].revision, 2)
        self.assertEqual(review["prior_archive_id"], first.archive_id)
        # 一封归档通知 + 一封复核通知
        self.assertEqual({e for _, e, _ in notifier.sent},
                         {first.archive_id, changed.review_id})

    def test_changed_content_reuploaded_returns_same_review(self):
        # 同一变更被原样再次拖入：沿用已有复核单，不重复开单、不重复通知。
        notifier = FakeNotifier()
        svc = IngestionService(notifier)
        svc.ingest_one("claim.json", make_claim(revision=1))
        changed_text = make_claim(revision=2)
        first = svc.ingest_one("claim-v2.json", changed_text)
        again = svc.ingest_one("claim-v2-copy.json", changed_text)
        self.assertEqual(first.status, REVIEW)
        self.assertEqual(again.status, REVIEW)
        self.assertEqual(first.review_id, again.review_id)
        self.assertEqual(len(svc.repo._reviews), 1)
        self.assertEqual(len(notifier.sent), 2)  # 归档 + 复核各一封

    def test_migrated_claim_archives_as_v1(self):
        svc = IngestionService()
        result = svc.ingest_one("legacy.json", VersionTest.V0)
        self.assertEqual(result.status, MIGRATED_STATUS)
        self.assertEqual(result.version, 1)
        self.assertTrue(result.archive_id.startswith("ARC-"))
        self.assertEqual(result.warnings[0].code, "MIGRATED_V0_TO_V1")


# --------------------------------------------------------------------------
# 停机恢复
# --------------------------------------------------------------------------


class RecoveryTest(unittest.TestCase):
    def test_recovery_only_completes_unfinished_actions(self):
        # ARC-0001 送达成功；ARC-0002 在归档与通知之间停机。
        notifier = FakeNotifier(fail_references={"ARC-0002"})
        svc = IngestionService(notifier)
        ok = svc.ingest_one("a.json", make_claim(record_id="rec-a"))
        crashed = svc.ingest_one("b.json", make_claim(record_id="rec-b"))
        self.assertTrue(ok.notified)
        self.assertFalse(crashed.notified)
        # 两份归档都已落库
        self.assertEqual(len(svc.repo._entries), 2)
        pending = {item.reference for item in svc.repo.pending_outbox()}
        self.assertEqual(pending, {"ARC-0002"})

        # 恢复：只补 ARC-0002，不重发 ARC-0001
        notifier.fail_references.clear()
        recovered = svc.recover_pending_actions()
        self.assertEqual(recovered, ["ARC-0002"])
        references = [reference for _, reference, _ in notifier.sent]
        self.assertEqual(sorted(references), ["ARC-0001", "ARC-0002"])

        # 恢复任务可安全重复执行
        self.assertEqual(svc.recover_pending_actions(), [])
        self.assertEqual(len(notifier.sent), 2)

    def test_retry_still_failing_keeps_action_pending(self):
        notifier = FakeNotifier(fail_references={"ARC-0001"})
        svc = IngestionService(notifier)
        svc.ingest_one("a.json", make_claim())
        self.assertEqual(svc.recover_pending_actions(), [])  # 通道仍未恢复
        self.assertEqual(
            {item.reference for item in svc.repo.pending_outbox()}, {"ARC-0001"}
        )


# --------------------------------------------------------------------------
# 向后兼容：现有 load_record 用法与业务样例
# --------------------------------------------------------------------------


class BackwardCompatibilityTest(unittest.TestCase):
    def test_existing_contract_test_still_passes(self):
        item = load_record(FIXTURES / "menu_claim.json")
        self.assertIsInstance(item, DomainRecord)
        self.assertEqual(item.domain, "menu_review")
        self.assertGreater(item.revision, 0)
        self.assertEqual(item.schema_version, 1)

    def test_describe_is_reviewer_ready(self):
        svc = IngestionService()
        results = svc.ingest_batch(
            [
                ("neg.json", make_claim(revision=-1)),
                ("future.json", make_claim(schema_version=5)),
                ("ok.json", make_claim(record_id="rec-z")),
            ]
        )
        text = "\n".join(r.describe() for r in results)
        self.assertIn("1. neg.json", text)
        self.assertIn("不予收件", text)
        self.assertIn("补正材料", text)
        self.assertIn("等待系统升级", text)
        self.assertIn("3. ok.json", text)  # 顺序保持
        self.assertIn("正式归档（归档号 ARC-0001）", text)


if __name__ == "__main__":
    unittest.main()
