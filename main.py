import datetime
import random
import traceback
import uuid
from collections import defaultdict
from typing import Optional, List, Tuple

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest, LLMResponse, Provider
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import At, Image, Plain, Forward, Reply
from astrbot.api.platform import MessageType
import astrbot.api.message_components as Comp

try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
    IS_AIOCQHTTP = True
except ImportError:
    IS_AIOCQHTTP = False


"""
群聊上下文感知插件
优化群聊上下文增强功能,提供群聊记录追踪、主动回复、图片描述等功能
"""

@register("group_context", "zz6zz666", "优化群聊上下文增强功能,提供群聊记录追踪、主动回复、图片描述、合并转发、指令过滤等功能", "1.0.0")
class GroupContextPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config  # AstrBotConfig继承自Dict,可以直接使用字典方法访问
        self.session_chats = defaultdict(list)
        """记录群成员的群聊记录"""
        self.active_reply_sessions = set()
        """记录当前是主动回复的会话"""

        # 合并转发相关配置
        self.enable_forward_analysis = bool(self.get_cfg("enable_forward_analysis", True))
        self.forward_prefix = "【合并转发内容】"

        # 指令过滤相关配置
        self.command_prefixes = self.get_cfg("command_prefixes", ["/"])

        # 图片处理相关配置
        self.enable_image_recognition = bool(self.get_cfg("enable_image_recognition", True))
        self.image_caption = bool(self.get_cfg("image_caption", False))
        self.image_caption_provider_id = self.get_cfg("image_caption_provider_id", "")

        logger.info("群聊上下文感知插件已初始化")
        logger.info(f"合并转发分析: {'已启用' if self.enable_forward_analysis else '已禁用'}")
        logger.info(f"指令前缀: {self.command_prefixes}")
        logger.info(f"图片识别: {'已启用' if self.enable_image_recognition else '已禁用'}")
        if self.enable_image_recognition:
            logger.info(f"图片处理模式: {'转述描述' if self.image_caption else 'URL注入'}")

    def get_cfg(self, key: str, default=None):
        """从插件配置中获取配置项"""
        return self.config.get(key, default)

    def is_command(self, message: str) -> bool:
        """判断消息是否是指令"""
        message = message.strip()
        if not message:
            return False
        for prefix in self.command_prefixes:
            if message.startswith(prefix):
                return True
        return False

    async def _detect_forward_message(self, event) -> Optional[str]:
        """检测合并转发消息并返回forward_id"""
        logger.debug(f"_detect_forward_message | IS_AIOCQHTTP={IS_AIOCQHTTP}, isinstance(event, AiocqhttpMessageEvent)={isinstance(event, AiocqhttpMessageEvent) if IS_AIOCQHTTP else 'N/A'}")

        if not IS_AIOCQHTTP or not isinstance(event, AiocqhttpMessageEvent):
            logger.debug("不符合合并转发检测条件，返回None")
            return None

        # 场景1: 直接发送的合并转发
        for seg in event.message_obj.message:
            if isinstance(seg, Comp.Forward):
                return seg.id
        
        # 场景2: 回复的合并转发
        reply_seg = None
        for seg in event.message_obj.message:
            if isinstance(seg, Comp.Reply):
                reply_seg = seg
                break

        if reply_seg:
            try:
                client = event.bot
                original_msg = await client.api.call_action('get_msg', message_id=reply_seg.id)
                
                if original_msg and 'message' in original_msg:
                    original_message_chain = original_msg['message']
                    if isinstance(original_message_chain, list):
                        for segment in original_message_chain:
                            if isinstance(segment, dict) and segment.get("type") == "forward":
                                return segment.get("data", {}).get("id")
            except Exception as e:
                logger.error(f"获取回复消息失败: {e}")

        return None

    async def _extract_forward_content(self, event, forward_id: str) -> Tuple[str, List[str]]:
        """提取合并转发消息的文本内容和图片URL

        返回: (文本内容, 图片URL列表)
        - 如果 enable_image_recognition = False，返回的图片URL列表为空
        - 如果 enable_image_recognition = True，返回所有图片URL
        """
        if not IS_AIOCQHTTP or not isinstance(event, AiocqhttpMessageEvent):
            return "", []

        try:
            # 调用API获取合并转发消息内容
            client = event.bot
            forward_data = await client.api.call_action('get_forward_msg', id=forward_id)
            messages = forward_data.get("messages", [])

            extracted_texts = []
            image_urls = []

            for message_node in messages:
                sender_name = message_node.get("sender", {}).get("nickname", "未知用户")
                raw_content = message_node.get("message") or message_node.get("content", [])

                # 解析消息内容
                node_text_parts = []
                for seg in raw_content:
                    if isinstance(seg, dict):
                        seg_type = seg.get("type")
                        seg_data = seg.get("data", {})

                        if seg_type == "text":
                            node_text_parts.append(seg_data.get("text", ""))
                        elif seg_type == "image":
                            if self.enable_image_recognition:
                                # 提取图片URL
                                img_url = seg_data.get("url") or seg_data.get("file")
                                if img_url:
                                    image_urls.append(img_url)
                                    node_text_parts.append("[图片]")
                            # 如果未启用图片识别，直接忽略图片
                        elif seg_type == "at":
                            node_text_parts.append(f"[At: {seg_data.get('qq', '')}]")

                full_node_text = "".join(node_text_parts).strip()
                if full_node_text:
                    extracted_texts.append(f"{sender_name}: {full_node_text}")

            final_text = "\n".join(extracted_texts)
            return final_text, image_urls

        except Exception as e:
            logger.error(f"提取合并转发内容失败: {e}")
            logger.error(traceback.format_exc())
            return "", []

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """处理群聊消息并支持主动回复"""
        # 仅支持群聊
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return

        # 检查是否有文本、图片或合并转发内容
        has_valid_content = False
        for comp in event.message_obj.message:
            if isinstance(comp, Plain) or isinstance(comp, Image):
                has_valid_content = True
                break
            # 合并转发消息需要立即处理，否则可能失效
            if IS_AIOCQHTTP and isinstance(comp, Forward):
                has_valid_content = True
                break

        if not has_valid_content:
            return

        # 检查是否为合并转发消息
        has_forward = False
        if IS_AIOCQHTTP and isinstance(event, AiocqhttpMessageEvent):
            for seg in event.message_obj.message:
                if isinstance(seg, Forward):
                    has_forward = True
                    break

        # 过滤指令消息（但保留合并转发消息）
        if not has_forward:
            message_text = event.message_str.strip()
            if self.is_command(message_text):
                logger.debug(f"[on_message] 跳过指令消息: {message_text}")
                return  # 指令消息不记录到上下文，也不触发主动回复

        # 检查是否需要主动回复
        need_active = await self.need_active_reply(event)

        # 记录对话
        try:
            await self.handle_message(event)
        except BaseException as e:
            logger.error(f"记录群聊消息失败: {e}")

        # 主动回复逻辑
        if need_active:
            # 标记当前会话为主动回复
            self.active_reply_sessions.add(event.unified_msg_origin)
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
        """记录群聊消息到上下文中

        图片处理逻辑：
        1. enable_image_recognition = False: 完全忽略所有图片
        2. enable_image_recognition = True, image_caption = False: 所有图片以URL形式注入
        3. enable_image_recognition = True, image_caption = True: 所有图片使用转述描述

        注意：指令消息过滤已在 on_message 中完成，这里不需要再次检查
        """

        # 1. 检测并处理合并转发消息
        forward_text = ""
        forward_images = []

        if self.enable_forward_analysis and IS_AIOCQHTTP:

            forward_id = await self._detect_forward_message(event)

            if forward_id:
                forward_text, forward_images = await self._extract_forward_content(event, forward_id)
                logger.info(f"检测到合并转发消息，提取了 {len(forward_text)} 字符和 {len(forward_images)} 张图片")
            else:
                logger.debug("未检测到合并转发消息")
        else:
            logger.debug(f"合并转发分析未启用或不支持当前平台")

        datetime_str = datetime.datetime.now().strftime("%H:%M:%S")
        parts = [f"[{event.message_obj.sender.nickname}/{datetime_str}]: "]

        # 2. 处理合并转发内容
        if forward_text:
            parts.append(f"\n{self.forward_prefix}\n\t<begin>\n{forward_text}\n\t<end>\n")

        # 3. 收集常规消息中的图片URL
        regular_images = []
        for comp in event.get_messages():
            if isinstance(comp, Plain):
                parts.append(f" {comp.text}")
            elif isinstance(comp, Image):
                if self.enable_image_recognition:
                    url = comp.url if comp.url else comp.file
                    if url:
                        regular_images.append(url)
                # 如果未启用图片识别，完全忽略图片
            elif isinstance(comp, At):
                parts.append(f" [At: {comp.name}]")

        # 5. 处理所有图片（常规 + 合并转发）
        all_images = regular_images + forward_images

        if all_images and self.enable_image_recognition:
            if self.image_caption and self.image_caption_provider_id:
                # 模式3: 图片转述描述
                for idx, img_url in enumerate(all_images):
                    try:
                        caption = await self.get_image_caption(img_url, self.image_caption_provider_id)
                        # 区分常规图片和合并转发图片
                        if idx < len(regular_images):
                            parts.append(f" [图片描述: {caption}]")
                        else:
                            parts.append(f" [合并转发图片描述: {caption}]")
                    except Exception as e:
                        logger.error(f"获取图片描述失败: {e}")
                        parts.append(" [图片]")
            else:
                # 模式2: URL注入
                for idx, img_url in enumerate(all_images):
                    if idx < len(regular_images):
                        parts.append(f" [图片URL: {img_url}]")
                    else:
                        parts.append(f" [合并转发图片URL: {img_url}]")

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
        is_active_reply = event.unified_msg_origin in self.active_reply_sessions
        if is_active_reply:
            system_message = self.get_cfg("active_reply_prompt", "You are now in a chatroom. The chat history is as above. Now, new messages are coming. Please react to it. Only output your response and do not output any other information.")
            # 清除主动回复标记
            self.active_reply_sessions.discard(event.unified_msg_origin)
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
