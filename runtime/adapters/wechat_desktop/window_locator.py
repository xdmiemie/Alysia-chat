from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class WindowInfo:
    title: str
    handle: str = ""
    visible: bool = True


class WindowAwareClient(Protocol):
    def locate_main_window(self) -> WindowInfo | None:
        ...


def locate_main_window(client: WindowAwareClient) -> WindowInfo | None:
    return client.locate_main_window()
