# Octo 全功能协议能力矩阵

> 状态：本地实现与审查完成，待逐项授权的实服验收
> 基线：`b5661d06f96a8d82fff5140602a65df566d4849b`
> 分支：`feat/protocol-parity-hardening`
> 最后核对：2026-08-06

本文档回答三个问题：当前插件已经能做什么、仍缺什么、每一项如何验收。实现状态以代码和测试为准，不以产品宣传或旧审计结论为准。

## 1. 权威来源与边界

协议来源按以下优先级裁决：

1. Octo server / OpenAPI 实现与实服响应；
2. `openclaw-channel-octo` 已验证实现；
3. Octo Web 客户端的渲染与交互实现；
4. 本仓库既有测试；
5. 文档和推断只作为线索，不得代替线协议证据。

参考检出：

- 本仓库：`src/hermes_octo_plugin/`
- OpenClaw 参考：独立只读审计 checkout
- Octo server：独立只读审计 checkout
- Octo Web：独立只读审计 checkout
- Hermes core：当前与最低声明版本的隔离安装

本阶段不修改 Hermes core。若 Hermes 没有专用平台扩展点，优先使用插件生命周期 hook、session ContextVar 和 adapter 方法完成集成；禁止 monkeypatch core。

## 2. 状态定义

- **DONE**：有实现、有自动化测试，且协议行为已经核验。
- **PARTIAL**：协议的一部分存在，但缺少出站、工具面、生命周期或实服证据。
- **MISSING**：当前代码没有该能力。
- **BLOCKED-UPSTREAM**：客户端可实现，但服务端/客户端尚无可用契约或实服明确拒绝。
- **VERIFY-LIVE**：自动化已覆盖，仍需实服无破坏性验证。

## 3. 当前能力矩阵

### 3.1 连接、身份与传输

| 能力 | 状态 | 当前证据 | 目标/验收 |
|---|---|---|---|
| Bot 注册、ECDH/AES、WuKongIM WS | DONE | `adapter.py`, `protocol.py` | 连接日志与收发回归通过 |
| WS / HTTP 双心跳 | DONE | `tests/test_heartbeat.py` | 独立周期、错误退避、404 disable |
| 重连、取消安全、资源所有权 | DONE | `tests/test_reconnect.py`, `tests/test_streaming.py` | 无 session/task 泄漏；文本回复不创建编辑 watchdog |
| 可信 requester 身份 | DONE | `agent_tools.py`, `permission.py` | `requester_uid` 必须匹配 ContextVar |
| `on_behalf_of` persona 身份 | VERIFY-LIVE | `OCTO_ON_BEHALF_OF` 只从 adapter 配置读取；text/typing/RichText/media/普通 edit 全链路透传，Type-17/progress 在该模式安全降级为纯文本；待实服授权核对 grant |
| DM / Group / Thread / Space 会话隔离 | DONE | `tests/test_context_isolation.py` | 无缓存、历史、成员跨域污染 |
| REST / WS 错误脱敏 | DONE | `OctoApiError`, security tests | 不泄露 token、正文、签名 URL |
| SSRF、redirect、metadata 防护 | DONE | guarded connector/download tests | literal/DNS/redirect 均 fail-closed |

### 3.2 入站消息

| 类型/能力 | 状态 | 说明 |
|---|---|---|
| Text(1) | DONE | 内容、mention、reply、history |
| Image(2) / GIF(3) / Voice(4) / Video(5) / File(8) | DONE | 下载、MEDIA 路由、元数据、大小限制 |
| Location(6) / Card(7) | DONE | 安全纯文本降级 |
| MultipleForward(11) | DONE | 递归纯文本解析与深度/大小边界 |
| RichText(14) | DONE | blocks + authoritative plain；图片块占位 |
| InteractiveCard(17) | VERIFY-LIVE | 安全读取 `plain`，过滤 `hidden_reasoning`；Type-17 出站、progress 与 action 生命周期已有自动化覆盖，待实服往返 |
| 未知消息类型 | DONE | 不伪装成 Text，不读取 raw payload；按数值 type 输出稳定占位，并用有界计数/日志预算记录 telemetry |
| @all / humans / ais | DONE | 三态解析、mention gate、bot loop guard |
| read receipt / typing | DONE | best-effort，不阻断主链路 |

