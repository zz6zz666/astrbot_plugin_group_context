# 群聊上下文插件功能更新说明

## 新增功能

### 1. 合并转发支持 ✨

自动提取QQ合并转发消息内容（仅aiocqhttp平台）

**支持场景：**
- ✅ 直接发送的合并转发消息
- ✅ 回复引用的合并转发消息

**相关配置：**
- `enable_forward_analysis`: 是否启用合并转发分析（默认: true）
- `forward_prefix`: 合并转发内容前缀标识（默认: "【合并转发内容】\n"）

**工作原理：**
插件会自动检测合并转发消息，调用QQ API提取其中的文本和图片内容，并将提取的内容注入到群聊上下文中。

---

### 2. 指令智能过滤 🔍

自动识别并跳过指令消息，防止指令干扰上下文

**相关配置：**
- `command_prefixes`: 指令前缀列表（默认: `["/"]`）

**示例：**
```json
{
  "command_prefixes": ["/", "#", "!", "＃"]
}
```

**工作原理：**
以配置的前缀开头的消息（如 `/help`, `#image`, `!info` 等）会被自动识别为指令，不会记录到群聊上下文中。

---

### 3. 增强的图片处理机制 🖼️

重新设计的图片处理逻辑，支持三种模式

#### 图片处理模式

| 模式 | enable_image_recognition | image_caption | 行为 |
|------|-------------------------|---------------|------|
| **模式1：完全忽略** | false | - | 忽略所有图片（包括常规和合并转发） |
| **模式2：URL注入** | true | false | 所有图片以URL形式注入到上下文 |
| **模式3：转述描述** | true | true | 使用AI模型描述图片内容 |

**相关配置：**
- `enable_image_recognition`: 是否启用群聊图片识别（默认: true）
- `image_caption`: 是否启用图片描述功能（默认: false）
- `image_caption_prompt`: 图片描述提示词（默认: "请描述这张图片的内容"）
- `image_caption_provider_id`: 用于图片描述的 Provider ID（留空使用当前Provider）

#### 示例配置

**场景1：只识别图片URL，不使用AI描述**
```json
{
  "enable_image_recognition": true,
  "image_caption": false
}
```
效果：群聊消息中的图片会以 `[图片URL: https://...]` 的形式注入到上下文中。

**场景2：使用AI描述图片**
```json
{
  "enable_image_recognition": true,
  "image_caption": true,
  "image_caption_provider_id": "your_provider_id"
}
```
效果：群聊消息中的图片会被AI分析，以 `[图片描述: ...]` 的形式注入到上下文中。

**场景3：完全忽略图片**
```json
{
  "enable_image_recognition": false
}
```
效果：所有图片都不会出现在上下文中。

---

## 完整配置示例

```json
{
  "enable_image_recognition": true,
  "image_caption": false,
  "image_caption_prompt": "请描述这张图片的内容",
  "image_caption_provider_id": "",
  "enable_forward_analysis": true,
  "forward_prefix": "【合并转发内容】\n",
  "command_prefixes": ["/"],
  "enable_active_reply": false,
  "ar_method": "possibility_reply",
  "ar_possibility": 0.1,
  "active_reply_prompt": "You are now in a chatroom. The chat history is as above. Now, new messages are coming. Please react to it. Only output your response and do not output any other information.",
  "normal_reply_prompt": "You are now in a chatroom. The chat history is as above. Now, new messages are coming.",
  "conversation_rounds_limit": 10,
  "ar_whitelist": []
}
```

---

## 技术细节

### 合并转发提取流程

1. 检测消息中的 Forward 组件或 Reply 组件
2. 如果是 Reply，获取被回复的原始消息
3. 通过 QQ API (`get_forward_msg`) 提取合并转发内容
4. 解析每条消息的发送者、文本、图片等信息
5. 格式化后注入到群聊上下文

### 图片处理流程

1. 收集常规消息中的图片URL
2. 收集合并转发消息中的图片URL
3. 根据配置选择处理模式：
   - 模式1：直接跳过
   - 模式2：以URL形式添加到上下文
   - 模式3：调用AI Provider获取描述后添加到上下文

### 指令过滤流程

1. 检查消息文本是否以配置的前缀开头
2. 如果是指令，直接返回，不记录到上下文
3. 否则正常处理消息

---

## 注意事项

⚠️ **合并转发功能仅支持 aiocqhttp 平台**
- 其他平台会自动跳过合并转发处理
- 插件会在启动时输出平台兼容性信息

⚠️ **图片转述需要配置支持视觉的 Provider**
- 确保配置的 Provider 支持图片输入
- 留空 `image_caption_provider_id` 将使用当前会话的 Provider

⚠️ **指令前缀配置为列表**
- 支持多个前缀，如 `["/", "#", "!"]`
- 前缀区分大小写

---

## 日志输出示例

插件启动时会输出配置信息：
```
[INFO] 群聊上下文感知插件已初始化
[INFO] 合并转发分析: 已启用
[INFO] 指令前缀: ['/']
[INFO] 图片识别: 已启用
[INFO] 图片处理模式: URL注入
```

检测到合并转发时：
```
[INFO] 检测到合并转发消息，提取了 245 字符和 3 张图片
```

跳过指令时：
```
[DEBUG] 跳过指令消息: /help
```

---

## 更新日期

2025-12-12
