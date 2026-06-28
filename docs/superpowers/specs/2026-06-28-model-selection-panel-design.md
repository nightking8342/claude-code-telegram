# Model Selection Panel Design

**Date:** 2026-06-28
**Status:** Approved
**Scope:** Replace `/model` text-only command with an interactive inline-keyboard panel supporting role assignment, context window toggles, model list fetch, pagination, and search.

## Summary

将 `/model` 命令从纯文本交互升级为 Telegram Inline Keyboard 面板。无参数调用进入交互面板（模型列表 + 分页 + 搜索），有参数调用保留文本快捷方式。模型详情页支持以模型为中心勾选角色 + 开关 1M 上下文窗口。

同时废弃顶层 `model_override` 字段，将默认模型归入 provider profile 的 `default_model`，与 `opus_model`/`sonnet_model`/`haiku_model` 统一。

## UI Flow

### 入口：`/model` 无参数

顶部展示当前各角色的生效模型和窗口，下方是可浏览的模型列表：

```
📋 模型列表 — cpa-n2
━━━━━━━━━━━━━━━━━━━━━━━━━━
默认: deepseek-v4-pro [1M]
🐙 opus: deepseek-v4-pro [1M]
🟡 sonnet: deepseek-v4-flash [1M]
🟢 haiku: deepseek-v4-flash [200K]
━━━━━━━━━━━━━━━━━━━━━━━━━━
  [mimo-v2.5-pro]
  [deepseek-v4-pro]
  [deepseek-v4-flash]
  [deepseek-v3.1]
  [qwen3-235b-a22b]
  [kimi-k2.5]
  ...
━━━━━━━━━━━━━━━━━━━━━━━━━━
  [🔍 搜索]  [⬅] [1/3] [➡]
```

Rich Messages 模式 (`RICH_MESSAGES=true`) 使用 Markdown 渲染，普通模式使用 HTML。

### 模型详情页（点击某个模型）

```
⚙️ deepseek-v4-pro
━━━━━━━━━━━━━━━━━━━━━━━━━━
  [✅ 默认]  [1M ✅]
  [✅ opus]  [1M ✅]
  [◽ sonnet][1M ◽]
  [◽ haiku] [1M ◽]
━━━━━━━━━━━━━━━━━━━━━━━━━━
  [💾 保存]  [📋 返回列表]
```

- `✅` = 该角色绑定此模型，`◽` = 未绑定
- `[1M ✅]` = 带上 `[1m]` 上下文窗口后缀，`[1M ◽]` = 不带（默认 200K）
- 同一按钮等宽（2 字符），切换不抖动
- 点击 `💾 保存` 一次性写入所有勾选；点击 `📋 返回列表` 不保存
- 同一角色只绑定一个模型——后选覆盖先选

### 搜索模式

点击 `🔍 搜索` 按钮：

```
📋 模型列表 — cpa-n2   🔍 搜索中...
━━━━━━━━━━━━━━━━━━━━━━━━━━
请在聊天框中输入搜索关键词（如 deepseek），
我会过滤匹配的模型。

（回复此消息的关键词会被拦截为搜索词）
━━━━━━━━━━━━━━━━━━━━━━━━━━
  [❌ 取消搜索]
```

用户回复关键词后，过滤器匹配模型名并刷新面板。也支持命令行：

- `/model -s deep` — 搜索 deep 并打开过滤后的面板
- `/model --search deep` — 同上

### `/model <name>` 文本快捷（保留）

```
/model deepseek-v4-pro[1m]
→ 等同于在详情页勾选 [✅ 默认] + [1M ✅] 后保存
```

现有子命令全部保留：
- `/model reset` — 清除所有角色覆盖
- `/model <role> <name>` — 设置角色模型
- `/model <role> reset` — 清除某个角色模型

## 数据模型变更

### `providers.json` 新结构

删除 `model_override` 顶层字段，`default_model` 归入 profile（不兼容旧格式）：

```json
{
  "active": "default",
  "profiles": {
    "default": {
      "name": "default",
      "base_url": null,
      "auth_token": null,
      "api_key": "...",
      "default_model": "claude-sonnet-4[1m]",
      "opus_model": null,
      "sonnet_model": "claude-sonnet-4",
      "haiku_model": "claude-haiku-4[200k]",
      "description": "Auto-created from .env"
    }
  }
}
```

### ProviderManager 变更

| 操作 | 旧 | 新 |
|------|----|----|
| 设置默认模型 | `set_model_override(m)` → 写顶层 `model_override` | `set_default_model(m)` → 写 `profiles.<active>.default_model` |
| 获取有效模型 | `model_override` → `profile.default_model` → `config.claude_model` | `profile.default_model` → `config.claude_model` |
| 切换 provider | 清空 `model_override` | 无需特殊处理（默认模型跟着 profile 走） |

移除的方法和字段：
- `_model_override` 字段
- `set_model_override()`
- `get_model_source()` 中的 `"override"` 分支

新增方法：
- `fetch_models()` — 从当前 provider 拉取模型列表
- `set_default_model(model: str | None)` — 设置默认模型（同 `set_role_model` 模式）

### 模型优先级（变更后）

```
profiles.<active>.default_model  >  config.claude_model (CLAUDE_MODEL env)
```

## Provider 模型获取

### `fetch_models()` 

```
GET {base_url}/v1/models
Authorization: Bearer {api_key}
```

处理逻辑：
- 兼容 OpenAI 格式响应（`data[].id`）
- 去重 + 按名称排序
- 超时 10 秒
- 失败降级：返回空列表，面板显示 `⚠️ 无法获取模型列表，请检查 Provider 连接`
- 每次实时拉取，不缓存