### 3.3 普通出站与编辑

| 能力 | 状态 | 当前证据 | 缺口 |
|---|---|---|---|
| Text 发送 | DONE | `api.send_message()` | 无 |
| Hermes 完整文本回复 | DONE | `SUPPORTS_MESSAGE_EDITING=False` + `api.send_message()`；`tests/test_streaming.py` | Hermes 在 turn 完成后发送一条权威正文，避免首 token 碎片被误判为终答 |
| server-backed text streaming / 普通消息 edit | BLOCKED-UPSTREAM | 实服 `/v1/bot/stream/start` 返回 404；`message/edit` 可写 `content_edit` 但不是可靠的客户端可见文本流 | 不用于 Hermes 回复；服务端与客户端提供明确、可验收的 Bot streaming 合同后再启用 |
| 图片、文件、语音、视频 | VERIFY-LIVE | adapter `send_*` + backend-agnostic presigned upload；当前会话媒体工具 | 与 OpenClaw 一致支持本地路径、`file://`、HTTP(S)，native adapter 支持 `data:`；按 server 100 MiB 上限上传；保留并重放服务端 `uploadUrl` / `downloadUrl` / signed headers；待实服上传 |
| RichText(14) | VERIFY-LIVE | `api.send_rich_text_message()` + `octo_send_rich_text` | 受控 text/image blocks 与 authoritative plain 已覆盖；待实服发送 |
| InteractiveCard(17) send | VERIFY-LIVE | `api.send_card_message()`、安全 renderer、当前会话工具 | Type-17 envelope/profile/identity 自动化已覆盖；待实服发送 |
| InteractiveCard(17) edit | VERIFY-LIVE | `api.edit_card_message()`、progress、session-bound `octo_edit_card` | 完整 frame、`card_seq`、`transient`、终态自动化已覆盖；待实服编辑 |
| card profile manifest | VERIFY-LIVE | `api.get_card_profile()` + adapter-local 60s cache | 404/disabled 区分、bot-scoped cache、legacy opt-in fallback 已覆盖；待实服 manifest |
| template-ref/v1 | VERIFY-LIVE | `api.send_template_card_message()` / `edit_template_card_message()`；`OCTO_PROGRESS_CARD_RENDERER=registry` 显式启用，manifest 不兼容时回退本地 Type-17 | 自动化覆盖 Registry 首发、编辑、终态、失败恢复；待实服模板验收 |
| 工具进度卡 | VERIFY-LIVE | 默认 `local`：中文 Type-17 执行轨迹、工具参数/结果安全摘要、耗时、运行/成功/失败状态、终态折叠；仅首次真实工具调用时创建 | 不展示 hidden CoT；待 xiao_ai 实服客户端视觉与逐步更新验收 |
| Hermes `send_clarify` | VERIFY-LIVE | `>=0.20.0`：当前会话 Type-17 单选/多选/Other；低版本走 Hermes 文本 fallback | 运行时 PEP 440 版本门控、pending entry 对账、直接 `resolve_gateway_clarify` / `mark_awaiting_text`、重放与失败降级已有自动化覆盖；待实服交互 |
| on-behalf-of Type-17 | BLOCKED-UPSTREAM | 参考实现说明 server 拒绝 | 不绕过；普通 bot 发卡不允许模型伪造身份 |

### 3.4 管理工具面

`octo_management` 当前已有：

- `list-groups`, `group-info`, `group-members`, `search-members`, `search-shared-groups`
- `read-messages`, `send-message`
- `group-md-read`, `group-md-update`
- `create-group`, `update-group`, `add-members`, `remove-members`
- `create-thread`, `list-threads`, `get-thread`, `delete-thread`
- `list-thread-members`, `join-thread`, `leave-thread`
- `thread-md-read`, `thread-md-update`
- `voice-context-read`, `voice-context-update`, `voice-context-delete`

新增当前可信会话工具：

