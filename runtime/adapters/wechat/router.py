from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Protocol

from runtime.adapters.wechat.config import WeChatConfig
from runtime.adapters.wechat.deduplicator import MessageDeduplicator
from runtime.adapters.wechat.ilink.crypto import stable_hash
from runtime.adapters.wechat.message_buffer import BufferedMessageBatch, MessageBuffer
from runtime.adapters.wechat.models import ILinkTextMessage, SendResult
from runtime.core.models import AlysiaReply, UnifiedMessage, new_request_id


class CoreProcessor(Protocol):
    def process(self, message: UnifiedMessage) -> AlysiaReply:
        ...


class TextSender(Protocol):
    async def send_text(self, to_user_id: str, context_token: str, text: str) -> SendResult:
        ...


class WeChatTextRouter:
    def __init__(
        self,
        *,
        config: WeChatConfig,
        core: CoreProcessor,
        sender: TextSender,
        deduplicator: MessageDeduplicator | None = None,
        buffer: MessageBuffer | None = None,
    ) -> None:
        self.config = config
        self.core = core
        self.sender = sender
        self.deduplicator = deduplicator or MessageDeduplicator()
        self.buffer = buffer or MessageBuffer(config.reply.buffer_seconds)

    async def accept(self, message: ILinkTextMessage) -> SendResult | None:
        if not self.config.contacts.is_allowed(message.from_user_id):
            self._write_log("ignored_not_allowed", message=message)
            return None
        if self.deduplicator.seen(message):
            self._write_log("ignored_duplicate", message=message)
            return None

        ready_batches = self.buffer.add(message)
        result: SendResult | None = None
        for batch in ready_batches:
            result = await self.reply_to_batch(batch)
        return result

    async def flush(self) -> list[SendResult]:
        results = []
        for batch in self.buffer.flush_all():
            results.append(await self.reply_to_batch(batch))
        return results

    async def flush_expired(self) -> list[SendResult]:
        results = []
        for batch in self.buffer.flush_expired():
            results.append(await self.reply_to_batch(batch))
        return results

    def next_flush_seconds(self) -> float | None:
        return self.buffer.next_flush_seconds()

    async def reply_to_batch(self, batch: BufferedMessageBatch) -> SendResult:
        latest = batch.latest
        user_text = batch.text
        self._write_log("received_text_batch", message=latest, batch_size=len(batch.messages), text=user_text)
        core_reply = self.core.process(
            UnifiedMessage(
                request_id=new_request_id(),
                channel="wechat_ilink",
                session_id=latest.conversation_key,
                sender_id=latest.from_user_id,
                sender_name=latest.from_user_id,
                message_type="text",
                text=user_text,
                character_id=str(latest.metadata.get("character_id", "elysia")),
                metadata={
                    "ilink_message_id": latest.message_id,
                    "ilink_seq": latest.seq,
                    "context_token_ref": stable_hash(latest.context_token),
                },
            )
        )
        if not core_reply.text.strip():
            result = SendResult(ok=False, error=core_reply.error or "core reply text was empty")
            self._write_log("send_skipped", message=latest, result=result.to_dict())
            return result

        result = await self.sender.send_text(
            to_user_id=latest.from_user_id,
            context_token=latest.context_token,
            text=core_reply.text,
        )
        self._write_log(
            "sent_text_reply" if result.ok else "send_failed",
            message=latest,
            result=result.to_dict(),
            request_id=core_reply.request_id,
        )
        return result

    def _write_log(self, event: str, *, message: ILinkTextMessage, **fields: object) -> None:
        log_path = Path(self.config.log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": int(time.time()),
            "event": event,
            "message": message.to_log_dict(),
            **fields,
        }
        with log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(payload, ensure_ascii=False) + "\n")
