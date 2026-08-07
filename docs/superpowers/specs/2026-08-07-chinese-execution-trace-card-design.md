# 中文执行轨迹卡设计

日期：2026-08-07

## 目标

把 Octo 当前英文、模板化的 reasoning/progress 卡改为中文技术助手的“执行轨迹”。卡片只回答三件事：正在做什么、做到哪一步、是否成功。它不展示或暗示 hidden Chain-of-Thought；只有 provider 明确提供且 `display.show_reasoning` 已开启的公开 reasoning summary 才可作为处理说明出现。

## 已确认决策

- progress/reasoning 默认使用插件本地 Type-17 Adaptive Card renderer；仍受 card profile `enabled`、`octo/v1`、元素、动作及 limits 门控。
- 服务端 `ai.reasoning-process` Registry 不再被 progress controller 自动优先选择。template-ref API、manifest 解析和显式 API 调用保留为兼容能力。
- 不硬编码机器人名称。卡片统一称“处理进度”，详情称“执行详情”。
- 不使用机器人、灯泡、勾叉等 emoji。状态通过语义色与细窄“执行轨”表达。
- 不使用 `01/02` 序号：工具调用可能并发，序号会伪造顺序语义。

## 视觉系统

概念色板：冷雾蓝 `#F3F7FA`、墨青 `#19313A`、海松绿 `#2E6B62`、琥珀 `#C4872F`、朱砂 `#B24A3A`、云灰 `#8A98A1`。Adaptive Card 不能保证客户端采用指定 hex，因此实现映射为 `Accent`、`Good`、`Warning`、`Attention` 和 `isSubtle`，不内嵌私有样式。

- 标题/正文：客户端系统无衬线；标题 `Bolder`，正文 `Default/Small`。
- 参数与结果：`Small` + `isSubtle`；客户端支持时使用 `fontType: Monospace`。
- 签名元素：左侧一列细轨。`●` 表示已完成，`◉` 表示当前进行，`○` 表示停止/失败；轨线用 `│`。符号不携带唯一状态含义，始终同时有中文状态和语义色。
- 活跃态默认展开；成功/停止终态默认收起；错误终态默认展开。支持 `Action.ToggleVisibility` 时显示“收起执行详情”/“展开执行详情”，否则始终展开。

目标结构：

```text
┌──────────────────────────────┐
│ 处理进度                 进行中 │
│ 已完成 2/3 个步骤 · 12 秒       │
├──────────────────────────────┤
│ ● 读取文件                     │
│ │ …/src/cards.py · 第 420–560 行│
│ ◉ 运行测试                     │
│   pytest · 正在运行             │
└──────────────────────────────┘
```

## 中文文案

- 状态：`进行中`、`正在整理答案`、`已完成`、`已停止`、`处理失败`。
- 进度：`正在处理…`、`正在等待子任务…`、`子任务已返回，正在收尾…`。
- 错误：`处理未完成`、`等待后台任务超时。`、`处理被中断，已完成的步骤仍然保留。`
- 计数：`N 个阶段`、`N 次工具调用`、`N 个步骤`、`已隐藏前 N 个步骤`。
- 结果：`N 项结果`、`N 个文件`、`N 字节`、`退出码 N`、`已完成`、`已接受`、`排队中`、`等待中`。
- 未知工具保留经过脱敏和长度限制的原始名称；MCP 与不安全名称显示为“扩展工具”或“工具”。

## 工具标签与安全参数摘要

已知 Hermes 工具使用中文标签：

- 文件：`read_file/read` → `读取文件`，`write_file/write` → `写入文件`，`patch/edit/apply_patch` → `修改文件`，`search_files/search/grep/glob` → `搜索文件`。
- 命令：`terminal/bash/exec/shell/process/exec_command` → `运行命令`。
- Web/UI：`web_search` → `搜索网页`，`web_extract/fetch` → `读取网页`，`browser_navigate` → `打开网页`，其余 browser 操作使用“读取网页/点击页面/填写表单/滚动页面/返回页面/发送按键/查看图片/查看控制台”；`browser_type` 不显示输入内容。
- 能力：`tool_search` → `查找工具`，`tool_describe` → `读取工具说明`，`skill_view` → `读取技能`，`skills_list` → `列出技能`，`tool_call` 识别内层已知工具并复用其标签和摘要。
- 上下文：`lcm_grep/lcm_describe/lcm_expand/lcm_inspect` → `检索上下文/查看上下文/展开上下文/检查上下文`；不展示原始上下文正文。

参数只从白名单字段生成：

- 文件读写/修改：缩短路径；读取额外显示合法的行范围。
- 搜索文件：脱敏且截断的 pattern/query 与缩短路径。
- 命令：只解析安全程序名和有限的已知子命令；不回显环境赋值、参数值、管道、重定向或原始完整命令。
- 网页导航：只显示归一化 origin，不显示路径、query、userinfo。
- 工具搜索/说明/技能：显示经过 URL 降级、secret 检测和长度限制的名称/查询。
- `tool_call`：只接受 mapping 形式的内层工具名和 arguments；仅当内层工具在上述白名单时递归生成摘要。
- 凭据字段、`browser_type` 内容、未知参数、原始工具输出、错误响应体一律不展示。

## 兼容与降级

- manifest 不允许 `octo/v1` 或显式 disabled：不发送卡片。
- 缺 `ColumnSet`/`Container`：降级为全中文 TextBlock 卡片。
- 缺 `ActionSet`/`Action.ToggleVisibility`：详情保持展开，不生成失效按钮。
- 超过本地或服务端限制：降级为全中文扁平卡；仍不得泄漏 raw tool output 或 hidden CoT。
- plain fallback 与卡片可见信息使用相同中文语义。

## 验收

1. 兼容 Registry manifest 时，progress controller 仍调用 `send_card_message`/`edit_card_message`，不调用 template-ref 发送/编辑。
2. 活跃、成功、停止、错误四类状态以及展开/收起按钮无英文 UI 文案、无 emoji。
3. 公开 reasoning summary 的门控不变；关闭时 plain/card 不出现 reasoning phase 文本。
4. `read_file`、`search_files`、`terminal`、`browser_navigate`、`tool_search`、`tool_describe`、`tool_call`、`skill_view`、`lcm_*` 有中文标签；只显示上述安全摘要。
5. 连续工具合并、12 步窗口、耗时、结果、错误、terminal collapse、card limits、并发/turn 生命周期行为保持。
6. 目标 tests、相关 card/API/mention/streaming tests、全量 pytest、Ruff 与 lock check 全部通过。
7. 同步到 xiao_ai bundled 插件并重启 Gateway 前再次取得明确授权；部署后由用户在 Octo 发送真实工具调用消息，确认终态和客户端视觉。