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
from astrbot.core.utils.io import download_image_by_url

try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
    IS_AIOCQHTTP = True
except ImportError:
    IS_AIOCQHTTP = False


"""
群聊上下文感知插件
优化群聊上下文增强功能,提供群聊记录追踪、主动回复、图片描述等功能
"""

@register("group_context", "zz6zz666", "优化群聊上下文增强功能,提供群聊记录追踪、主动回复、图片描述、合并转发、指令过滤等功能", "1.1.0")
class GroupContextPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config  # AstrBotConfig继承自Dict,可以直接使用字典方法访问
        self.session_chats = defaultdict(list)
        """记录群成员的群聊记录，每个元素是包含多模态内容的列表"""
        self.active_reply_sessions = set()
        """记录当前是主动回复的会话"""

        # 合并转发相关配置
        self.enable_forward_analysis = bool(self.get_cfg("enable_forward_analysis", True))
        self.forward_prefix = "【合并转发内容】"

        # 图片处理相关配置
        self.enable_image_recognition = bool(self.get_cfg("enable_image_recognition", True))
        self.image_caption = bool(self.get_cfg("image_caption", False))
        self.image_caption_provider_id = self.get_cfg("image_caption_provider_id", "")
        self.image_carry_rounds = int(self.get_cfg("image_carry_rounds", 1))

        self.active_reply_prompt = self.get_cfg("active_reply_prompt", "You are now in a chatroom. The chat history is as above. Now, new messages are coming. Please react to it. Only output your response and do not output any other information.")
        self.normal_reply_prompt = self.get_cfg("normal_reply_prompt", "You are now in a chatroom. The chat history is as above. Now, new messages are coming. Please react to it.")

        # 私聊场景控制配置
        self.enable_private_control = bool(self.get_cfg("enable_private_control", False))
        self.private_conversation_rounds_limit = int(self.get_cfg("private_conversation_rounds_limit", 10))
        self.private_image_carry_rounds = int(self.get_cfg("private_image_carry_rounds", 5))

        # 指令消息过滤配置
        self.enable_command_filter = bool(self.get_cfg("enable_command_filter", True))
        self.command_prefixes = self.get_cfg("command_prefixes", ["/"])

        logger.info("群聊上下文感知插件已初始化")
        logger.info(f"合并转发分析: {'已启用' if self.enable_forward_analysis else '已禁用'}")
        logger.info(f"图片识别: {'已启用' if self.enable_image_recognition else '已禁用'}")
        if self.enable_image_recognition:
            logger.info(f"图片处理模式: {'转述描述' if self.image_caption else 'URL注入'}")
            logger.info(f"图片携带轮数: {self.image_carry_rounds}")
        logger.info(f"私聊控制: {'已启用' if self.enable_private_control else '已禁用'}")
        if self.enable_private_control:
            logger.info(f"私聊对话轮数: {self.private_conversation_rounds_limit}")
            logger.info(f"私聊图片携带轮数: {self.private_image_carry_rounds}")

    def get_cfg(self, key: str, default=None):
        """从插件配置中获取配置项"""
        return self.config.get(key, default)

    def is_command(self, message: str) -> bool:
        """检测是否为指令消息"""
        if not self.enable_command_filter or not message:
            return False
        message = message.strip()
        for prefix in self.command_prefixes:
            if message.startswith(prefix):
                return True
        return False

    def _extract_image_url(self, image_data) -> Optional[str]:
        """从不同格式的图片数据中提取URL

        支持的格式：
        1. 字符串URL: "https://example.com/image.jpg"
        2. 字典格式: {"url": "https://..."}
        3. OpenAI标准格式: {"image_url": {"url": "data:image/jpeg;base64,..."}}
        4. Image组件对象: Image.url 或 Image.file
        """
        if not image_data:
            return None

        # 情况1: 字符串URL
        if isinstance(image_data, str):
            return image_data

        # 情况2: 字典格式
        if isinstance(image_data, dict):
            # OpenAI标准格式: {"image_url": {"url": "..."}}
            if "image_url" in image_data:
                image_url_obj = image_data["image_url"]
                if isinstance(image_url_obj, dict) and "url" in image_url_obj:
                    return image_url_obj["url"]
            # 简单字典格式: {"url": "..."}
            if "url" in image_data:
                return image_data["url"]

        # 情况3: Image组件对象
        if isinstance(image_data, Image):
            if hasattr(image_data, 'url') and image_data.url:
                return image_data.url
            if hasattr(image_data, 'file') and image_data.file:
                return image_data.file

        return None

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
                                # 提取图片URL，支持多种格式
                                img_url = self._extract_image_url(seg_data)
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

        # 提取文本内容用于指令检测
        message_text = ""
        for comp in event.message_obj.message:
            if isinstance(comp, Plain):
                message_text += comp.text

        # 过滤指令消息
        if self.is_command(message_text):
            logger.debug(f"群聊上下文 | {event.unified_msg_origin} | 检测到指令消息，已过滤")
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
        2. enable_image_recognition = True, image_caption = False: 所有图片以URL形式注入，保留原始位置
        3. enable_image_recognition = True, image_caption = True: 所有图片使用转述描述，保留原始位置

        注意：指令消息过滤已在 on_message 中完成，这里不需要再次检查
        """

        datetime_str = datetime.datetime.now().strftime("%H:%M:%S")
        
        # 创建当前消息的多模态内容列表
        current_message_content = []
        
        # 合并后的完整文本内容，只有遇到图片时才插入image_url块
        full_text = f"[{event.message_obj.sender.nickname}/{datetime_str}]: "
        
        # 1. 检测并处理合并转发消息
        if self.enable_forward_analysis and IS_AIOCQHTTP:

            forward_id = await self._detect_forward_message(event)

            if forward_id:
                # 提取合并转发的原始消息结构，包括位置信息
                if IS_AIOCQHTTP and isinstance(event, AiocqhttpMessageEvent):
                    try:
                        client = event.bot
                        forward_data = await client.api.call_action('get_forward_msg', id=forward_id)
                        messages = forward_data.get("messages", [])
                        
                        # 添加合并转发前缀
                        full_text += f"\n{self.forward_prefix}\n\t<begin>\n"
                        
                        for message_node in messages:
                            sender_name = message_node.get("sender", {}).get("nickname", "未知用户")
                            raw_content = message_node.get("message") or message_node.get("content", [])
                            
                            # 发送者名称作为消息开头
                            full_text += f"{sender_name}: "
                            
                            # 解析并合并原始消息结构
                            for seg in raw_content:
                                if isinstance(seg, dict):
                                    seg_type = seg.get("type")
                                    seg_data = seg.get("data", {})
                                    
                                    if seg_type == "text":
                                        # 合并文本
                                        full_text += seg_data.get("text", "")
                                    elif seg_type == "at":
                                        # @ 也作为文本处理
                                        full_text += f"[At: {seg_data.get('qq', '')}]"
                                    elif seg_type == "image":
                                        img_url = self._extract_image_url(seg_data)
                                        if img_url:
                                            if self.enable_image_recognition:
                                                if self.image_caption:
                                                    try:
                                                        caption = await self.get_image_caption(img_url, self.image_caption_provider_id)
                                                        # 图片描述作为文本处理
                                                        full_text += f" [图片描述: {caption}]"
                                                    except Exception as e:
                                                        logger.error(f"获取图片描述失败: {e}")
                                                        full_text += " [图片]"
                                                else:
                                                    # 遇到图片URL时，先将之前的文本添加到列表
                                                    if full_text:
                                                        current_message_content.append({"type": "text", "text": full_text})
                                                        full_text = ""  # 重置当前文本
                                                    # 将图片转换为base64编码，使用OpenAI格式
                                                    image_data = await self._encode_image_bs64(img_url)
                                                    if image_data:
                                                        current_message_content.append({"type": "image_url", "image_url": {"url": image_data}})
                                                    else:
                                                        # 如果转换失败，使用[图片]占位符
                                                        full_text += " [图片]"
                                            else:
                                                # 关闭视觉开关时，使用[图片]占位符，不换行
                                                full_text += " [图片]"
                            
                            # 添加换行
                            full_text += "\n"
                        
                        # 添加合并转发后缀
                        full_text += "\t<end>\n"
                        forward_has_content = True
                        logger.info(f"检测到合并转发消息，已保留原始结构")
                    except Exception as e:
                        logger.error(f"处理合并转发消息失败: {e}")
                        logger.error(traceback.format_exc())
                else:
                    logger.debug("未检测到合并转发消息")
        else:
            logger.debug(f"合并转发分析未启用或不支持当前平台")

        # 2. 处理常规消息内容，构建连续的内容流
        for comp in event.message_obj.message:
            if isinstance(comp, Plain):
                # 合并连续的文本
                full_text += comp.text
            elif isinstance(comp, At):
                # @ 也作为文本处理
                full_text += f" [At: {comp.name if hasattr(comp, 'name') else comp.qq}]"
            elif isinstance(comp, Image):
                url = self._extract_image_url(comp)
                if url:
                    if self.enable_image_recognition:
                        if self.image_caption:
                            try:
                                caption = await self.get_image_caption(url, self.image_caption_provider_id)
                                # 图片描述作为文本处理，保持在同一行
                                full_text += f" [图片描述: {caption}]"
                            except Exception as e:
                                logger.error(f"获取图片描述失败: {e}")
                                # 图片描述获取失败时，使用[图片]占位符，保持在同一行
                                full_text += " [图片]"
                        else:
                            # 遇到图片URL时，先将之前的文本添加到列表
                            if full_text:
                                current_message_content.append({"type": "text", "text": full_text})
                                full_text = ""  # 重置当前文本
                            # 将图片转换为base64编码，使用OpenAI格式
                            image_data = await self._encode_image_bs64(url)
                            if image_data:
                                current_message_content.append({"type": "image_url", "image_url": {"url": image_data}})
                            else:
                                # 如果转换失败，使用[图片]占位符
                                full_text += " [图片]"
                    else:
                        # 关闭视觉开关时，使用[图片]占位符，保持在同一行
                        full_text += " [图片]"
            elif isinstance(comp, Forward):
                # 合并转发消息已在前面处理
                pass
        
        # 处理最后剩余的文本
        if full_text:
            current_message_content.append({"type": "text", "text": full_text})
        
        # 只有当有实际内容时才添加到会话历史
        if current_message_content:
            # 将当前消息的多模态内容添加到会话历史
            self.session_chats[event.unified_msg_origin].append(current_message_content)
            
            # 调试日志
            logger.debug(f"群聊上下文 | {event.unified_msg_origin} | 添加了一条包含 {len(current_message_content)} 个组件的消息")

    async def _encode_image_bs64(self, image_url: str) -> str:
        """将图片转换为 base64 编码
        
        支持的格式：
        1. base64://... 格式的 base64 数据
        2. http/https 开头的网络图片 URL
        3. file:/// 开头的本地文件路径
        4. 直接的本地文件路径
        """
        try:
            import base64
            
            if image_url.startswith("base64://"):
                return image_url.replace("base64://", "data:image/jpeg;base64,")
            elif image_url.startswith("http"):
                # 下载网络图片
                image_path = await download_image_by_url(image_url)
                with open(image_path, "rb") as f:
                    image_bs64 = base64.b64encode(f.read()).decode("utf-8")
                return "data:image/jpeg;base64," + image_bs64
            elif image_url.startswith("file:///"):
                # 本地文件路径
                image_path = image_url.replace("file:///", "")
                with open(image_path, "rb") as f:
                    image_bs64 = base64.b64encode(f.read()).decode("utf-8")
                return "data:image/jpeg;base64," + image_bs64
            else:
                # 直接的本地文件路径
                with open(image_url, "rb") as f:
                    image_bs64 = base64.b64encode(f.read()).decode("utf-8")
                return "data:image/jpeg;base64," + image_bs64
        except Exception as e:
            logger.error(f"将图片转换为base64失败: {image_url}, 错误: {e}")
            return ""

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

        # 使用概率触发主动回复
        ar_possibility = float(self.get_cfg("ar_possibility", 0.1))
        return random.random() < ar_possibility

    def _control_conversation_rounds(self, req: ProviderRequest, rounds_limit: int):
        """控制对话轮数，保留最近N轮user/assistant消息对"""
        if not req.contexts or rounds_limit <= 0:
            return
            
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
    
    def _control_image_carry_rounds(self, req: ProviderRequest, image_carry_rounds: int):
        """控制图片携带轮数，只保留最后N个user消息中的图片"""
        if not req.contexts or image_carry_rounds <= 0:
            return
            
        # 找出所有user角色的消息索引
        user_indices = [i for i, ctx in enumerate(req.contexts) if ctx.get("role") == "user"]
        
        # 如果user消息数量超过image_carry_rounds，只保留最后N个
        if len(user_indices) > image_carry_rounds:
            # 需要保留图片的user消息索引
            keep_indices = user_indices[-image_carry_rounds:]
            
            # 遍历所有user消息
            for i in user_indices:
                # 如果不是需要保留的user消息，将图片替换为[图片]占位符
                if i not in keep_indices:
                    ctx = req.contexts[i]
                    if isinstance(ctx.get("content"), list):
                        # 创建新的content列表
                        new_content = []
                        current_text = None
                        
                        for item in ctx["content"]:
                            if item["type"] == "text":
                                text = item["text"]
                                
                                # 检查是否为新的时间戳（以[开头）
                                if text.startswith("["):
                                    # 如果是新的时间戳，保存当前文本（如果有）
                                    if current_text:
                                        new_content.append({"type": "text", "text": current_text})
                                    # 开始新的文本块
                                    current_text = text
                                else:
                                    # 如果不是新的时间戳，合并到当前文本
                                    if current_text:
                                        current_text += text
                                    else:
                                        # 如果没有当前文本，直接创建新的
                                        current_text = text
                            elif item["type"] == "image_url":
                                # 如果是图片，将[图片]追加到当前文本
                                if current_text:
                                    current_text += " [图片]"
                                else:
                                    # 如果没有当前文本，创建一个新的
                                    current_text = " [图片]"
                        
                        # 保存最后一个文本块
                        if current_text:
                            new_content.append({"type": "text", "text": current_text})
                        
                        # 更新为新的content列表
                        ctx["content"] = new_content

    @filter.on_llm_request()
    async def on_req_llm(self, event: AstrMessageEvent, req: ProviderRequest):
        """当触发 LLM 请求前,调用此方法修改 req（群聊场景）"""
        if event.unified_msg_origin not in self.session_chats:
            return

        # 获取群聊的会话轮数限制
        rounds_limit = int(self.get_cfg("conversation_rounds_limit", 10))
        
        # 首先，清洗掉先前已经嵌入的system字段
        req.contexts = [
            ctx for ctx in req.contexts 
            if not (ctx.get("role") == "system" and (ctx.get("content", "").startswith(self.active_reply_prompt[:30]) or ctx.get("content", "").startswith(self.normal_reply_prompt[:30])))
        ]

        # 控制对话轮数
        self._control_conversation_rounds(req, rounds_limit)
        
        # 控制图片携带轮数
        self._control_image_carry_rounds(req, self.image_carry_rounds)
        
        # 获取配置的提示词
        is_active_reply = event.unified_msg_origin in self.active_reply_sessions
        if is_active_reply:
            system_message = self.active_reply_prompt
            # 清除主动回复标记
            self.active_reply_sessions.discard(event.unified_msg_origin)
        else:
            system_message = self.normal_reply_prompt

        # 将 system 消息添加到上下文
        req.contexts.append({"role": "system", "content": system_message})

        # 构建会话历史 - 转换为OpenAI兼容的多模态格式
        combined_content = []
        # 同时构建纯文本prompt，图片用[图片]占位
        text_prompt_parts = []
        
        for message in self.session_chats[event.unified_msg_origin]:
            combined_content.extend(message)
            
            # 构建纯文本prompt部分
            text_part = ""
            for comp in message:
                if comp["type"] == "text":
                    text_part += comp["text"]
                elif comp["type"] == "image_url":
                    text_part += " [图片]"
            
            if text_part.strip():
                text_prompt_parts.append(text_part.strip())
        
        # 构建纯文本prompt，用---分割（允许其他插件的llm+request钩子获取prompt内容）
        req.prompt = ""
        if text_prompt_parts:
            req.prompt = "\n---\n".join(text_prompt_parts)
        
        logger.debug(f"构建的prompt: \n{req.prompt}")

        # 创建用户角色的多模态消息
        user_message = {
            "role": "user",
            "content": combined_content
        }
        
        # 将用户消息添加到上下文
        req.contexts.append(user_message)
        
        # 清空该会话的历史记录，只保留上一次请求过后的群聊消息
        self.session_chats[event.unified_msg_origin].clear()

    @filter.on_llm_request()
    async def on_req_llm_private(self, event: AstrMessageEvent, req: ProviderRequest):
        """私聊场景的LLM请求处理，实现对话轮数和图片携带轮数控制"""
        # 仅处理私聊消息且已启用私聊控制
        if not (self.enable_private_control and hasattr(event, 'get_message_type') and 
                event.get_message_type() == MessageType.PRIVATE_MESSAGE):
            return

        # 使用私聊场景的配置
        rounds_limit = self.private_conversation_rounds_limit
        image_carry_rounds = self.private_image_carry_rounds

        # 控制对话轮数
        self._control_conversation_rounds(req, rounds_limit)
        
        # 控制图片携带轮数
        self._control_image_carry_rounds(req, image_carry_rounds)
    
    @filter.on_llm_request(priority=-10000)
    async def on_req_llm_clear_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        """在所有插件处理完后，将prompt清空，并清除上下文中空的user字段"""
        # 只有群聊场景下才执行操作
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            logger.debug(f"非群聊消息，不执行清空prompt操作")
            return
        
        # 清空prompt，避免请求体包含重复内容
        req.prompt = ""
        
        # 清除上下文中空的user字段
        if req.contexts:
            req.contexts = [
                ctx for ctx in req.contexts 
                if not (ctx.get("role") == "user" and 
                       (ctx.get("content") == "" or 
                        (isinstance(ctx.get("content"), list) and not ctx.get("content"))))
            ]


    @filter.on_llm_response(priority=-10000)
    async def save_memories(self, event: AstrMessageEvent, resp: LLMResponse):
        # 只有群聊场景下才执行操作
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            logger.debug(f"非群聊消息，不执行清空prompt操作")
            return
        
        # 再次prompt，防止其他插件在请求前保存了prompt并在请求后又注入进去
        req = event.get_extra("provider_request")
        if req is not None:
            req.prompt = ""


    async def terminate(self):
        """插件卸载时的清理工作"""
        logger.info("群聊上下文感知插件已卸载")
