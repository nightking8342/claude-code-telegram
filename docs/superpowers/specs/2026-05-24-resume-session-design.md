# `/sessions` —— Telegram 端 Session 浏览器与恢复

- **日期**：2026-05-24
- **状态**：设计已通过，待写实施计划
- **作者**：与 brainstorming 协作产出

## 1. 背景与目标

### 1.1 现状

claude-code-telegram 已实现**自动 session 恢复**：每个 `(user_id, project_path)` 一对一持有最近一次的 session，下次用户在同目录发消息时透明续上。
但没有任何方法让用户**主动浏览历史 session、选定其中之一查看其内容、或显式恢复某个旧 session**。已有的命令 `/new`、`/end`、`/continue`、`/status`、`/export` 均不能跨 session 浏览，全仓 grep 不到任何 `/sessions` / `list_sessions` 雏形。

### 1.2 用户场景

用户在 Telegram 端使用桥接，想要：

1. 浏览自己在当前目录下的历史 session 列表
2. 查看某个 session 的完整对话内容（包括代码、tool calls 等细节）
3. **可选**：显式恢复某个旧 session 继续聊（不一定每次都恢复）
4. 可选：把某个 session 的内容导出成文件做归档

### 1.3 设计硬约束

由用户在 brainstorming 中明确给出：

- **C1**：查看会话内容**不能直接刷在聊天里**（会话内容可能数百条，刷屏不可接受）
- **C2**：但**必须能看到详细内容**，不能只是摘要
- **C3**：会话查看功能**绝不占用 AI 上下文**（任何路径都不调用 Claude SDK）
- **C4**：**以代码方式实现**（不依赖外部 web 服务、telegraph、第三方平台）

### 1.4 范围

**In scope**：

- 新命令 `/sessions`
- 仅列出**当前工作目录**下、当前用户的 session
- 两层 UI：列表（每行一 session，点进详情）→ 详情（三个动作按钮）
- 详情动作：**[查看 HTML]、[恢复继续]、[导出其它格式（MD/JSON）]**
- HTML 通过 `sendDocument` 发送，复用现有 `SessionExporter`
- 分页（10 条/页）
- agentic 与 classic 双模式都注册

**Out of scope**：

- 跨目录 / 跨项目浏览
- 删除 session / 重命名 session
- 在聊天里分页刷出对话内容
- Telegram Web App / 自托管 web view
- 任何引入新外部服务的方案（Telegraph 等）
- 修改数据库 schema（不加 title 列；aiTitle 实时从 jsonl 读）

## 2. 关键决策记录

| # | 决策 | 选定方案 | 否决方案 / 理由 |
|---|------|----------|-----------------|
| D1 | session 列表的范围 | **仅当前目录** | 跨项目分组列表过长、混乱；用户切换项目本就用 `/cd`，跨项目查询不是常态 |
| D2 | session 列表中如何识别每个 session | **CLI 已生成的 `ai-title`**（实时读 jsonl），不命中时回退首条 user 消息前 60 字 | 自建标题表会冗余、Claude 调用会有成本、首条消息单独使用语义弱 |
| D3 | "查看历史内容"的呈现机制 | **HTML 文件附件**（`sendDocument`） | 聊天里分页刷会刷屏；Telegraph 把私密内容上公网；Web App 工程量大、需 HTTPS+域名+鉴权 |
| D4 | 列表 UI 形态 | **一行一个 session，点进详情** | 每行多按钮容易拥挤；先选后出按钮多一步交互 |
| D5 | 详情页保留的动作 | **[查看 HTML][恢复继续][导出其它格式]**，二级菜单挑 MD / JSON | 删除/重命名是低频/可后补能力 |
| D6 | 命令名 | **`/sessions`** | `/resume` 偏向单一动作语义、`/history` 与 messages 历史概念冲突 |
| D7 | 分页方式 | **10 条/页 + 翻页按钮** | "默认列 20 可扩全部"实现复杂度差不多但更易越界；不分页一次发完不直观 |
| D8 | "恢复继续"的实现 | **改 `context.user_data["claude_session_id"]`** ，下次用户发消息时 auto-resume 路径接管 | 不主动调 SDK ——满足约束 C3；与现有 `/new`/`/continue` 实现对称 |
| D9 | aiTitle 读取来源 | **倒序读 `~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`** 找最后一个 `type=ai-title` | 不缓存到 DB（用户没要求增加 schema 字段）；倒序读避免大文件加载到内存 |

## 3. 架构

### 3.1 核心原则

1. **纯代码路径**：列表、HTML 生成、文件发送、恢复切换全部在 bot 进程内完成，**任何代码路径都不会触发 Claude SDK 调用**
2. **复用现有基础设施**：`SessionExporter`、`SessionStorage`、callback 路由、`AGENTIC_MODE` 双模式
3. **零新外部依赖**：不引入 telegraph、不起新 web service、不改 schema

