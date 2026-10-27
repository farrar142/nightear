"""테스트용 가짜 알림기. 실제 Discord 로는 아무것도 보내지 않고 메시지를 모아 둔다."""
from __future__ import annotations


class FakeNotifier:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    def send(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("발송 실패")
        self.sent.append(text)
