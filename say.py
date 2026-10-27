"""让插件替我说话：造一个假的消息事件，走框架正常的发送流程。

直接调发送工具绕过了框架，消息既能挂不上表情包，也不会写进会话历史，
下一轮我自己说过什么都想不起来。这里改成伪装成「刚有人发消息来」，
让 AstrBot 按正规流程处理，分段、表情包、历史存档就都自动生效了。
"""

from __future__ import annotations

import asyncio
import inspect
import traceback
import uuid
from typing import Any

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import (
    MessageChain,
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.star_handler import EventType, star_handlers_registry

try:  # 不同版本位置不一样，能拿到就用平台原生的发送入口
    from astrbot.core.platform.astr_message_event import MessageSession
except ImportError:  # pragma: no cover
    try:
        from astrbot.core.platform.message_session import MessageSession
    except ImportError:  # pragma: no cover
        MessageSession = None  # type: ignore[assignment]


_MSG_ID_PREFIX = "bili-say"


def _split_umo(umo: str) -> tuple[str, str, str]:
    """把 platform:MessageType:session_id 拆开，拆不动就原样返回当会话号用。"""
    parts = (umo or "").split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return "", "", umo or ""


def _resolve_platform(context: Any, platform_id: str) -> Any:
    """按平台 id 找到平台实例。"""
    manager = getattr(context, "platform_manager", None)
    if manager is None:
        return None
    try:
        platforms = manager.get_insts() or []
    except Exception:
        platforms = []
    for inst in platforms:
        try:
            meta = inst.meta()
        except Exception:
            continue
        if getattr(meta, "id", None) == platform_id:
            return inst
    return None


class BiliSayEvent(AstrMessageEvent):
    """假的消息事件：长得像有人发消息来了，其实是我要主动说话。"""

    def __init__(
        self,
        *,
        plugin: Any,
        platform_meta: Any,
        umo: str,
        target_id: str,
        message_type: MessageType,
        self_id: str = "",
        is_group: bool = False,
    ) -> None:
        # 基类构造里会读 unified_msg_origin，所以这几个字段必须先赋值
        self._say_plugin = plugin
        self._say_umo = umo
        self._say_target = target_id
        self._say_is_group = is_group
        self._say_sent = False

        msg = AstrBotMessage()
        msg.type = message_type
        msg.self_id = self_id or ""
        msg.session_id = target_id
        msg.message_id = f"{_MSG_ID_PREFIX}:{uuid.uuid4().hex}"
        msg.message = []
        msg.message_str = ""
        msg.raw_message = None
        msg.sender = MessageMember(
            user_id="" if is_group else target_id, nickname=None
        )
        if is_group:
            msg.group = Group(group_id=target_id)

        super().__init__(
            message_str="",
            message_obj=msg,
            platform_meta=platform_meta,
            session_id=target_id,
        )

        self.plugins_name = None
        self.is_wake = True
        self.is_at_or_wake_command = True
        self.role = "member"

    @property
    def unified_msg_origin(self) -> str:  # type: ignore[override]
        return self._say_umo

    async def send(self, message: MessageChain) -> bool:  # type: ignore[override]
        """先走平台原生发送，不行再退回核心接口。"""
        if message is None:
            return False

        sent = False
        platform_id, _, _ = _split_umo(self._say_umo)
        platform = _resolve_platform(getattr(self._say_plugin, "context", None), platform_id)
        if platform is not None and MessageSession is not None:
            try:
                session = MessageSession(
                    platform_name=platform_id,
                    message_type=(
                        MessageType.GROUP_MESSAGE
                        if self._say_is_group
                        else MessageType.FRIEND_MESSAGE
                    ),
                    session_id=self._say_target,
                )
                await platform.send_by_session(session, message)
                sent = True
            except Exception as exc:
                logger.warning(f"[bili-arona] 平台发送失败，退回核心接口: {exc}")

        if not sent:
            try:
                res = await self._say_plugin.context.send_message(self._say_umo, message)
                sent = res is not False
            except Exception as exc:
                logger.warning(f"[bili-arona] 核心接口发送失败: {exc}")
                sent = False

        if sent:
            self._say_sent = True
            self._has_send_oper = True
        return sent


async def _dispatch_hook(event: Any, hook_type: Any) -> bool:
    """把框架的标准钩子挨个跑一遍，一个钩子出错不拖累后面。"""
    if event is None or hook_type is None or star_handlers_registry is None:
        return False

    try:
        handlers = star_handlers_registry.get_handlers_by_event_type(
            hook_type, plugins_name=getattr(event, "plugins_name", None)
        )
    except Exception as exc:
        logger.debug(f"[bili-arona] 取钩子列表失败: {exc}")
        return False

    for handler in handlers or []:
        name = getattr(handler, "handler_full_name", None) or getattr(
            handler, "handler_name", "unknown"
        )
        try:
            if not inspect.iscoroutinefunction(handler.handler):
                continue
            await handler.handler(event)
        except Exception as exc:
            logger.warning(
                f"[bili-arona] 钩子 {name} 执行失败: {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()}"
            )
        try:
            if event.is_stopped():
                return True
        except Exception:
            continue

    try:
        return bool(event.is_stopped())
    except Exception:
        return False


async def say(plugin: Any, umo: str, text: str) -> bool:
    """替我说一句话：走正规发送流程，发完记进会话历史。

    Returns:
        True 表示确实送到了。
    """
    body = (text or "").strip()
    if not body or not umo:
        return False

    platform_id, msg_type_str, target_id = _split_umo(umo)
    if not target_id:
        return False

    context = getattr(plugin, "context", None)
    platform = _resolve_platform(context, platform_id)

    if platform is not None:
        try:
            platform_meta = platform.meta()
        except Exception:
            platform_meta = None
    else:
        platform_meta = None

    if platform_meta is None:
        logger.warning(f"[bili-arona] 找不到平台 {platform_id}，这条发不出去")
        return False

    is_group = "group" in (msg_type_str or "").lower()
    self_id = str(getattr(platform_meta, "self_id", "") or "")

    event = BiliSayEvent(
        plugin=plugin,
        platform_meta=platform_meta,
        umo=umo,
        target_id=target_id,
        message_type=MessageType.GROUP_MESSAGE if is_group else MessageType.FRIEND_MESSAGE,
        self_id=self_id,
        is_group=is_group,
    )

    result = MessageEventResult()
    # 标成 LLM 结果，依赖这个判断的装饰器（分段、表情包）才会动手
    result.set_result_content_type(ResultContentType.LLM_RESULT)
    result.chain = [Plain(text=body)]
    event.set_result(result)

    if await _dispatch_hook(event, EventType.OnDecoratingResultEvent):
        logger.info("[bili-arona] 装饰钩子终止了发送，这条放弃")
        return False

    decorated = event.get_result()
    if decorated is None:
        return False
    chain_components = getattr(decorated, "chain", None)
    if not chain_components:
        return False

    ok = await event.send(MessageChain(chain=chain_components))
    if not ok:
        logger.warning("[bili-arona] 这条没送到，不写历史")
        return False

    await _dispatch_hook(event, EventType.OnAfterMessageSentEvent)
    await _remember(plugin, umo, body)
    return True


async def _remember(plugin: Any, umo: str, text: str) -> None:
    """把我说的话记进会话历史，下一轮才不会忘了自己刚才讲什么。

    不能当场写：叫醒那一轮结束时框架会拿自己手里的旧副本整份重写历史，
    当场写进去的马上被盖掉。所以交给后台，等那轮彻底收尾再补。
    """
    try:
        cm = plugin.context.conversation_manager
        if cm is None:
            logger.warning("[bili-arona] 记进历史失败：拿不到 conversation_manager")
            return
        cid = await cm.get_curr_conversation_id(umo)
        if not cid:
            logger.warning(f"[bili-arona] 记进历史失败：这个会话还没有 id，umo={umo}")
            return
        asyncio.create_task(_remember_late(cm, cid, text))
    except Exception as exc:
        logger.warning(f"[bili-arona] 记进历史失败: {type(exc).__name__}: {exc}")


async def _remember_late(cm: Any, cid: str, text: str) -> None:
    """等这一轮收尾，再反复确认历史里到底有没有这句话，缺了就补。"""
    want = text.strip()
    for attempt in range(1, 6):
        await asyncio.sleep(6)
        try:
            conv = await cm.db.get_conversation_by_id(cid=cid)
            history = (conv.content if conv else None) or []
            found = any(
                isinstance(m, dict) and str(m.get("content", "")).strip() == want
                for m in history
            )
            if found:
                logger.info(f"[bili-arona] 第 {attempt} 次确认，历史里已有我刚说的话")
                return
            await cm.add_message_pair(
                cid,
                {
                    "role": "user",
                    "content": "（后台任务叫我说话，不是我主动开口，这句是我回的话）",
                },
                {"role": "assistant", "content": want},
            )
            logger.info(f"[bili-arona] 第 {attempt} 次补写历史，会话 {cid[:8]}，{len(want)} 字")
        except Exception as exc:
            logger.warning(f"[bili-arona] 补写历史出错: {type(exc).__name__}: {exc}")
