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
        prompt = req.prompt
        rounds_limit = int(self.get_cfg("conversation_rounds_limit", 10))

        # 首先，清洗掉先前已经嵌入的system字段
        if req.contexts:
            # 过滤掉所有system角色的消息
            req.contexts = [ctx for ctx in req.contexts if ctx.get("role") != "system"]

        # 统计当前contexts中的user/assistant消息对数量
        pair_count = sum(1 for ctx in req.contexts if ctx.get("role") == "assistant")

        # 如果超过轮数限制，找到最后一个需要删除的assistant消息位置
        if pair_count > rounds_limit:
            # 需要删除的assistant消息数量
            remove_count = pair_count - rounds_limit
            assistant_count = 0
            cut_index = 0

            # 从前往后找到第 remove_count 个 assistant 消息的位置
            for i, ctx in enumerate(req.contexts):
                if ctx.get("role") == "assistant":
                    assistant_count += 1
                    if assistant_count == remove_count:
                        cut_index = i + 1  # 在这个assistant之后切割
                        break

            # 删除cut_index之前的所有消息
            req.contexts = req.contexts[cut_index:]
        

        # 获取配置的提示词
        if enable_active_reply:
            system_message = self.get_cfg("active_reply_prompt", "You are now in a chatroom. The chat history is as above. Now, new messages are coming. Please react to it. Only output your response and do not output any other information.")
        else:
            system_message = self.get_cfg("normal_reply_prompt", "You are now in a chatroom. The chat history is as above. Now, new messages are coming.")

        # 将 system 消息添加到上下文
        req.contexts.append({"role": "system", "content": system_message})

        # 构建会话历史
        chats_str = "\n---\n".join(self.session_chats[event.unified_msg_origin])

        # 会话历史作为新的 prompt
        req.prompt = chats_str
        
        # 清空该会话的历史记录，只保留上一次请求过后的群聊消息
        self.session_chats[event.unified_msg_origin].clear()


    async def terminate(self):
        """插件卸载时的清理工作"""
        logger.info("群聊上下文感知插件已卸载")
