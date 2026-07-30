from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib import error, request

from runtime.adapters.wechat.ilink.headers import build_headers
from runtime.adapters.wechat.models import ILinkCredentials


class Transport(Protocol):
    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class ILinkApiError(Exception):
    message: str
    status_code: int | None = None
    code: str = ""
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


class UrlLibTransport:
    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=timeout) as response:
                response_body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ILinkApiError(detail or exc.reason, status_code=exc.code, retryable=exc.code >= 500) from exc
        except error.URLError as exc:
            raise ILinkApiError(str(exc.reason), retryable=True) from exc
        if not response_body.strip():
            return {}
        data = json.loads(response_body)
        if not isinstance(data, dict):
            raise ILinkApiError("iLink response was not a JSON object")
        return data


class ILinkApiClient:
    def __init__(
        self,
        *,
        credentials: ILinkCredentials,
        bot_agent: str,
        timeout_seconds: float,
        channel_version: str = "2.1.7",
        transport: Transport | None = None,
    ) -> None:
        self.credentials = credentials
        self.bot_agent = bot_agent
        self.timeout_seconds = timeout_seconds
        self.channel_version = channel_version
        self.transport = transport or UrlLibTransport()

    async def get_updates(self, cursor: str) -> dict[str, Any]:
        return await self.call(
            "getupdates",
            {
                "get_updates_buf": cursor,
                "base_info": self.base_info(),
            },
        )

    async def send_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.call("sendmessage", payload)

    async def send_typing(self, *, to_user_id: str, context_token: str, typing: bool) -> dict[str, Any]:
        return await self.call(
            "sendtyping",
            {
                "to_user_id": to_user_id,
                "context_token": context_token,
                "typing": typing,
                "base_info": self.base_info(),
            },
        )

    def base_info(self) -> dict[str, str]:
        return {"channel_version": self.channel_version}

    async def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.credentials.base_url.rstrip('/')}/ilink/bot/{method.lower()}"
        headers = build_headers(self.credentials, self.bot_agent)
        response = await asyncio.to_thread(
            self.transport.post_json,
            url,
            payload,
            headers,
            self.timeout_seconds,
        )
        _raise_for_ilink_error(response)
        return response


def _raise_for_ilink_error(response: dict[str, Any]) -> None:
    code = response.get("ret", response.get("errcode", response.get("error_code", 0)))
    if code in (0, "0", None, ""):
        return
    message = str(response.get("errmsg", response.get("message", "iLink API returned an error")))
    code_text = str(code)
    retryable = code_text.startswith("5") or code_text in {"timeout", "temporarily_unavailable"}
    auth_failed = code_text in {"-14", "401", "403", "unauthorized", "login_expired", "invalid_token"}
    raise ILinkApiError(message, code=code_text, retryable=retryable and not auth_failed)
