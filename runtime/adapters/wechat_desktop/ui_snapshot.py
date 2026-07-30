from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChatSessionSnapshot:
    contact_key: str
    display_name: str
    title: str
    unread: bool = False
    last_text: str = ""
    is_group: bool = False
    is_system: bool = False
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatMessageSnapshot:
    direction: str
    message_type: str
    text: str
    runtime_id: str = ""
    control_path: str = ""
    relative_order: int = 0
    time_label: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class UISnapshot:
    logged_in: bool
    account_display_name: str = ""
    version: str = ""
    main_window_title: str = ""
    controls: dict[str, bool] = field(default_factory=dict)
    blocking_dialogs: tuple[str, ...] = ()
