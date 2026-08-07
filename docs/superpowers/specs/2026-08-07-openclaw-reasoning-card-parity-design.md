# OpenClaw 推理卡完整对齐设计

日期：2026-08-07

## 背景与目标

Hermes Octo 当前进度卡曾只显示工具名与 `complete`。目标改为以当前 `/tmp/openclaw-channel-octo-audit` 中 OpenClaw Octo 插件为唯一可观察行为基准，复现相同的 reasoning/progress 状态、文案、步骤摘要、耗时、折叠与 Registry 交付行为。

“对齐”指对 Hermes 与 OpenClaw 都能表达的非 continuation、非 token-streaming 输入子集，同一组模型调用、工具调用、结果、终态和卡片能力输入产生等价的 ViewModel、plain 文本和 Adaptive Card 结构。Hermes hook 字段名或内部类名不构成外部差异；下列两项宿主能力差异必须显式保留，不得把它们伪装成已实现：Hermes 没有 `sessions_yield` continuation 生命周期，也没有 token 级 reasoning event。

## 范围

### 包含

- `model_call`/provider API attempt 映射为 `__thinking__` 步骤。
- `reasoningVisibility` 等价门控：Octo `display.show_reasoning` 或平台级覆盖开启时，使用 provider 明确提供的 reasoning summary；关闭时使用普通 progress card，不显示 reasoning phase 文本。
- 工具参数摘要、结构化结果摘要、错误、每步耗时、总耗时。
- 连续同名成功工具合并、最近 12 步窗口和早期步骤计数。
- OpenClaw 的 phase、状态文案、图标、计数、折叠/展开和终态默认折叠。
- `ai.reasoning-process` Registry 模板发现、wire data、递增 `card_seq` 和 Model B fallback。
- 纯模型回复和仅发送 display/interactive card 的 turn 不产生额外进度卡。
- 并发 `tool_call_id` 精确配对、turn 隔离、失败 fail-soft、断连清理。

### 不包含

- OpenClaw 的 `sessions_spawn`/`sessions_yield` continuation 状态；Hermes 当前生命周期没有等价的受信 continuation 事件。本次不得伪造 paused/resuming。该差异不属于上述共同输入子集，验收必须明确记录。
- 修改 Hermes core hook 协议。
- card_action 竞争消费者问题；该问题独立处理。

## 已知约束

- Hermes `pre_api_request`/`post_api_request` 提供 provider attempt ID、耗时和已清洗的 `assistant_message`。
- Hermes `post_tool_call` 提供 `result`、`duration_ms`、`status` 和错误字段。
- Hermes 没有 OpenClaw 流式 reasoning event；因此同一 provider call 的 reasoning summary 在 `post_api_request` 一次性落入对应 thinking step，不能伪造 token 级流式刷新。最终 phase 内容与 OpenClaw 等价，帧刷新频率是明确的宿主差异。
- Type-17 OBO 不受服务端支持；与 OpenClaw 一致，OBO 不发送 progress/reasoning card。

## 关键决策

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 行为基准 | 当前 OpenClaw Octo `card-progress.ts`、`card-render.ts`、`reasoning-process.ts` | 用户要求完全一致 |
| reasoning 展示门控 | 复用 Hermes Octo `show_reasoning`，语义等价于 OpenClaw `on/stream` | Hermes 已有平台显示配置，无需第二套配置 |
| reasoning 数据源 | 只读取 provider 明确的 summary 字段；原始 `reasoning` 字段不参与 | 与 OpenClaw user-visible reasoning lane 的可观察语义一致 |
| reasoning 关闭 | 使用 OpenClaw fallback progress card，而非带 `Thinking through…` 的 reasoning card | 这是当前 Hermes 与 OpenClaw 的关键差异 |
| Registry 投递模式 | 只由模板模式和 manifest 兼容性选择；默认 experimental，兼容即 Model A | 对齐 OpenClaw `resolveEntryDeliveryMode`；与 reasoning 是否可见正交 |
| Model B renderer | reasoning 可见且有公开 thought 时使用 reasoning renderer，否则使用 progress renderer | 对齐 OpenClaw `usesReasoningProcessContract` |
| card authoring 工具 | `octo_send_display_card`、`octo_send_interactive_card` 不计入进度 | 产物本身就是卡片，避免旁路噪音 |

## 状态模型

每个 `(session_id, turn_id)` 持有一个独立 entry：

- `phase`: `thinking | tool | answering | completed | stopped | error | expired`
- `started_at`、`message_id`、`card_seq`、delivery mode、capabilities
- 有序 steps：
  - thinking：provider attempt ID、status、started/duration、公开 thought summary
  - tool：tool call ID、原始工具名、参数摘要、结果摘要、status、error、duration

转换：

1. `pre_llm_call` 建立 entry，不立即发卡。
2. `pre_api_request` 新增 running thinking；纯 thinking 不触发首卡。
3. `post_api_request` 精确结束 thinking，记录耗时和公开 summary；仅在已有卡或已有真实工具时刷新。
4. `pre_tool_call` 结束仍在运行的 thinking，新增 running tool，并懒发首卡。
5. `post_tool_call` 按 tool call ID 更新结果、耗时和错误。
6. `post_api_request` 观察到非工具 final content 时进入 `answering`。
7. `on_session_end` 生成唯一非 transient 终态，冲突优先级为 `interrupted=True` → `stopped`，否则 `failed=True` → `error`，否则 `completed=True` → `completed`；`completed=False` 且前两者均为 false 时按 `error`，因为 Hermes 只证明 turn 未正常完成。
8. 网络/渲染失败按下述可重试性处理；所有失败均不影响正常回答。

