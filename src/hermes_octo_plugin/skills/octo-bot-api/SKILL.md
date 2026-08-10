---
name: octo-bot-api
version: 0.1.0
description: 在 Hermes 的 Octo 当前会话中安全发送消息、RichText、媒体和 Type-17 卡片，或执行受授权的 Octo 管理动作。
metadata: {"octo":{"category":"messaging"}}
---

# Octo 当前会话工具

## 路由与授权

插件从 Hermes task-local session 取得可信 Octo route。当前会话工具不接受 target、channel、requester、session 或身份覆盖；不要手工构造 Bot API 信封绕过该边界。

跨会话文本发送和管理操作使用 `octo_management`。它解析 DM、Group 和 Thread 目标，并在调用服务端前校验当前 Octo requester 的读取或 owner 权限。只有用户明确授权目标与动作时才执行 mutation。

## 文本与管理

Octo 平台的跨会话文本发送使用：

```text
octo_management(
  action="send-message",
  target="<user_uid> | <group_no> | <group_no>____<short_id>",
  content="...",
  reply_to_message_id="<optional>",
  mention_uids=["uid1", "uid2"],
  mention_all=false
)
```

不要改写 Thread 的 `{group_no}____{short_id}` 复合 ID。加入或离开 Thread 必须使用显式、受权限控制的 `join-thread` / `leave-thread` 动作。

## 当前会话输出

按输出语义直接选用工具；插件自动协商 capability，不需要也不存在单独的 card-profile 查询工具。

- `octo_send_rich_text`: 发送受控 text/image blocks 和 authoritative plain fallback。
- `octo_send_image`, `octo_send_file`, `octo_send_voice`, `octo_send_video`: 发送 HTTP(S) 或 Hermes 授权的本地媒体。不要尝试读取授权失败的路径。
- `octo_send_display_card`: 发送只读 Type-17 展示卡。
- `octo_send_interactive_card`: 发送绑定当前 requester/session 的交互卡。
- `octo_edit_card`: 编辑当前进程中仍有效、且与当前 requester/session 完全匹配的卡片。

所有工具都以结构化参数调用。不要传 raw Adaptive Card JSON，不要在参数中添加目标或身份字段。服务端不支持 Type-17 时，展示能力会按工具合同回退到同一会话的纯文本；交互动作只有在 message、channel、operator、binding 和 session 全部匹配时才接受。

Hermes `>=0.20.0,<0.21` 的 bounded `send_clarify` 由插件自动呈现为 Type-17 单选/多选卡；旧版本、未知版本或能力不兼容时使用 Hermes 文本 fallback。点击直接解析原 pending clarify，不创建新的用户 turn。

## 行为边界

- DM 直接回复当前用户；群聊只在 Hermes 已接纳当前消息时回复。
- 不主动向未指定的群或 Thread 发消息。
- 媒体本地路径必须先通过 Hermes 原生媒体授权；HTTP(S) 下载仍使用插件受保护 transport。
- Type-17 renderer 只做 schema、字段长度、节点、payload 和 action URL 协议校验，不应猜测或静默改写用户可见内容。
- 原始 Bot API、curl、注册和事件信封仅供运维与集成排障，见仓库 `docs/OCTO_BOT_API.md`；正常 Agent 工作流不要使用。
