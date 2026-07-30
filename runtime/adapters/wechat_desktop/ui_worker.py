from __future__ import annotations

from runtime.adapters.wechat_desktop.ui_queue import SingleUIQueue


class WeChatUIWorker(SingleUIQueue):
    """Single serialized UI lane for Windows WeChat operations."""
