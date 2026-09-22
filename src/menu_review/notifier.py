"""通知出口：归档与复核事件的外发，消费方按通知 id 幂等。"""

from __future__ import annotations

from typing import Protocol


class Notifier(Protocol):
    def send(self, notification_id: str, kind: str, payload: dict) -> None:
        """发送通知；实现方必须以 notification_id 作为幂等键去重。"""
        ...


class RecordingNotifier:
    """测试与演示用通知器：记录全部调用，并按通知 id 幂等去重。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.delivered: dict[str, tuple[str, dict]] = {}

    def send(self, notification_id: str, kind: str, payload: dict) -> None:
        self.calls.append((notification_id, kind))
        self.delivered.setdefault(notification_id, (kind, payload))
