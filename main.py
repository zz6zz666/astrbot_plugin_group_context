import datetime
import random
import traceback
import uuid
from collections import defaultdict

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest, LLMResponse, Provider
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.platform import MessageType

"""
群聊上下文感知插件
优化群聊上下文增强功能,提供群聊记录追踪、主动回复、图片描述等功能
"""

@register("group_context", "zz6zz666", "优化群聊上下文增强功能,提供群聊记录追踪、主动回复、图片描述等功能", "1.0.0")
class GroupContextPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config  # AstrBotConfig继承自Dict,可以直接使用字典方法访问
        self.session_chats = defaultdict(list)
        """记录群成员的群聊记录"""
        logger.info("群聊上下文感知插件已初始化")

    def get_cfg(self, key: str, default=None):
        """从插件配置中获取配置项"""
        return self.config.get(key, default)

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """处理群聊消息并支持主动回复"""
        # 仅支持群聊
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return

        # 检查是否有文本或图片内容
        has_image_or_plain = False
        for comp in event.message_obj.message:
            if isinstance(comp, Plain) or isinstance(comp, Image):
                has_image_or_plain = True
                break

        if not has_image_or_plain:
            return

        # 检查是否需要主动回复
        need_active = await self.need_active_reply(event)

        # 记录对话
        try:
            await self.handle_message(event)
        except BaseException as e:
            logger.error(f"记录群聊消息失败: {e}")

        # 主动回复逻辑
        if need_active:
            provider = self.context.get_using_provider(event.unified_msg_origin)
            if not provider:
                logger.error("未找到任何 LLM 提供商。请先配置。无法主动回复")
                return
            try:
                session_curr_cid = await self.context.conversation_manager.get_curr_conversation_id(
                    event.unified_msg_origin,
                )

                if not session_curr_cid:
                    logger.error(
                        "当前未处于对话状态,无法主动回复,请确保 平台设置->会话隔离(unique_session) 未开启,并使用 /switch 序号 切换或者 /new 创建一个会话。",
                    )
                    return

                conv = await self.context.conversation_manager.get_conversation(
                    event.unified_msg_origin,
                    session_curr_cid,
                )

                prompt = event.message_str

                if not conv:
                    logger.error("未找到对话,无法主动回复")
                    return

                yield event.request_llm(
                    prompt=prompt,
                    func_tool_manager=self.context.get_llm_tool_manager(),
                    session_id=event.session_id,
                    conversation=conv,
                )
            except BaseException as e:
                logger.error(traceback.format_exc())
                logger.error(f"主动回复失败: {e}")

    def _limit_chat_history(self, unified_msg_origin: str):
        """限制聊天记录数量"""
        max_cnt = int(self.get_cfg("group_message_max_cnt", 300))
        if len(self.session_chats[unified_msg_origin]) > max_cnt:
            self.session_chats[unified_msg_origin].pop(0)

    async def handle_message(self, event: AstrMessageEvent):
        """记录群聊消息到上下文中"""
        datetime_str = datetime.datetime.now().strftime("%H:%M:%S")
        parts = [f"[{event.message_obj.sender.nickname}/{datetime_str}]: "]

        # 获取配置
        image_caption = bool(self.get_cfg("image_caption", False))
        image_caption_provider_id = self.get_cfg("image_caption_provider_id", "")

        for comp in event.get_messages():
            if isinstance(comp, Plain):
                parts.append(f" {comp.text}")
            elif isinstance(comp, Image):
                if image_caption and image_caption_provider_id:
                    try:
                        url = comp.url if comp.url else comp.file
                        if not url:
                            raise Exception("图片 URL 为空")
                        caption = await self.get_image_caption(
                            url,
                            image_caption_provider_id
                        )
                        parts.append(f" [Image: {caption}]")
                    except Exception as e:
                        logger.error(f"获取图片描述失败: {e}")
                        parts.append(" [Image]")
                else:
                    parts.append(" [Image]")
            elif isinstance(comp, At):
                parts.append(f" [At: {comp.name}]")

        final_message = "".join(parts)
        logger.debug(f"群聊上下文 | {event.unified_msg_origin} | {final_message}")
        self.session_chats[event.unified_msg_origin].append(final_message)
        self._limit_chat_history(event.unified_msg_origin)

    async def get_image_caption(self, image_url: str, image_caption_provider_id: str) -> str:
        """获取图片描述"""
        if not image_caption_provider_id:
            provider = self.context.get_using_provider()
        else:
            provider = self.context.get_provider_by_id(image_caption_provider_id)
            if not provider:
                raise Exception(f"没有找到 ID 为 {image_caption_provider_id} 的提供商")

        if not isinstance(provider, Provider):
            raise Exception(f"提供商类型错误({type(provider)}),无法获取图片描述")

        # 从全局配置获取图片描述提示词
        image_caption_prompt = self.get_cfg("image_caption_prompt", "请描述这张图片的内容")

        response = await provider.text_chat(
            prompt=image_caption_prompt,
            session_id=uuid.uuid4().hex,
            image_urls=[image_url],
            persist=False,
        )
        return response.completion_text

    async def need_active_reply(self, event: AstrMessageEvent) -> bool:
        """判断是否需要主动回复"""
        enable_active_reply = bool(self.get_cfg("enable_active_reply", False))
        if not enable_active_reply:
            return False

        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return False

        if event.is_at_or_wake_command:
            # 如果是命令,不主动回复
            return False

        # 检查白名单
        ar_whitelist = self.get_cfg("ar_whitelist", [])
        if ar_whitelist:
            if (event.unified_msg_origin not in ar_whitelist and
                (event.get_group_id() and event.get_group_id() not in ar_whitelist)):
                return False

        # 检查触发方式
        ar_method = self.get_cfg("ar_method", "possibility_reply")
        if ar_method == "possibility_reply":
            ar_possibility = float(self.get_cfg("ar_possibility", 0.1))
            return random.random() < ar_possibility

        return False

    @filter.on_llm_request()
    async def on_req_llm(self, event: AstrMessageEvent, req: ProviderRequest):
        """当触发 LLM 请求前,调用此方法修改 req"""
        if event.unified_msg_origin not in self.session_chats:
            return

        enable_active_reply = bool(self.get_cfg("enable_active_reply", False))
        ar_prompt = self.get_cfg("ar_prompt", "")
        prompt = req.prompt

        # 首先，清洗掉先前已经嵌入的system字段
        if req.contexts:
            # 过滤掉所有system角色的消息
            req.contexts = [ctx for ctx in req.contexts if ctx.get("role") != "system"]

        if ar_prompt:
            # 如果有自定义提示词，保持原有的单一 prompt 方式
            chats_str = "\n---\n".join(self.session_chats[event.unified_msg_origin])
            req.prompt = ar_prompt.replace("{chat_history}", chats_str).replace("{message}", prompt)
        else:
            # 使用标准的 user/assistant 对话格式
            # 注意：这里不再清空req.contexts，而是在原有基础上处理

            # 将聊天记录转换为 user/assistant 对
            chat_history = self.session_chats[event.unified_msg_origin]
            user_messages = []
            assistant_messages = []

            for chat in chat_history:
                if chat.startswith("[You]"):
                    # AI 的回复
                    # 格式: [You]: 消息内容
                    content = chat.split("]: ", 1)[1] if "]: " in chat else chat
                    assistant_messages.append(content)
                else:
                    # 用户消息
                    if assistant_messages and user_messages:
                        # 如果有累积的 user 和 assistant 消息，先添加到 contexts
                        user_content = "\n---\n".join(user_messages)
                        req.contexts.append({"role": "user", "content": user_content})

                        assistant_content = "\n---\n".join(assistant_messages)
                        req.contexts.append({"role": "assistant", "content": assistant_content})

                        user_messages = []
                        assistant_messages = []
                    elif user_messages:
                        # 如果只有 user 消息没有 assistant 回复，添加到 contexts
                        user_content = "\n---\n".join(user_messages)
                        req.contexts.append({"role": "user", "content": user_content})
                        user_messages = []

                    user_messages.append(chat)

            # 处理剩余的消息
            if user_messages:
                user_content = "\n---\n".join(user_messages)
                req.contexts.append({"role": "user", "content": user_content})
            if assistant_messages:
                assistant_content = "\n---\n".join(assistant_messages)
                req.contexts.append({"role": "assistant", "content": assistant_content})

            # 构建 system 消息
            system_message = "You are now in a chatroom. The chat history is as above."
            if enable_active_reply:
                system_message += "\nNow, a new message is coming. Please react to it. Only output your response and do not output any other information."
            else:
                system_message += "\nNow, a new message is coming."

            # 将 system 消息插入到当前一次请求prompt的user字段前面
            # 也就是插入到contexts的最后面，因为下一个将是新的user prompt
            req.contexts.append({"role": "system", "content": system_message})

            # 当前用户消息作为新的 prompt
            req.prompt = prompt

    @filter.on_llm_response()
    async def after_req_llm(self, event: AstrMessageEvent, llm_resp: LLMResponse):
        """LLM 响应后,记录 AI 的回复"""
        if event.unified_msg_origin not in self.session_chats:
            return

        if llm_resp.completion_text:
            final_message = f"[You]: {llm_resp.completion_text}"
            logger.debug(
                f"记录 AI 响应: {event.unified_msg_origin} | {final_message}"
            )
            self.session_chats[event.unified_msg_origin].append(final_message)

            # 限制记录数量
            max_cnt = int(self.get_cfg("group_message_max_cnt", 300))
            if len(self.session_chats[event.unified_msg_origin]) > max_cnt:
                self.session_chats[event.unified_msg_origin].pop(0)

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """消息发送后处理,支持清除会话"""
        try:
            clean_session = event.get_extra("_clean_ltm_session", False)
            if clean_session:
                await self.remove_session(event)
        except Exception as e:
            logger.error(f"清理会话失败: {e}")

    async def remove_session(self, event: AstrMessageEvent) -> int:
        """移除指定会话的聊天记录"""
        cnt = 0
        if event.unified_msg_origin in self.session_chats:
            cnt = len(self.session_chats[event.unified_msg_origin])
            del self.session_chats[event.unified_msg_origin]
            logger.info(f"已清除 {event.unified_msg_origin} 的 {cnt} 条聊天记录")
        return cnt

    async def terminate(self):
        """插件卸载时的清理工作"""
        logger.info("群聊上下文感知插件已卸载")
