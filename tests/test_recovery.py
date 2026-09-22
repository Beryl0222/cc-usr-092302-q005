import tempfile
import unittest

import helpers
from menu_review import ArchiveStore, IntakeService, RecordingNotifier

FIXED_CLOCK = lambda: "2026-09-22T09:00:00+08:00"  # noqa: E731


class FailingNotifier:
    """第 fail_on 次发送（1 起计）抛出异常，模拟归档与通知之间的停机。"""

    def __init__(self, fail_on):
        self.fail_on = fail_on
        self.attempts = []

    def send(self, notification_id, kind, payload):
        self.attempts.append(notification_id)
        if len(self.attempts) == self.fail_on:
            raise RuntimeError("模拟停机")


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _service(self, notifier):
        return IntakeService(ArchiveStore(self.tmp.name), notifier, clock=FIXED_CLOCK)

    def test_recovery_completes_only_unfinished_notifications(self):
        notifier = FailingNotifier(fail_on=2)
        outcomes = self._service(notifier).intake_batch([
            ("a.json", helpers.claim(record_id="r-1")),
            ("b.json", helpers.claim(record_id="r-2")),
        ])
        # 两份都已归档，停机发生在第二份的通知阶段
        self.assertEqual([o.status for o in outcomes], ["archived", "archived"])
        self.assertEqual(len(notifier.attempts), 2)
        first_id, second_id = notifier.attempts

        # 重启：全新服务实例，仅依据磁盘状态恢复
        recovered = RecordingNotifier()
        report = self._service(recovered).recover()
        # 只补做未完成动作：第二份的通知
        self.assertEqual(report.completed, (second_id,))
        self.assertEqual(report.remaining, ())
        self.assertEqual(list(recovered.delivered), [second_id])
        self.assertNotIn(first_id, recovered.delivered)

        # 再次恢复：无任何动作
        again = self._service(recovered).recover()
        self.assertEqual(again.completed, ())
        self.assertEqual(len(recovered.calls), 1)

        # 恢复不重归档：归档号与记录内容保持原样
        store = ArchiveStore(self.tmp.name)
        self.assertEqual(store.find_record("r-1")["archive_id"], "AR-000001")
        self.assertEqual(store.find_record("r-2")["archive_id"], "AR-000002")
        self.assertEqual(store.find_record("r-1")["notification"]["state"], "sent")
        self.assertEqual(store.find_record("r-2")["notification"]["state"], "sent")

    def test_crash_after_send_before_mark_is_idempotent(self):
        class CrashMarkStore(ArchiveStore):
            crashed = False

            def mark_notification_sent(self, ref):
                if not self.crashed:
                    self.crashed = True
                    raise RuntimeError("标记落盘前停机")
                super().mark_notification_sent(ref)

        notifier = RecordingNotifier()
        service = IntakeService(CrashMarkStore(self.tmp.name), notifier, clock=FIXED_CLOCK)
        with self.assertRaises(RuntimeError):
            service.intake_one("a.json", helpers.claim(record_id="r-1"))
        # 通知已发出，但磁盘上仍是 pending
        self.assertEqual(len(notifier.delivered), 1)

        # 恢复时以同一通知 id 重发，幂等消费方去重 → 有效投递仍是一次
        report = self._service(notifier).recover()
        self.assertEqual(len(report.completed), 1)
        self.assertEqual(len(notifier.calls), 2)
        self.assertEqual(notifier.calls[0][0], notifier.calls[1][0])
        self.assertEqual(len(notifier.delivered), 1)

        entry = ArchiveStore(self.tmp.name).find_record("r-1")
        self.assertEqual(entry["notification"]["state"], "sent")

    def test_recover_with_nothing_pending_is_noop(self):
        notifier = RecordingNotifier()
        self._service(notifier).intake_one("a.json", helpers.GOOD_V1)
        report = self._service(notifier).recover()
        self.assertEqual(report.completed, ())
        self.assertEqual(report.remaining, ())
        self.assertEqual(len(notifier.delivered), 1)  # 不重发

    def test_recovery_also_covers_review_notifications(self):
        notifier = FailingNotifier(fail_on=2)
        service = self._service(notifier)
        service.intake_one("v1.json", helpers.claim(revision=1))
        conflict = service.intake_one("v2.json", helpers.claim(revision=2))
        self.assertEqual(conflict.status, "conflict_review")
        review_id = conflict.review_id

        recovered = RecordingNotifier()
        report = self._service(recovered).recover()
        self.assertEqual(len(report.completed), 1)
        kind, payload = recovered.delivered[report.completed[0]]
        self.assertEqual(kind, "review_opened")
        self.assertEqual(payload["review_id"], review_id)


if __name__ == "__main__":
    unittest.main()
