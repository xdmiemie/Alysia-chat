from __future__ import annotations


class TakeoverState:
    def __init__(self) -> None:
        self.enabled = False
        self.reason = ""

    def activate(self, reason: str) -> None:
        self.enabled = True
        self.reason = reason

    def release(self) -> None:
        self.enabled = False
        self.reason = ""