### 3.2 数据来源

| 数据 | 来源 | 用途 |
|------|------|------|
| session 元数据（id、cwd、message_count、cost、时间） | SQLite `sessions` 表 | 列表渲染、详情元数据 |
| session 对话历史 | SQLite `messages` 表 | HTML / MD / JSON 内容生成 |
| 语义标题 `aiTitle` | `~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`（CLI 自动写入） | 列表行标题（新增读取） |

`ai-title` 行的结构（已验证）：

```json
{"type": "ai-title", "aiTitle": "Debug status command showing 0% context", "sessionId": "..."}
```

CLI 在对话推进后会重复追加（同一文件可能出现上百次），**总取最后一行**为当前标题。

### 3.3 两层 UI

```
/sessions
  ┌─ "📂 当前目录的 sessions（第 1 页 / 共 N 页）"
  ├─ [aiTitle 标题 · 2h ago · 23 条]   → callback "sessions:detail:<id>"
  ├─ [aiTitle 标题 · 3h ago · 8 条]
  ├─ ...至多 10 行...
  └─ [< 上一页]    [下一页 >]            → callback "sessions:list:<page>"

→ 详情页（edit_message_text 替换列表消息）：
  "📄 «aiTitle»
   创建于 2026-05-22 14:32
   最近活动 2h ago · 23 条消息 · $0.18
   session id: e9c7e144-..."
  
  [📄 查看 HTML]                          → callback "sessions:view:<id>"
  [▶ 恢复继续]                           → callback "sessions:resume:<id>"
  [📦 导出其它格式]                       → callback "sessions:export:<id>"
  [← 返回列表]                           → callback "sessions:back:<page>"
```

## 4. 组件清单

### 4.1 新增

**`src/bot/features/session_browser.py`** ——核心新文件：

| 函数 | 职责 |
|------|------|
| `list_sessions_view(user_id, project_path, page, page_size=10)` | 查 SQLite + 读 ai-title，返回 `(text, InlineKeyboardMarkup)` |
| `session_detail_view(session_id)` | 查单个 session 元数据 + ai-title，返回详情卡 + 三按钮 |
| `read_ai_title(project_path, session_id) -> Optional[str]` | 倒序扫描 jsonl 最后 ≤1MB 找 `type=ai-title` |
| `derive_fallback_title(session_id) -> str` | aiTitle 缺失时从 SQLite `messages` 取首条 user prompt 前 60 字（剥掉 `<command-message>` 等包裹） |
| `encode_project_path(path) -> str` | 复现 SDK 的 cwd → 目录名 编码规则（如 `D:\foo\bar` → `D--foo-bar`） |

### 4.2 修改

| 文件 | 改动 |
|------|------|
| `src/bot/handlers/command.py` | 加 `sessions_command()` —— classic 模式入口 |
| `src/bot/orchestrator.py` | agentic 路径加 `sessions_command()`；`_register_*_handlers()` 注册；`get_bot_commands()` 加 `/sessions` 描述；审计日志 |
| `src/bot/handlers/callback.py` | 新增分发分支：`sessions:list:<page>`、`sessions:detail:<id>`、`sessions:view:<id>`、`sessions:resume:<id>`、`sessions:export:<id>`、`sessions:export:<id>:<fmt>`、`sessions:back:<page>` |
| `src/bot/features/session_export.py` | 验证 / 拓宽 `export_to_html / markdown / json` 是否支持显式 `session_id` 入参 |
| `src/storage/session_storage.py` | `get_user_sessions()` 增 `limit`/`offset`；新增 `count_user_sessions(user_id, project_path)` |

### 4.3 不需要改

- **数据库 schema 不变**
- **`claude_integration` / SDK 集成不变**
- **不增依赖**（标准库 `json` 和已在依赖里的 `aiofiles` 即可）

## 5. 数据流（五条主路径）

### 5.1 `/sessions` 列表初始页

```
TG 消息 /sessions
  → middleware（security/auth/rate）
  → sessions_command()
    → SessionStorage.count_user_sessions(user, cwd)
    → SessionStorage.get_user_sessions(user, cwd, limit=10, offset=0)
        ↓（10 行 session 元数据）
    → 对每行 session 并行：read_ai_title(cwd, session_id)
        ↓（10 个标题，未拿到的回退到 derive_fallback_title）
    → 拼 "第 1 页 / 共 N 页" 文本 + 10 个按钮 + [<][>]
  → reply_text(text, reply_markup=keyboard)
  → 审计日志
```

### 5.2 翻页 / 进详情

```
TG callback "sessions:list:<page>"
  → callback_handler 分发到 sessions_callback()
  → 同 5.1，offset = page * 10
  → edit_message_text 替换当前消息（不发新消息）

TG callback "sessions:detail:<id>"
  → SessionStorage.get_session(id)
  → read_ai_title + 元数据格式化
  → edit_message_text 替换列表为详情卡 + 四按钮
```

