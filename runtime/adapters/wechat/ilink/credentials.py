from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from runtime.adapters.wechat.models import ILinkCredentials


def load_credentials(path: str | Path) -> ILinkCredentials:
    credential_path = Path(path)
    if not credential_path.exists():
        raise FileNotFoundError(f"iLink credentials not found: {credential_path}")
    data = json.loads(credential_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"iLink credentials must be a JSON object: {credential_path}")
    credentials = ILinkCredentials(
        bot_token=str(data.get("bot_token", "")),
        bot_user_id=str(data.get("bot_user_id", "")),
        base_url=str(data.get("base_url", "")),
        account_id=str(data.get("account_id", "main")),
    )
    credentials.validate()
    return credentials


def save_credentials(path: str | Path, credentials: ILinkCredentials) -> None:
    credentials.validate()
    credential_path = Path(path)
    credential_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "bot_token": credentials.bot_token,
        "bot_user_id": credentials.bot_user_id,
        "base_url": credentials.base_url.rstrip("/"),
        "account_id": credentials.account_id,
    }
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=credential_path.parent,
        delete=False,
        prefix=f".{credential_path.name}.",
        suffix=".tmp",
    ) as temp_file:
        json.dump(payload, temp_file, ensure_ascii=False, indent=2)
        temp_name = temp_file.name

    try:
        os.chmod(temp_name, 0o600)
    except OSError:
        pass
    os.replace(temp_name, credential_path)
