# Clarify 结果卡显示优化设计

日期：2026-08-10

## 背景与目标

Octo 插件目前在 clarify 动作完成后复用通用交互卡状态渲染器：冻结输入控件、原样回显输入值，并追加英文状态、动作标签和操作者 UID。多选值是内部 `clarify_choice_*` 标识，因此最终卡片暴露实现细节且中英文混杂。

目标是仅优化 clarify 卡的提交后显示：保留原问题和用户可读的选择，隐藏内部标识与 UID，并使用简洁中文状态。动作解析、权限绑定、幂等和 Hermes clarify resolver 语义保持不变。

## 范围

### 包含

- 为 `CardSession.clarify` 提供专用状态渲染。
- 单选和多选完成后显示原问题及 canonical 选项文本。
- clarify 各终态和处理中状态使用中文文案。
- 移除完成态中的输入控件和动作按钮。

### 不包含

- 改变通用 Type-17 交互卡的状态样式。
- 改变 clarify 的选择解析、resolver、权限、重放或 ACK 行为。
- 部署活跃插件或重启 Gateway。

## 关键决策

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 信息密度 | 保留标题、原问题、已选项和状态 | 方便事后回看，同时避免当前内部值噪声 |
| 选项来源 | 只从服务端 `ClarifySession.action_choices` 映射 | 客户端提交值不可信，不能直接展示为答案 |
| 影响边界 | clarify 专用分支；普通交互卡继续沿用现有 renderer | 避免改变已有通用卡外部契约 |
| 身份显示 | 不显示 operator UID | requester 已由动作绑定校验，结果卡无需暴露内部身份标识 |

## 设计

Clarify 状态卡固定保留“需要确认”标题和 `ClarifySession.question`。

合法选择的展示值由服务端会话映射得出：

- 单选：以动作 ID 查找 canonical choice。
- 多选：解析提交的本地 choice ID 集合，再按 `action_choices` 的原始顺序生成 canonical choice 列表。
- “其他”、过期以及无法得到合法选择的状态不显示“已选择”区块。

完成态结构：

```text
需要确认
<原问题>

已选择
<选项一>、<选项二>

已提交
```

状态文案：

- `processing`：`正在提交…`
- `completed`：`已提交`
- `awaiting_text`：`请直接发送文字回复`
- `expired`：`该确认已失效或已处理`
- `failed`：`提交失败，请重试`

卡片 body 仅包含只读 `TextBlock`，不保留 `Input.*` 或 `Action.Submit`。plain fallback 从同一组用户可见文本生成。内部 choice ID、clarify ID、session key、binding、动作 ID 和 operator UID 均不进入 card 或 plain。

普通非 clarify `CardSession` 继续使用现有冻结控件和通用英文状态逻辑。

## 错误与边界情况

- 多选提交顺序不影响显示顺序；结果始终遵循权威选项顺序。
- 未知或重复 choice ID 不会到达成功状态；即使状态渲染被独立调用，也不回显未知原始值。
- “其他”进入文字回答时不显示此前勾选值，避免暗示这些选项已提交。
- 状态卡编辑失败不回滚已经完成的 Hermes clarify 状态。

## 验收标准

1. 多选完成态显示原问题、canonical 选项文本和“已提交”，且顺序遵循权威 choices。
2. 单选完成态显示对应 canonical 选项文本。
3. clarify 状态卡和 plain 均不包含 `clarify_choice_*`、operator UID 或英文 `Completed ... for ...`。
4. awaiting-text、expired、failed 和 processing 使用约定中文文案。
5. clarify 状态卡不包含输入控件或动作按钮。
6. 普通交互卡现有状态渲染行为不变。

## 未决事项

无。