### 窗口后缀

不限制——所有模型都可手动勾选 [1M]。用户自行判断该模型是否支持 100 万上下文。

## 检索的模型数量

模型列表最多展示 **20 个模型**。超出部分通过分页展示，而非截断。

## 回调路由

所有面板按钮使用 `callback_data` 前缀 `model:` 路由。

### 路由表

| 按钮 | callback_data | 处理 |
|------|--------------|------|
| 模型列表项 | `model:detail:<model_name>` | 打开该模型详情页 |
| 角色勾选 | `model:toggle:<model_name>:<role>` | 翻转角色 ✅↔◽ |
| 1M 开关 | `model:1m:<model_name>:<role>` | 翻转该角色的 1M ✅↔◽ |
| 保存 | `model:save:<model_name>` | 写入 ProviderManager，返回列表页 |
| 返回列表 | `model:list:<page>` | 回到列表第 N 页 |
| 搜索按钮 | `model:search` | 进入搜索模式 |
| 取消搜索 | `model:search_cancel` | 退出搜索，回到完整列表 |
| 分页 | `model:page:<n>` 或 `model:page:<n>:<keyword>` | 列表第 N 页（搜索模式下附加关键词） |
| 搜索过滤 | `model:filter:<keyword>` | 按关键词过滤模型并刷新列表 |

### 注册

```python
app.add_handler(
    CallbackQueryHandler(
        self._inject_deps(self._handle_model_callback),
        pattern=r"^model:",
    )
)
```

与现有的 `provider:`、`auq:`、`plan:` 同模式。

### 内存 UI 状态

类似 AUQ 多选机制，暂存于 `_model_panel_state` dict（key 为 `user_id`），仅在点击 `保存` 时落盘：

```python
_model_panel_state = {
    user_id: {
        "active_model": "deepseek-v4-pro",
        "roles": {"default": True, "opus": True, "sonnet": False, "haiku": False},
        "context_1m": {"default": True, "opus": True, "sonnet": False, "haiku": False},
        "page": 1,
        "search": None,          # None = 不在搜索模式
        "chat_id": 123456,
        "message_id": 7890,       # 面板消息 ID，用于 edit
    }
}
```

### 搜索拦截

当 `_model_panel_state[user_id].search is not None` 时，该用户的下一条文本消息被拦截（绕过正常流程）用于搜索过滤。匹配 `callback_data: model:filter:<keyword>` 刷新列表。

## 命令解析

`/model` 解析优先级：

1. 无参数 → 打开按钮面板（新行为）
2. `-s <keyword>` / `--search <keyword>` → 直接打开搜索过滤后的面板
3. `reset` → 清除所有角色覆盖（保留）
4. 第一个参数匹配角色别名 → 角色模型设置（保留）
5. 其他 → 设为默认模型（保留）

## 消息编辑策略

- **列表页翻页/搜索**：`edit_message_text` + `edit_message_reply_markup`（原地刷新，不产生新消息）
- **列表 → 详情**：`edit_message_text` + `edit_message_reply_markup`（原地替换）
- **详情页切换**：`edit_message_reply_markup`（原地更新按钮）
- **保存**：`edit_message_text` 回到列表页（原地刷新，更新顶部状态区）
- **搜索模式进入/退出**：`edit_message_text` + `edit_message_reply_markup`

所有操作优先原地编辑，避免消息刷屏。

## 错误处理

| 场景 | 处理 |
|------|------|
| `fetch_models()` 超时 | 显示 `⚠️ 无法获取模型列表`，面板只显示顶部状态区 + 手动输入提示 |
| `fetch_models()` 返回空 | 同上 |
| Provider API 返回 401/403 | 显示 `⚠️ Provider 认证失败，请检查 API Key` |
| 保存时 profile 不存在 | `query.answer("Provider 配置异常，请检查", show_alert=True)` |
| 用户在搜索模式发送了空消息 | 忽略，保持搜索等待状态 |
| 不相关用户点击按钮 | `query.answer("不是你打开的", show_alert=False)` |
| 面板消息已被删除 | `query.answer("此面板已过期，请重新 /model", show_alert=True)` |

## 检索的模型数量

模型列表最多展示 **20 个模型**。超出部分通过分页展示，而非截断。

## 文件变更清单

| 文件 | 变更 |
|------|------|
| `src/config/providers.py` | 删 `_model_override` 及 `set_model_override()`；新增 `set_default_model()`、`fetch_models()`；简化 `get_effective_model()`、`get_model_source()` |
| `src/bot/orchestrator.py` | 新增 `_handle_model_callback`、`_build_model_list_markup`、`_build_model_detail_markup`、`_enter_model_search`、`_intercept_search_text`；`agentic_model` 无参时打开面板；注册 `CallbackQueryHandler(pattern=r"^model:")` |
| `src/bot/handlers/command.py` | classic 模式 `model_command` 同步改为 `set_default_model` |

不改变的文件：
- `src/claude/sdk_integration.py` — 模型仍通过 `get_effective_model()` 传入，接口不变
- 测试文件 — 本次不新增（后续补齐）

## Non-Goals

- 不实现模型列表缓存（每次实时拉取）
- 不实现模型列表的 API 字段推断（1M 支持由用户自行判断）
- 不修改 Rich Messages 渲染管道（复用现有 `send_rich_message`）
- 不修改 ProviderManager 的 `_write_overlay` 逻辑（模型变更自动触发覆盖层更新）
