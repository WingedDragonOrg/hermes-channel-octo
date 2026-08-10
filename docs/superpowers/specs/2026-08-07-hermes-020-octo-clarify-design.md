# Hermes 0.20 Octo Clarify 适配设计

日期：2026-08-07

## 背景与目标

`BasePlatformAdapter.send_clarify()` 在 Hermes 0.14.0 已存在，0.20.0 的相关增量是 `multi_select`、活跃选项提示期间的普通文本解析，以及更严格的原生交互语义。因此 Octo 不能以方法存在性判断能力，必须读取运行时 `hermes-agent` 版本。

目标是在 Hermes `>=0.20,<0.21` 时，把带选项的 clarify 映射为 Octo Type-17 原生交互卡；旧版本、未来未验证版本和任何不安全或不受支持的场景继续使用 Hermes 基类文本 fallback。

## 范围

### 包含

- 在 `OctoAdapter` 初始化时读取并解析一次运行时 `hermes-agent` 版本。
- 覆盖 `send_clarify()`，为 Hermes 0.20.x 提供单选和多选 Type-17 卡片。
- 复用现有 Type-17 renderer、能力协商、`CardSessionRegistry`、事件 poller、ownership/replay 门控。
- 将原生卡动作直接回传给 Hermes clarify primitive。
- 在能力、身份、版本或发送条件不满足时调用 `super().send_clarify()`。

### 不包含

- Hermes 0.21+ 支持；需要单独兼容验证后开放。
- Hermes 0.14–0.19 的交互卡适配。
- slash approval / `send_slash_confirm()`。
- open-ended clarify 的卡片化；仍由基类文本捕获处理。
- 改变 Hermes clarify primitive、超时或普通文本解析规则。
- 自动部署、重启 Gateway 或实服发卡。

## 已知约束

- 项目依赖边界为 `hermes-agent>=0.14,<0.21`。
- 0.14.0 与 0.20.0 的 `send_clarify(chat_id, question, choices, clarify_id, session_key, metadata=None)` 签名基本一致，不能作为版本探针。
- 0.20.0 在 pending clarify entry 中保存 `multi_select`；adapter 方法签名本身没有该参数。
- 原生动作必须调用 `tools.clarify_gateway.resolve_gateway_clarify()`；“其他”必须调用 `mark_awaiting_text()`。
- 原生动作不能转换为普通 `MessageEvent`，否则会创建错误的新用户 turn。
- Type-17 action payload 来自客户端，不能信任原始 choice 文本、clarify id、session key 或目标会话。
- `on_behalf_of` 当前不支持 Type-17 persona 交付。

## 关键决策

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 版本探测 | `importlib.metadata.version("hermes-agent")`，Adapter 初始化时读取一次，并按 PEP 440 比较 | 反映实际运行环境；避免方法存在性误判；避免每次 clarify 重复读取 |
| 原生启用边界 | 仅 `>=0.20,<0.21` | 与已验证 Hermes 语义和当前依赖上限一致 |
| 未知版本 | 基类文本 fallback | 保守、可用，不把未知语义误判为原生能力 |
| open-ended clarify | 基类文本 fallback | 没有可安全绑定的选项，Hermes 已有权威文本捕获 |
| 单选 UI | 安全数量内使用独立 choice 按钮和“其他” | 一次点击即可完成；动作只携带本地 action id |
| 多选 UI | `Input.ChoiceSet(isMultiSelect=true)`、确认按钮和“其他” | 与 Hermes 0.20 `multi_select` 语义一致 |
| 超出安全卡片限制 | 完整文本 fallback，不截断、不重排 | 不能改变模型提供的权威选项集合 |
| 动作解析 | 以服务端 `CardSession` 保存的权威映射解析 | 客户端 payload 只承担不透明 action/input id，不承担授权或答案内容 |
| Other | `mark_awaiting_text(clarify_id)` 返回 true 后才进入等待文本 | 已过期或已解析的 clarify 不能伪报成功 |
| 失败降级 | 只在 POST 前失败或服务端明确拒绝且确认未创建消息时调用基类 fallback | 超时/断连等投递结果不明时避免双 prompt |
| POST 后失败 | 使用同一 `client_msg_no` 做有界幂等确认；仍无法确认则返回失败，不发送第二个 prompt | 服务端可能已接受原生卡但响应丢失 |

