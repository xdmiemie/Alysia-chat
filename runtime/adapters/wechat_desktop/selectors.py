from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SelectorProfile:
    name: str
    chat_list: str = "chat_list"
    chat_title: str = "chat_title"
    message_list: str = "message_list"
    input_box: str = "input_box"
    send_button: str = "send_button"


DEFAULT_SELECTOR_PROFILE = SelectorProfile(name="set-after-calibration")
