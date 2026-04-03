from __future__ import annotations

import json
import logging
import uuid
from typing import Iterable

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from .config import Settings
from .models import DeliveryChunk


class DeliveryService:
    def __init__(self, settings: Settings, logger: logging.Logger, client: lark.Client):
        self.settings = settings
        self.logger = logger
        self.client = client

    def split_text(self, title: str, text: str) -> list[DeliveryChunk]:
        normalized = (text or "").strip() or "_空回复_"
        max_chars = max(self.settings.text_chunk_chars, 500)
        if len(normalized) <= max_chars:
            return [DeliveryChunk(title=title or "Codex", text=normalized)]

        parts: list[str] = []
        remaining = normalized
        while remaining:
            if len(remaining) <= max_chars:
                parts.append(remaining.strip())
                break
            window = remaining[:max_chars]
            split_at = window.rfind("\n\n")
            if split_at < max_chars * 0.4:
                split_at = window.rfind("\n")
            if split_at < max_chars * 0.4:
                split_at = window.rfind(" ")
            if split_at < max_chars * 0.4:
                split_at = max_chars
            parts.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].lstrip()

        total = len(parts)
        return [
            DeliveryChunk(
                title=f"{title or 'Codex'} ({index}/{total})" if total > 1 else title or "Codex",
                text=part or "_空回复_",
            )
            for index, part in enumerate(parts, start=1)
        ]

    def send_chunk(self, conversation_id: str, chunk: DeliveryChunk) -> bool:
        content = {"text": f"{chunk.title}\n\n{chunk.text}".strip()}
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(conversation_id)
                .msg_type("text")
                .content(json.dumps(content, ensure_ascii=False))
                .uuid(str(uuid.uuid4()))
                .build()
            )
            .build()
        )
        try:
            response = self.client.im.v1.message.create(request)
        except Exception as error:
            self.logger.exception("failed to send feishu message: %s", error)
            return False

        if response.success():
            return True

        self.logger.error(
            "failed to send feishu message code=%s msg=%s log_id=%s",
            response.code,
            response.msg,
            response.get_log_id(),
        )
        return False

    def send_chunks(
        self,
        conversation_id: str,
        chunks: Iterable[DeliveryChunk],
    ) -> tuple[bool, list[DeliveryChunk]]:
        unsent: list[DeliveryChunk] = []
        all_sent = True
        for chunk in chunks:
            if not self.send_chunk(conversation_id, chunk):
                unsent.append(chunk)
                all_sent = False
        return all_sent, unsent

    def help_chunks(self) -> list[DeliveryChunk]:
        model_list = ", ".join(f"`{item}`" for item in self.settings.available_models)
        reasoning_list = ", ".join(f"`{item}`" for item in self.settings.available_reasoning_efforts)
        help_text = "\n".join(
            [
                "Feishu Codex Bridge",
                "",
                "/help",
                "/status",
                "/model <model_id>",
                "/think <low|medium|high|xhigh>",
                "/new exec <absolute_workdir>",
                "/new tmux <absolute_workdir>",
                "/resume exec <thread_id> <absolute_workdir>",
                "/resume tmux <tmux_session_name> <absolute_workdir>",
                "/stop",
                "",
                f"可用模型: {model_list}",
                f"可用推理强度: {reasoning_list}",
                f"默认配置: model=`{self.settings.default_model}`, think=`{self.settings.default_reasoning_effort}`",
                "",
                "支持在同一条消息里按行发送多个 slash 命令，系统会按顺序执行。",
                "当前会话内的 Codex 可按需调用 `lark-cli`，用于发消息、回消息、创建/更新文档、上传文件等飞书侧操作。",
                "",
                "普通文本消息会直接发送给当前激活的 Codex 会话。",
            ]
        )
        return [DeliveryChunk(title="帮助", text=help_text)]
