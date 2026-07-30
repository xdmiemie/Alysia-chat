from __future__ import annotations

import base64
import random

from runtime.adapters.wechat.models import ILinkCredentials


def build_headers(credentials: ILinkCredentials, bot_agent: str) -> dict[str, str]:
    credentials.validate()
    return {
        "Authorization": f"Bearer {credentials.bot_token}",
        "AuthorizationType": "ilink_bot_token",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": bot_agent,
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": "131335",
        "X-Bot-User-Id": credentials.bot_user_id,
    }


def _random_wechat_uin() -> str:
    value = str(random.randint(1, 2**32 - 1)).encode("ascii")
    return base64.b64encode(value).decode("ascii")