## 设计

### 运行时版本能力

`OctoAdapter.__init__` 初始化一个不可变的 clarify 兼容状态：

- 成功解析且 `0.20 <= version < 0.21`：候选启用原生 clarify。
- `0.14 <= version < 0.20`：文本 fallback。
- `version >= 0.21`：文本 fallback。
- distribution 不存在、版本为空或无法解析：文本 fallback。

版本状态只表示 Hermes 语义兼容；每次发送仍必须通过 Octo profile、Type-17 capability、可信 route、requester 和 `on_behalf_of` 门控。

### send_clarify 流程

1. 若没有 choices、版本不兼容、`on_behalf_of` 启用，直接调用 `super().send_clarify()`。
2. 从当前受信会话上下文解析 Octo route，并核对 `chat_id` 与 `session_key`。无法确定 requester 或共享会话归属时 fallback。
3. 从 Hermes 0.20 pending clarify entry 读取权威状态，并核对 `clarify_id`、`session_key`、choices；任何不一致 fallback。
4. 协商 card profile/capability；不可用或禁用时 fallback。
5. 构建有界 Type-17 card，并预备尚未绑定 `message_id` 的 `CardSession` 权威动作状态。
6. 以从 `clarify_id` 派生的稳定 `client_msg_no` 发送卡片；重试必须复用同一值。
7. 取得真实 `message_id` 后完成 session 的 message/binding 绑定，并返回 Hermes `SendResult(success=True, message_id=...)`。

fallback 必须调用基类实现，而不是自行复制编号文本，以继承 0.14 和 0.20 各自的文本语义。

### 单选卡

- 卡片展示 question。
- 每个选项分配本地生成的 action id；action data 只含既有可信 binding 字段和 action id。
- `CardSession` 保存 `action id -> canonical choice`。
- 添加“其他” action；该 action 不携带自由文本。
- 若选项数量或任何文本超过 renderer/card 安全上限，整体走文本 fallback；不得截断、删除或重排。

### 多选卡

- 从 pending entry 的 `multi_select=True` 判定，不从客户端或 metadata 判定。
- 使用 `Input.ChoiceSet`，开启 `isMultiSelect`。choice value 是匹配 `[A-Za-z0-9_-]{1,64}` 的本地不透明 id，禁止逗号和空白。
- 按 Adaptive Cards 1.x 合同，真实 `Action.Submit` 事件中的 `inputs[input_id]` 是逗号分隔字符串。解析器只接受字符串，按逗号切分并去除 token 两侧 ASCII 空白；空 token、重复 id、未知 id 或不符合字符集的 token 使整个提交失败。
- “确认” action 只提交 input value；服务端按 session 中的权威映射验证完整集合，不做部分接受。
- 至少选择一项才解析。canonical choices 按原始权威 choices 顺序输出，不依赖客户端选择/序列化顺序。
- 将 canonical choices 序列化为 JSON 数组字符串，调用 `resolve_gateway_clarify(clarify_id, response)`。
- “其他”不提交当前选择；仅当 `mark_awaiting_text(clarify_id)` 返回 true 时进入等待文本。

### CardSession 与动作生命周期

Clarify session 在现有 CardSession 结构中记录：

- kind=`clarify`
- `clarify_id`
- `session_key`
- requester/operator、channel、message、binding
- 单选 action 映射或多选 input 映射
- Other action id
- 过期时间与既有 replay/claim 状态

事件处理沿用现有 ownership 顺序，并为 `kind=clarify` 增加独立消费分支：解析事件 → 查找 session → operator/channel/message/binding/ownership/replay 校验 → 解析 action → 调 Hermes clarify primitive → 持久化终态/cursor → ACK。现有普通 card-action 分支仍注入 `MessageEvent`，不能被 clarify 分支整体替换。