### 5.3 [查看 HTML]

```
TG callback "sessions:view:<id>"
  → answer_callback_query("生成中…")
  → SessionExporter.export_to_html(session_id) → bytes
  → send_document(chat_id, html_bytes, filename=f"{safe_aiTitle}_{date}.html")
  → 审计日志
  ❌ 全程不经 Claude SDK
```

### 5.4 [恢复继续]

```
TG callback "sessions:resume:<id>"
  → 校验 session.user_id == effective_user.id & is_active
  → context.user_data["claude_session_id"] = id
  → context.user_data["force_new_session"] = False
  → reply_text("✅ 已切到 session «<aiTitle>»，发消息即继续。")
  → 审计日志

后续用户发消息 → run_command 走现有 auto-resume 路径，
带这个 id 给 SDK → SDK 续上原 session
```

### 5.5 [导出其它格式]

```
TG callback "sessions:export:<id>"
  → edit_message_text 出二级按钮 [MD] [JSON] [取消]

TG callback "sessions:export:<id>:md"（或 :json）
  → SessionExporter.export_to_markdown(id) / export_to_json(id)
  → send_document(...)
  → 审计日志
```

### 5.6 关键不变量

- **任何路径都不调用 `claude_integration.run_command()` 或 SDK 接口** —— 浏览 / 查看 / 导出全是 SQLite + 本地 jsonl + 文件发送
- **"恢复"也不主动调 SDK**，只改 `context.user_data`；真正的 SDK 调用要等用户发下一条消息时自动发生
- **跨用户隔离**：每个 callback 都先校验 `session.user_id == update.effective_user.id`

## 6. 错误处理

### 6.1 数据缺失类

| 场景 | 处理 |
|------|------|
| jsonl 不存在 / 损坏 / 还没生成 ai-title | 静默 fallback → 首条 user 消息前 60 字 |
| 首条 user 消息也没有 | 最终兜底：`Session <id 前 8 位>` |
| jsonl 文件超大 | 倒序读最后 1MB，找不到 `type=ai-title` 则 fallback |
| messages 表为空 | HTML/MD/JSON 模板渲染"（空 session）"占位，不抛错 |

### 6.2 权限 / 一致性

| 场景 | 处理 |
|------|------|
| 跨用户 callback（session.user_id ≠ 当前用户） | `answer_callback_query("无权访问该 session", show_alert=True)`，**写审计日志**，不返回列表（避免列表本身被用作信息泄露） |
| callback 携带的 session_id 不存在 | `answer_callback_query("session 不存在或已删除")`，刷回列表第一页 |
| session 被标记 `is_active=FALSE` | `get_user_sessions` 已过滤；详情入口二次校验 |
| 页码越界 | clamp 到 `[0, total_pages-1]` |

### 6.3 系统类

| 场景 | 处理 |
|------|------|
| 列表为空 | "当前目录下还没有 session — 直接发条消息开个新的吧"，不渲染翻页按钮 |
| `send_document` 失败 | 重试 1 次；仍失败 → `reply_text` 错误说明 + 审计 |
| HTML 生成抛异常 | catch → `reply_text("生成 HTML 失败：<异常类名>")`，不暴露 traceback |
| `encode_project_path` 与 SDK 不一致 → 找不到 jsonl | **实施前先看 SDK 源码对齐编码规则**；运行期当 jsonl 不存在处理 |

### 6.4 Telegram 协议

| 场景 | 处理 |
|------|------|
| `callback_data` ≤ 64 字节限制 | 最长 `sessions:export:<UUID 36>:json` = 57 字节（`sessions:export:` 16 + UUID 36 + `:json` 5），**实施期单测覆盖** |
| 按钮 ≤ 100 限制 | 最多 10 + 2 = 12 个按钮，远低于 |

### 6.5 审计点（强制）

- `/sessions` 命令本身
- 详情查看（`sessions:detail`）
- HTML / MD / JSON 导出
- **恢复继续**（状态变更，必须审计）
- 跨用户访问尝试

### 6.6 故意不处理

- 多端同时改同个 session 的 active 状态：`context.user_data` per-chat、per-user，不会冲突
- session 总数极大（>1000）：当前目录场景下不会触发

## 7. 测试策略

### 7.1 单元测试（`tests/unit/`）

**`test_session_browser.py`** —— 新文件：