## 渲染

### Reasoning 模式

仅当配置允许且至少一个 thinking step 有公开 thought 时启用：

- ViewModel 与 OpenClaw `ReasoningProcessData` 字段和值一致。
- 最多 6 phases、12 actions。
- 活跃态展开；completed/stopped 折叠；error 展开。
- Toggle 可用时使用 `trace_panel`、`collapsed_panel` 和 `Show / hide reasoning`；不可用时展开为 display-only。
- Registry 兼容时无条件选择 Model A；首个真实 action 到达后才有非空 wire data 并发送首帧。reasoning 关闭或没有公开 thought 也不改变 delivery mode。

### Progress fallback

reasoning 关闭、没有公开 thought，或 Model B reasoning 布局不兼容时：

- Header：`🤖 Thinking…`、`🤖 Working…`、`🤖 Answering`、`✅ Done · N steps · duration`、`⚠️ Stopped`、`⚠️ Interrupted...`。
- 行：运行、成功、失败图标；参数摘要；每步耗时；错误摘要。
- 连续同名且成功的步骤合并为 `× N · total duration — latest: summary`。
- 最多显示最近 12 步，较早步骤显示 `… N earlier steps hidden`。
- 支持能力允许时，终态默认折叠并提供 `Hide details`/`Show details`。
- `plain` 与可见行语义一致。

## 错误与边界情况

- 带 tool call ID 的迟到/重复 post event 未命中时直接忽略，不按工具名误配并发调用。
- 只有旧 hook 缺 tool call ID 时才按 OpenClaw 规则配对最后一个同名 running 步骤（LIFO）。
- 非数字、负数或非有限耗时不显示。
- 未知工具保留经过既有 label 归一化后的名称；参数/结果没有受支持结构时显示 OpenClaw fallback，不展开原始输出。
- 结构超过服务端能力时降级为 TextBlock progress card，但不得退化为旧的 `name: complete` 清单。
- Registry 模板歧义、缺 view、错误 profile、未知 submit action时不用 Registry。
- Registry 首帧只有在 transport status 明确为 `400/404/422`，内层 semantic status（如存在）也属于该集合，且错误码（如存在）属于 `card_invalid`/`err.server.bot_api.card_invalid` 时，才在同一 flush 中恰好回退一次 Model B。408、冲突、429、5xx、网络错误或提交结果不确定时不得再次 send，避免双卡。
- Registry edit 对网络/无 transport status、429、transport 5xx、semantic 5xx 或 `err.shared.internal` 使用同一 body 和同一 `card_seq` 重试，延迟依次为 100ms、250ms，最多三次请求。确定性 4xx 不重试。
- Registry transient 失败耗尽重试后保留 entry，后续事件用新的递增 `card_seq` 继续；确定性 4xx 才终止该 entry。

## 验收标准

1. 移植 OpenClaw reasoning-process 的 ViewModel、状态、sanitization、wire-data 和 Adaptive Card 行为测试；同等输入断言同等输出。
2. 移植 fallback progress 的 header、step line、连续分组、12 步窗口、终态折叠测试。
3. reasoning 关闭或没有公开 thought 时，Model B plain/card 不含 phase thought，仍包含工具耗时和结果摘要；若 manifest 选择 Model A，delivery mode 仍为 Registry，首个真实 action 到达后发送 wire data。
4. reasoning 开启且 provider 给出 summary 时，Model B 展示 phase thought，终态为 `Done · duration · phases · tool calls`。
5. 用户示例中的 `skill_view`、`tool_describe`、`octo_card_profile`、`terminal` 等不再只显示 `complete`；至少显示 OpenClaw 等价状态/耗时，存在可提取参数或结果时显示摘要，连续相同调用合并。
6. 纯文本 turn 和仅 card-authoring turn不发送进度卡。
7. Registry 和 Model B 的首次发送、transient edit、终态 edit 顺序正确，`card_seq` 单调递增；分别覆盖正常、interrupted→stopped、failed→error、incomplete→error，所有终态均非 transient。
8. Registry 首帧确定性拒绝只回退一次 Model B；网络/429/5xx 不二次 send。Registry edit 的两次延迟重试复用相同 body/`card_seq`，重试耗尽后下一事件仍可恢复。
9. 两个缺失 call ID 的同名 running 工具按 LIFO 配对；有 ID 的迟到事件不回退按名匹配。
10. `tests/test_card_progress.py`、相关 card/API tests、全量 pytest、ruff、lock check 全部通过。
11. xiao_ai bundled 插件更新到最终提交，gateway 重启后实服产生一张新推理卡；支持项观察到与 OpenClaw 等价的 phase/step/耗时/折叠展示，并记录 continuation 与 token 级 streaming 两项预期宿主差异。

## 未决事项

无。