| 工具/动作 | 状态 | 当前合同 |
|---|---|---|
| `octo_send_rich_text` | VERIFY-LIVE | 受控 text/image blocks，保留 authoritative `plain` fallback |
| `octo_send_image` / `octo_send_file` / `octo_send_voice` / `octo_send_video` | VERIFY-LIVE | 与 OpenClaw 插件一致支持本地路径、`file://` 或 HTTP(S) URL，按 server 100 MiB 上限重新上传 |
| `octo_send_display_card` | VERIFY-LIVE | 结构化 blocks；目标来自可信 ContextVar；unsupported 回退同会话纯文本 |
| `octo_send_interactive_card` | VERIFY-LIVE | 受控 inputs/buttons；可信 session binding；登记 action session |
| `octo_card_profile` | VERIFY-LIVE | 只读返回协商后的能力、限制与 authoritative 状态 |
| `octo_edit_card` | VERIFY-LIVE | 仅终态编辑当前注册的 interactive card session；精确 session/channel/requester 与 `card_seq` claim |
| GROUP.md / THREAD.md 并发版本条件 | BLOCKED-UPSTREAM | GET 返回 version，但已核验的 server/OpenClaw 写合同只有 `content`，无 if-version、ETag 或 `expected_version` |
| Thread join/leave 语义 | VERIFY-LIVE | owner-only 工具边界与 API 已覆盖；非 creator 服务端权限结果仍需实服样例 |

管理工具持续遵守：

- 所有动作必须有可信 Octo session requester；
- 跨群/线程读写先做成员/owner 校验；
- 管理 mutation owner-only；
- 交互卡默认只发当前可信会话，不允许模型在参数里选择任意目标；
- 不把用户点击当作业务授权证明。

### 3.5 Type-17 / 进度卡 / 交互卡

#### 协议合同

出站 Type-17 信封：

```json
{
  "channel_id": "...",
  "channel_type": 1,
  "client_msg_no": "...",
  "payload": {
    "type": 17,
    "card": {"type": "AdaptiveCard", "version": "1.5", "body": []},
    "plain": "fallback",
    "profile": "octo/v1",
    "card_version": "1.5"
  }
}
```

交互元素（`Input.*` 或 `Action.Submit`）必须自动升级为 `octo/v2`。发送前必须读取 manifest：

```text
GET /v1/bot/card/profile
```

卡片编辑：

```text
POST /v1/bot/message/edit
```

要求：完整 Type-17 `content_edit`、单调 `card_seq`、过程帧 `transient=true`、终态 `transient=false`。

事件：

```text
POST /v1/bot/events
body: {"event_id": <cursor>, "limit": <n>, "wait": <seconds, optional>}
POST /v1/bot/events/{event_id}/ack
```

`card_action` 必须经过：envelope 校验 → message/session/channel 身份校验 → input id/大小/敏感信息校验 → 幂等 claim → 按 card session 类型分发 → cursor 持久化 → ack。普通交互卡作为同一 Octo 会话的新用户输入；Hermes `>=0.20.0` clarify 卡直接调用 `resolve_gateway_clarify` 或 `mark_awaiting_text`，绝不注入 `MessageEvent`。持久化必须先于 ack。

#### 已实现的进度卡合同

- 使用 `octo/v1` 展示卡，metadata layout=`agent_progress_v1`；
- `pre_llm_call` 表示安全的“思考中”，不显示隐藏 Chain-of-Thought；
- `pre_tool_call` / `post_tool_call` 更新工具步骤；
- 摘要只按工具 allowlist 提取，未知/MCP 参数默认不显示；
- URL 只保留 scheme + registrable domain；shell 只显示程序名；路径最多保留末两段；
- 每个 turn 只保留最近 32 条工具摘要，避免长任务无限增长；
- secret-shape、token、password、authorization、签名 URL 命中即整段隐藏；
- 首帧发送卡，后续用 transient edit，完成/错误/中断写 final frame；
- card 不取代最终答案，最终答案由 Hermes 缓冲完成后通过普通 Text 消息一次发送；
- capability 不支持时 fail-soft：回退现有文字进度或静默，绝不阻断答案。

#### 已实现的 clarify 合同