- 单选：解析 canonical choice 后调用 `resolve_gateway_clarify()`。
- 多选确认：按逗号分隔线格式严格验证完整集合后调用 `resolve_gateway_clarify()`。
- Other：调用 `mark_awaiting_text()`。
- primitive 返回 true：完成 claim、持久化 cursor 并 ACK；卡片可 best-effort 编辑为已提交或等待文本。
- `resolve_gateway_clarify()` 或 `mark_awaiting_text()` 返回 false：视为 expired/already-resolved，将该 clarify 卡终结为不可操作状态；事件仍按“已归属且已消费”持久化并 ACK，避免永久 replay，但不得显示已提交或等待文本。
- 错误 binding/operator/channel、未知 action/input：rejected/ignored，不调用 primitive，不注入 agent turn。

primitive 已成功后的卡片编辑失败不能撤销 Hermes 状态，也不能释放事件 claim。

## 错误与边界情况

- 版本读取或解析异常：不抛出到 Gateway，使用文本 fallback。
- Hermes 0.20 pending entry 缺失或与参数不一致：文本 fallback。
- requester 无法唯一确定：文本 fallback，尤其是共享 group session。
- profile 404、explicit disabled、缺少 interaction capability：POST 前失败，文本 fallback。
- render/limit 校验失败：POST 前失败，文本 fallback。
- 服务端明确拒绝且响应证明未创建消息：文本 fallback。
- 一旦发起卡片 POST，超时、断连、5xx、成功响应缺少 `message_id` 等结果不明场景不得立即 fallback；仅可复用同一 `client_msg_no` 做有界幂等重试/确认。仍无权威 `message_id` 时返回失败，不发送第二个 prompt。
- 卡片发送已成功但本地 session 最终绑定失败：不重复发文本；返回失败并记录脱敏错误，避免双 prompt。
- 事件重复：沿用 CardSession replay 语义，只允许一次有效 resolve；primitive 的 false 返回也会终结并 ACK 该已归属事件。
- 普通文本：只有 Hermes 自己的 active clarify text intercept 负责处理；Octo clarify 分支不吞普通消息。
- 0.21+：即使方法和字段仍存在也不启用原生路径。

## 验收标准

1. 运行版本 0.14.x、0.19.x、0.21.x、缺失或不可解析时，带 choices 的 clarify 调用基类文本 fallback，不发送 Type-17。
2. 运行版本 0.20.x 且所有门控满足时，单选 clarify 发送一张 Type-17 卡；点击合法选项以 canonical choice 解析同一个 `clarify_id`。
3. “其他”仅在 `mark_awaiting_text()` 返回 true 时进入 awaiting-text；随后普通消息由 Hermes 0.20 原生 text intercept 解析，不创建额外 Octo card-action turn。返回 false 时卡片终结为 expired/already-resolved，不显示等待文本。
4. 多选卡只接受真实 Octo `Action.Submit` envelope 中逗号分隔的 `inputs[input_id]`；合法集合以权威 choices 顺序序列化为 JSON 数组字符串 resolve；空 token、空集合、重复、未知 id 或混合非法集合不部分解析。
5. 错误 operator/channel/message/binding、replay、过期 clarify 不触发成功 resolve；已归属但 primitive 返回 false 的事件终结并 ACK，不能永久重放。
6. open-ended、身份不明确、`on_behalf_of`、profile/capability 不支持、render 或 POST 前确定失败走基类 fallback。
7. 选项过多或超限时保留完整语义走文本 fallback，不截断。
8. 原生卡 POST 成功后本地绑定失败不发送第二个 prompt。
9. 服务端接受 POST 但响应丢失时，重试复用同一 `client_msg_no`；无法确认投递结果时返回失败且不发送文本 fallback。
10. 现有 display/interactive/progress/card_action、mention、send-final-only 行为无回归；普通 card-action 仍按原路径注入 `MessageEvent`。
11. 当前 Hermes 0.20 环境与最低 Hermes 0.14 兼容测试通过；全量 pytest、Ruff、`uv lock --check` 通过。

## 未决事项

无。