| 用例 | 验证 |
|------|------|
| `test_read_ai_title_finds_last_in_jsonl` | 多个 ai-title 时取最后一个 |
| `test_read_ai_title_missing_file_returns_none` | jsonl 不存在 → None |
| `test_read_ai_title_corrupt_jsonl_returns_none` | 部分行损坏 → 跳过损坏行 + 仍能找到合法的 |
| `test_read_ai_title_huge_file_reads_tail_only` | 构造 2MB 假 jsonl，验证只读最后 1MB |
| `test_derive_fallback_title_strips_command_wrappers` | 含 `<command-message>` 时剥掉 |
| `test_derive_fallback_title_truncates_to_60` | 长消息截到 60 字 + 省略号 |
| `test_encode_project_path_matches_sdk` | 数据驱动：`D:\foo\bar` → `D--foo-bar`、`C:\Users\WHY` → `C--Users-WHY`、Linux 路径同样验证（具体规则以 SDK 源码为准） |
| `test_list_sessions_view_pagination` | 25 个 session，page=0/1/2 的分页正确、最后页不足 10 个仍显示、page>=3 clamp |
| `test_list_sessions_view_empty` | 0 个 session → 无翻页按钮 |
| `test_callback_data_under_64_bytes` | 所有 callback prefix + UUID 拼出来 ≤ 64 字节 |

**`test_session_storage_pagination.py`** —— 拓展现有 storage 测试：

| 用例 | 验证 |
|------|------|
| `test_get_user_sessions_with_limit_offset` | limit+offset 正确切片 + 按 `last_used DESC` 排序 |
| `test_count_user_sessions` | 与 `get_user_sessions(limit=None)` 长度一致 |
| `test_count_excludes_inactive` | `is_active=FALSE` 不计入 |

### 7.2 集成测试（`tests/integration/`）

**`test_sessions_command_flow.py`** —— mock Telegram update + context：

| 用例 | 验证 |
|------|------|
| `test_sessions_command_lists_only_current_dir` | 用户在两个不同目录都有 session，`/sessions` 只列当前目录 |
| `test_sessions_command_other_user_isolation` | 用户 A 的 session 不会被用户 B 在同目录列出 |
| `test_detail_callback_cross_user_denied` | 用户 B 伪造 `sessions:detail:<A 的 id>` → 审计 + 拒绝 |
| `test_resume_callback_sets_user_data` | 点恢复后 `context.user_data["claude_session_id"]` 被设置 + `force_new_session=False` |
| `test_view_html_sends_document` | mock `send_document`，验证被调 + filename 含 aiTitle |
| `test_view_html_fallback_filename` | 无 aiTitle 时 filename 用 session_id 前 8 位 |
| `test_export_subflow_md_and_json` | `sessions:export:<id>` → 二级菜单 → MD/JSON 各发 |
| `test_callback_to_nonexistent_session` | 篡改 session_id → 友好错误 + 刷回列表 |

### 7.3 不写单测

- **SessionExporter 已有的导出逻辑** —— 已被现有 `/export` 测试覆盖
- **callback 路由本身** —— 路由分发已有测试，我们只新增分支
- **审计日志输出格式** —— 项目其它命令都未逐字段断言审计日志

### 7.4 手测（发布前必须）

在真 Telegram 客户端跑：

1. `/sessions` 列表显示 + 翻页
2. 点详情 → [查看 HTML] → 在 Telegram 桌面 + 手机各打开一次 HTML
3. 点 [恢复继续] → 发条消息确认 SDK 真的拿到了正确的 session_id（看日志）
4. 点 [导出其它格式] → MD / JSON 都能下载并正确显示
5. 列表里有 0 / 1 / 10 / 25 个 session 时 UI 都正常

## 8. 风险与未决项

| 风险 | 缓解 |
|------|------|
| **path encoding 与 SDK 实现不一致** | 实施第一步先看 `claude-agent-sdk` 源码（或抓一遍线上目录名）确认编码规则；写完单测多个平台数据驱动 |
| **`SessionExporter` 现有方法不支持显式 session_id 入参** | 实施时验证；若需小改动则连带改 |
| **agentic 与 classic 在 `context.user_data` 上的 session 状态键名不一致** | 实施第一步在两个 handler 路径都 grep `claude_session_id`/`force_new_session` 确认键名；若不同则在 SessionBrowser 层封装统一 setter |
| **jsonl ai-title 读取的并发性能** | 列表每行并行读取，10 行最多 10 个 small file IO；async 化即可，不需要批量优化 |

## 9. 不写之前先确认

实施期开工前，应先：

1. 在 `claude-agent-sdk` 源码或现有 `~/.claude/projects/*` 实际目录中**确认 cwd 编码算法**
2. 在 `session_export.py` 中**确认 `export_to_html / markdown / json` 是否接收显式 `session_id`** —— 不接收则需小改
3. 在 `command.py:310`、`orchestrator.py`、`facade.py` 中**统一 session 切换的 user_data 键名**（`claude_session_id`、`force_new_session`）

这些是开 PR 前要快速核对的，不算未决（确认即可），但留出验证窗口。