- 运行时读取并按 PEP 440 解析 `hermes-agent` 版本一次；`>=0.20.0` 启用原生 Type-17 clarify，`0.14`–`0.19`、未知或不可解析版本调用 Hermes 基类文本 fallback；
- 必须同时匹配 gateway pending `clarify_id/session_key/question/choices/multi_select` 与可信当前 Octo route；不接受模型提供 channel、requester、session 或答案映射；
- 单选每个 choice 使用 opaque `Action.Submit` id；多选使用 `Input.ChoiceSet(isMultiSelect=true)`，点击后按 gateway 原 choices 顺序回传 canonical JSON 数组；
- **Other** 只调用 `mark_awaiting_text(clarify_id)`，保留同一 pending clarify 供下一条文本解析；
- action 仍经过 message/channel/operator/binding/input、幂等 claim 与 replay 门控；已失效或已解决的 clarify 显示 expired，不创建新模型 turn；
- profile/capability/render 或明确未创建 card 的 POST 拒绝走文本 fallback；timeout、断连、5xx、409/429 等不确定结果仅用同一 `client_msg_no` 重试一次，仍失败则返回失败，不发送第二个 prompt；
- `OCTO_ON_BEHALF_OF` 下 Type-17 不受支持，clarify 保持 Hermes 文本 fallback；无单独配置开关。

#### 插件内集成方案

不改 Hermes core：

1. `register(ctx)` 注册 `pre_llm_call`, `post_llm_call`, `pre_tool_call`, `post_tool_call` observer hooks；
2. hook 通过 `gateway.session_context.get_session_env()` 获取可信 platform/chat/session；
3. 仅 `HERMES_SESSION_PLATFORM == "octo"` 时处理；
4. 使用当前 adapter 的 gateway loop 线程安全调度异步卡片更新；
5. 状态键至少包含 `session_key + chat_id + turn_id`，并发 turn 不得互相编辑；
6. disconnect 必须取消/完成进度卡与 event poller；
7. hook 永远 fail-soft，任何卡片失败不影响工具或 agent turn。

### 3.6 卡片安全与适配

已实现并由 `tests/test_cards_render.py`、`tests/test_cards_security.py` 覆盖：

- Adaptive Card 版本固定 1.5；
- server manifest elements/inputs/actions/limits 白名单；
- max nodes / depth / payload bytes / text bytes；
- 不允许模型直接传 raw arbitrary Adaptive Card 作为默认工具参数；
- display tool 使用受控 `section/facts/text/image/actions` block；
- interactive tool 使用受控 input/button schema；
- `Action.OpenUrl` 只允许 http/https，拒绝 userinfo、危险 scheme；
- `Action.Submit.data` 注入 server/session 绑定字段，模型不得覆盖内部字段；
- `plain` 始终存在，老客户端/禁用服务端可降级；
- 卡片内任何文本都视为群可见 sink，执行 secret/url/error 清洗；
- 入站 `hidden_reasoning` 永不显示、永不注入模型上下文。
- 任意来源的入站 Type-17 只展示服务端安全 `plain`，不解析不可信 card JSON；只有绑定到本地活会话并通过 message/session/channel/operator/input 校验的 `card_action` 才能提交；

## 4. 实施顺序（RED → GREEN）

### Phase A — 协议原语

1. RED：Type-17 send/edit/profile/events/ack API tests；
2. GREEN：`api.py` + `types.py`；
3. 验收：payload byte-for-byte 与 OpenClaw/server 合同一致。

### Phase B — 安全 renderer 与工具

1. RED：profile auto-upgrade、manifest gate、limit、secret、URL、plain fallback；
2. GREEN：`cards.py`（纯函数）与独立 tool schemas；
3. 验收：无任意 raw-card 注入，unsupported 自动降级。

### Phase C — 进度卡生命周期

1. RED：并发工具、乱序完成、失败、disconnect、capability off、hook exception；
2. GREEN：plugin hooks + adapter-owned progress state；
3. 验收：单消息卡片过程更新、final frame、最终答案独立。

### Phase D — 交互事件

1. RED：cursor、poll pacing/backoff、event parse、ack ordering、duplicate/replay、session mismatch；
2. GREEN：poller + card session + dispatch bridge；
3. 验收：点击在同一 Octo session 形成新用户输入；无跨会话注入。

### Phase E — 其余工具面

1. `send-rich-text`；
2. media management actions；
3. unknown-type telemetry/fallback；
4. thread/markdown 实服边界验证。

## 5. 自动化与交付门槛

