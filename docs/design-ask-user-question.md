# 设计：AskUserQuestion 适配 Telegram 按钮

## 背景

Claude Code 的 `AskUserQuestion` 工具允许 Claude 向用户提问并提供选项。在 CLI 中选项以编号列表展示，用户输入数字回答。但在 Telegram bot 环境下，这个工具目前**无法工作** —— SDK 的 CLI 子进程尝试读 stdin（终端），headless 环境没有终端，结果返回空。

## 目标

将 `AskUserQuestion` 的选项以 Telegram inline keyboard 按钮展示，用户点击按钮即回答。整个过程对 Claude 透明 —— 它看到的是一个正常的工具调用和结果。

## 技术方案：PreToolUse Hook

### 机制

`claude-agent-sdk` 提供 `hooks` 参数，支持 `PreToolUse` 事件。Hook 在工具执行**之前**触发，可以：
- `permissionDecision: "deny"` —— 阻止工具执行，Claude 看到 `permissionDecisionReason` 作为反馈
- `permissionDecision: "allow"` + `updatedInput` —— 修改工具输入后放行
- `additionalContext` —— 在工具结果旁注入系统提醒

Hook 函数是 async 的，SDK 会 await 它，因此可以等待用户回复。

### 选择的策略

**策略：async hook + deny + additionalContext**

```
Claude 调用 AskUserQuestion(questions=[{question, options, header, multiSelect}])
    ↓
PreToolUse hook 触发（matcher: "AskUserQuestion"）
    ↓
解析 tool_input，提取 question / options / header / multiSelect
    ↓
构造 Telegram InlineKeyboardMarkup：
  - 每个 option 一行按钮（label 为按钮文字，callback_data 含 option index）
  - 如果 multiSelect=True，加 "确认选择" 按钮
  - header 作为按钮区域标题
    ↓
发送 Telegram 消息："Claude 想问你：{question}\n[按钮列表]"
    ↓
hook `await future`（无单独超时，由 CLAUDE_TIMEOUT_SECONDS 兜底）
    ↓
用户点击按钮 → callback handler 设置 Future 的值
    ↓
hook 获得用户选择 → 返回：
{
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "User answered via Telegram: {answer text}",
        "additionalContext": "The user was asked: '{question}' and selected: {answer}"
    }
}
    ↓
Claude 看到拒绝原因 + 额外上下文，得知用户答案，继续推理
```

### 为什么用 deny 而不是 allow

- `allow` 放行后 SDK 仍然执行 `AskUserQuestion`（读 stdin），headless 下会失败
- `deny` + `permissionDecisionReason` 把答案直接注入 Claude 上下文
- Claude 看到 "User answered via Telegram: 选项B" 后能正确理解并继续
- 如果 Claude 忽略了答案再次提问（edge case），hook 再次触发，用户再答一次，不影响正确性

---

## 详细需求

### R1：注册 PreToolUse Hook

在 `src/claude/sdk_integration.py` 的 `execute_command()` 中，构建 `ClaudeAgentOptions` 时注册 hook：

```python
options.hooks = {
    "PreToolUse": [
        HookMatcher(
            matcher="AskUserQuestion",
            hooks=[async_hook_function],
        )
    ]
}
```

hook 函数需要：
- 接收 `(hook_input: PreToolUseHookInput, stdin: str | None, context: HookContext)`
- 返回 `SyncHookJSONOutput` 或 `AsyncHookJSONOutput`

### R2：解析 AskUserQuestion 参数

从 `hook_input["tool_input"]` 提取：

```python
questions = tool_input["questions"]  # List[dict]
# 每个 dict:
#   question: str          # 问题文本
#   header: str            # 短标签（≤12 字符）
#   options: List[dict]    # 选项列表
#     label: str           # 显示文字
#     description: str     # 解释
#   multiSelect: bool      # 是否多选（默认 false）
```

### R3：构造 Telegram 消息和按钮

**单选（multiSelect=False）**：
```
🤔 <b>Claude 想问你：</b>{question}

[选项1 label]
[选项2 label]
[选项3 label]
[选项4 label]
```

- 每个选项一行按钮
- callback_data: `auq:{tool_use_id}:{option_index}`
- 按钮数量上限 4 个（Telegram 单行限制，每行一个按钮）

**多选（multiSelect=True）**：
```
🤔 <b>Claude 想问你：</b>{question}
（可多选，选完点"确认"）

[☐ 选项1]  [☐ 选项2]  [☐ 选项3]  [☐ 选项4]
[确认选择]
```

- 前 4 个选项一行（Telegram 限制一行最多 8 个按钮，但 label 长的话一行 4 个合适）
- 点击切换选中状态（☐ → ☑），通过 edit_message_reply_markup 更新按钮
- "确认选择" 按钮发送最终结果
- callback_data: `auq:{tool_use_id_short}:{option_index}`（toggle）或 `auq:{tool_use_id_short}:confirm`
- `tool_use_id_short` = `tool_use_id` 截取前 16 字符（Telegram callback_data 上限 64 字节）