每一阶段至少执行目标测试；最终必须全部通过：

```bash
uv run pytest -q
uv run ruff check .
uv lock --check
git diff --check
git diff --cached --check
```

还需：

- [x] 当前 Hermes editable 全量；
- [x] `hermes-agent==0.14.*` 最低兼容隔离全量；
- [x] wheel/sdist 构建、安装、package-data、plugin discovery smoke；
- [x] added-line secret/local-path scan；
- [x] fresh read-only security/lifecycle/protocol review：无残留 Blocker / High / Medium；
- [ ] 经逐项授权的实服 feature manifest、display card、progress card、interactive action、media、thread 权限验证。

最新本地证据（2026-08-07）：

- 当前项目环境、实际 Hermes `0.20.0` 源码环境与 `hermes-agent==0.14.*` 无缓存隔离环境均为 `743 passed, 1 skipped`；
- `ruff check .` 与 `uv lock --check` 通过；
- 2026-08-06 的 wheel/sdist 无缓存安装、package-data smoke 与对应构建哈希仍是上一构建证据；本次 clarify 修改后尚未重新构建发布产物；
- fresh clarify lifecycle review 发现的版本签名、多选权威源、无效提交占用、pending 重验问题均已补回归并修复；实服 Type-17 clarify 仍待单独授权验收。

实服动作必须使用临时对象并清理。不得自动发送群消息、上传媒体、创建/删除 thread、替换活跃插件、重启 gateway、commit、push 或开 PR；这些分别等待明确授权。

## 6. 当前待完成清单

- [x] Type-17 API 原语
- [x] capability manifest cache + fallback
- [x] 安全 display-card renderer
- [x] 安全 interactive-card authoring
- [x] 独立 display/interactive card tools
- [x] progress/reasoning summary card 生命周期
- [x] event poller + durable cursor + ack
- [x] interactive card session/dispatch/dedup
- [x] RichText 当前会话工具
- [x] media 当前会话工具
- [x] unknown-type telemetry/fallback
- [x] `on_behalf_of` 配置、常规消息透传与 Type-17 安全降级
- [x] backend-agnostic presigned `uploadUrl` / `downloadUrl` / signed headers 重放
- [x] GROUP/THREAD 条件写能力核验（结论：BLOCKED-UPSTREAM）
- [ ] thread join/leave 非 creator 实服边界
- [x] 独立只读审查与最终本地回归
- [ ] 经逐项授权的实服 manifest/card/action/media/thread 验收

## 7. 实服验收清单（未执行）

以下每项都是独立的外部副作用，必须在执行前取得针对目标、范围和值的明确授权。只使用临时对象；记录测试对象 ID 以便清理；任何身份、权限或能力不一致立即停止，不自动扩大范围。

1. `GET /v1/bot/card/profile`：记录 `available/enabled/profiles/card_version/elements/inputs/actions/limits/templates`，确认 404 与显式 disabled 的区别。
2. Display card：向获授权的临时会话发送最小卡片，核对 Type-17、`message_id`、纯文本 fallback 和客户端渲染。
3. Progress card：执行一次首发、transient edit、final edit，核对同一 `message_id`、严格递增 `card_seq`、终态 `transient=false`，并确认无 hidden reasoning 泄漏。
4. Interactive action：发送带 `Action.Submit` 的临时卡片；分别验证合法点击、错误 operator/session/channel/message 拒绝、cursor 落盘、ack、重复事件不二次执行。
5. RichText：发送含文本与受控图片块的最小消息，核对块顺序、plain fallback 与尺寸字段。
6. Media：逐项验证 image/file/voice/video；只上传获授权的临时文件，核对 MIME、size、width/height/duration、caption/reply，并清理可删除的临时对象。
7. Unknown Type 99：仅在服务端允许受控注入时验证 numeric-safe fallback 和有界 telemetry；不得把原始 payload 写入日志。
8. Thread 权限：由非 creator 对临时 thread 执行 join/leave，确认真实权限边界；随后由获授权主体清理临时 thread。
9. 收尾：确认无残留临时消息、媒体或 thread；保存去敏后的协议证据；不得保留 token、signed URL、用户内容或本机绝对路径。

当前状态：上述实服步骤均未执行。