**选项超过 4 个时**：
- 最多展示前 4 个选项（`AskUserQuestion` 规范要求 2-4 个选项，不需要处理更多）

### R4：Callback Handler

在 `src/bot/orchestrator.py` 中注册 `CallbackQueryHandler`：

```python
app.add_handler(
    CallbackQueryHandler(
        self._inject_deps(self._handle_auq_callback),
        pattern=r"^auq:",  # matches "auq:{tool_use_id_short}:{idx}"
    )
)
```

`_handle_auq_callback` 逻辑：
1. 解析 callback_data：`auq:{tool_use_id}:{option_idx}` 或 `auq:{tool_use_id}:confirm`
2. 查找对应的 `_pending_auq[tool_use_id]`
3. 单选：直接 `future.set_result(answer)`
4. 多选：toggle 选项状态，更新按钮文字（☐ ↔ ☑），确认时 `future.set_result(answer)`
5. 编辑消息，移除按钮，显示 "你选择了：{option label}"
6. answer the callback query

### R5：Hook 与 Callback 的通信

在 `MessageOrchestrator` 上维护一个 dict：

```python
self._pending_auq: Dict[str, asyncio.Future] = {}
# key = tool_use_id（来自 hook_input）
# value = asyncio.Future，resolve 时的值 = {"selected": ["option_label", ...]}
```

Hook 流程：
1. hook 触发 → 取 `tool_use_id` → 创建 Future → 存入 `_pending_auq[tool_use_id]`
2. 发送 Telegram 消息（带按钮）
3. `await future`（等用户点击，无单独超时，由 CLAUDE_TIMEOUT_SECONDS 兜底）
4. 获得结果 → 清理 `_pending_auq` → 返回 hook output

Callback 流程：
1. 用户点击按钮 → `_handle_auq_callback`
2. 查找 `_pending_auq[tool_use_id]`
3. `future.set_result(answer)`

### R6：超时

不单独设超时。Claude 的执行超时由 `CLAUDE_TIMEOUT_SECONDS`（默认 300 秒）兜底，用户可在 `.env` 里调大。如果执行超时，SDK 终止整个执行，hook 随之清理，Telegram 消息保留为"已发送但未回答"状态。

### R7：并发安全

- `_pending_auq` 用 `tool_use_id` 作为 key（每个 tool_call 唯一）
- callback_data 含 `tool_use_id` 截取前 16 字符，callback handler 验证匹配
- 用户点击不属于自己的按钮：callback handler 验证 `query.from_user.id` 是否匹配触发提问的用户

### R8：把 AskUserQuestion 加入 allowed_tools

`src/config/settings.py` 的 `claude_allowed_tools` 默认列表中没有 `AskUserQuestion`。需要添加，否则 Claude 不会使用这个工具。

### R9：格式化答案返回

答案格式应让 Claude 无歧义地理解：

**单选**：
```
permissionDecisionReason: "User selected: '是的，继续' (via Telegram)"
additionalContext: "The user was asked '{question}' and chose: '是的，继续'"
```

**多选**：
```
permissionDecisionReason: "User selected: 'React', 'TypeScript' (via Telegram)"
additionalContext: "The user was asked '{question}' and chose: React, TypeScript"
```

**超时**：
```
permissionDecisionReason: "User did not respond within 120 seconds"
```

### R10：消息生命周期

| 状态 | 消息内容 |
|---|---|
| 发送时 | 问题 + 按钮 |
| 用户回答后 | 编辑为 "✅ 你选择了：{answer}"，移除按钮 |
| 出错 | 编辑为 "❌ 处理出错"，移除按钮 |

---

## 涉及的文件

| 文件 | 改动 |
|---|---|
| `src/claude/sdk_integration.py` | 在 `execute_command()` 构建 options 时注册 `PreToolUse` hook。需要把 orchestrator 的 `_handle_auq_callback` 引用传入，或把 hook 闭包建在这里 |
| `src/bot/orchestrator.py` | 添加 `_pending_auq` dict、`_handle_auq_callback`、`_send_auq_message`、auq callback handler 注册。hook 函数建在这里并传给 sdk_manager |
| `src/config/settings.py` | `claude_allowed_tools` 默认列表加入 `AskUserQuestion` |
| `src/bot/utils/html_format.py` | 可能需要 escape HTML（question/option 文本） |

## 依赖关系

hook 函数需要访问 Telegram Bot 实例（发消息、编辑消息）。当前 `ClaudeSDKManager` 没有 Telegram Bot 引用。需要：
- hook 闭包在 `MessageOrchestrator` 中构建（它有 `bot_data` 引用）
- 通过 `sdk_manager` 传入 hook 列表，或在 orchestrator 层面包装

## 不做

- 不处理"Other"选项（`AskUserQuestion` 的 Other 是用户自由输入，Telegram 按钮不支持，先跳过）
- 不处理 question 里嵌入的 preview 内容（纯文本展示）
- 不做持久化（问答回合不存 DB）
