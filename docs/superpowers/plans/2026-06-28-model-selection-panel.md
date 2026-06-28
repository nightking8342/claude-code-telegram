# 模型选择面板 实现计划

> **给 agentic workers：** 需要子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 来按任务逐个实现。步骤使用 checkbox（`- [ ]`）语法跟踪。

**目标：** 将 `/model` 命令从纯文本交互升级为 Telegram Inline Keyboard 面板，支持模型列表浏览（分页+搜索）、模型详情页（角色勾选 + 1M 上下文窗口切换）、废弃 `model_override` 顶层字段。

**架构：** 数据层在 `ProviderManager` 中增加 `fetch_models()` 和 `set_default_model()`；UI 层在 `MessageOrchestrator` 中新增回调处理器，内存暂存面板状态，仅在保存时落盘。现有文本子命令保留。

**技术栈：** Python 3.10+, python-telegram-bot, aiohttp（HTTP 调用）, 现有 ProviderManager / InlineKeyboardMarkup 模式。

## 全局约束

- 所有文件操作必须在 `APPROVED_DIRECTORY` 内
- 删除 `model_override` 顶层字段，`default_model` 归入 profile（不兼容旧 JSON）
- 回调 `callback_data` 使用 `model:` 前缀
- ✅/◽ 等宽 2 字符用于勾选状态
- Rich Messages 模式在面板列表页不适用（inline keyboard 与 markdown table 无法共存）；面板统一使用纯文本 + inline buttons
- `/model` 无参 = 面板，有参 = 文本快捷（保留）
- `fetch_models()` 每次实时拉取，10 秒超时
- 模型列表每页最多 20 个

---

### Task 1: ProviderManager — 删除 model_override 并新增 set_default_model

**文件:**
- 修改：`src/config/providers.py:82-93`（`__init__`、`_load`）、`src/config/providers.py:109-120`（`_save`）、`src/config/providers.py:147-156`（`switch_profile`）、`src/config/providers.py:166-208`（model override 区块）、`src/config/providers.py:92`（`get_model_source`）

**接口:**
- 消费：无（Task 1 是基础层）
- 产出：`ProviderManager.set_default_model(model: str | None) -> None`、`ProviderManager.get_effective_model() -> Optional[str]`（简化版，去掉 `_model_override` 分支）、`ProviderManager.get_model_source() -> str`（去掉 `"override"`）

- [ ] **Step 1: 修改测试 — 将 model_override 替换为 profile.default_model**

```python
# tests/unit/test_providers.py

def test_build_env_overlay_reflects_default_model_set_via_set_default_model(tmp_path):
    """set_default_model 写入 profile.default_model，影响 overlay 输出。"""
    pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")

    pm.set_default_model("claude-opus-4-8")
    overlay = pm.build_env_overlay()

    assert overlay["ANTHROPIC_MODEL"] == "claude-opus-4-8"
    assert overlay["ANTHROPIC_BASE_URL"] == "https://cpa.example"


def test_set_default_model_clears_when_none(tmp_path):
    """set_default_model(None) 清除 profiles.<active>.default_model。"""
    pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")
    pm.set_default_model(None)

    profile = pm.get_active()
    assert profile.default_model is None
    assert pm.get_effective_model() is None  # config.claude_model is None


def test_switch_profile_no_longer_clears_model_override(tmp_path):
    """切换 provider 保持各自 profile 的 default_model 不变。"""
    pm = _seed(
        tmp_path,
        {"a": {**_SPARSE, "name": "a", "default_model": "model-a[1m]"},
         "b": {**_SPARSE, "name": "b", "default_model": "model-b[200k]"}},
        active="a",
    )
    assert pm.get_effective_model() == "model-a[1m]"

    pm.switch_profile("b")
    assert pm.get_effective_model() == "model-b[200k]"

    pm.switch_profile("a")
    assert pm.get_effective_model() == "model-a[1m]"


def test_get_model_source_no_more_override(tmp_path):
    """get_model_source 不再返回 'override'。"""
    pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")
    assert pm.get_model_source() == "profile"

    config_with_model = SimpleNamespace(claude_model="from-env", anthropic_api_key_str=None)
    pm2 = _seed(tmp_path, {"empty": {**_SPARSE, "name": "empty", "default_model": None}},
                active="empty")
    assert pm2.get_effective_model() is None  # config.claude_model 在 _seed 中是 None


def test_save_no_longer_writes_model_override(tmp_path):
    """_save 不再写入 model_override 顶层字段。"""
    pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")
    pm.set_default_model("some-model[1m]")

    raw = json.loads(pm._storage_path.read_text(encoding="utf-8"))
    assert "model_override" not in raw
    assert raw["profiles"]["cpa"]["default_model"] == "some-model[1m]"
```

同时更新 `_seed` 辅助函数，移除 `model_override` 参数:

```python
# tests/unit/test_providers.py — 更新 _seed

def _seed(tmp_path: Path, profiles: dict, active: str):
    """Construct a ProviderManager from a pre-written providers.json (no model_override)."""
    storage = tmp_path / "providers.json"
    storage.write_text(
        json.dumps({"active": active, "profiles": profiles}),
        encoding="utf-8",
    )
    config = SimpleNamespace(claude_model=None, anthropic_api_key_str=None)
    return ProviderManager(config, storage_path=storage)
```

- [ ] **Step 2: 运行测试验证失败**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_providers.py -v
```
预期：新增的 5 个测试失败（`set_default_model` 未定义、`model_override` 仍存在），旧的 `test_build_env_overlay_reflects_model_override` 失败（`set_model_override` 被移除或行为改变）。

- [ ] **Step 3: 实现 ProviderManager 变更**

修改 `src/config/providers.py`：

```python
# 1. __init__ — 删除 _model_override 字段
def __init__(self, config, storage_path: Path = Path("data/providers.json")):
    self._config = config
    self._storage_path = storage_path
    self._profiles: Dict[str, ProviderProfile] = {}
    self._active_name: Optional[str] = None
    self._load()

# 2. _load — 不再读取 model_override 顶层字段
def _load(self) -> None:
    if self._storage_path.exists():
        try:
            data = json.loads(self._storage_path.read_text(encoding="utf-8"))
            self._active_name = data.get("active")
            for name, pdata in data.get("profiles", {}).items():
                self._profiles[name] = ProviderProfile(**pdata)
            logger.info(
                "Loaded provider profiles",
                count=len(self._profiles),
                active=self._active_name,
            )
            self._write_overlay()
            return
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed to parse providers.json, recreating", error=str(exc))
    self._auto_create_default()

# 3. _save — 不再写入 model_override
def _save(self) -> None:
    self._storage_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "active": self._active_name,
        "profiles": {n: asdict(p) for n, p in self._profiles.items()},
    }
    self._storage_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    self._write_overlay()
    logger.debug("Saved provider profiles", path=str(self._storage_path))

# 4. switch_profile — 不再清空 model_override
def switch_profile(self, name: str) -> ProviderProfile:
    if name not in self._profiles:
        raise KeyError(
            f"Provider '{name}' not found. Available: {', '.join(self._profiles)}"
        )
    self._active_name = name
    self._save()
    logger.info("Switched provider", provider=name)
    return self._profiles[name]

# 5. 删除 set_model_override 方法，替换为 set_default_model
def set_default_model(self, model: Optional[str]) -> None:
    """Set the default model on the active profile (writes to profile.default_model)."""
    active = self.get_active()
    if not active:
        raise RuntimeError("No active provider profile")
    active.default_model = model
    self._save()
    if model:
        logger.info("Default model set", model=model)
    else:
        logger.info("Default model cleared")

# 6. get_effective_model — 去掉 _model_override 分支
def get_effective_model(self) -> Optional[str]:
    """Return the effective model name (may include [1m] suffix)."""
    active = self.get_active()
    if active and active.default_model:
        return active.default_model
    return getattr(self._config, "claude_model", None)

# 7. get_model_source — 去掉 "override" 分支
def get_model_source(self) -> str:
    """Return where the effective model comes from."""
    active = self.get_active()
    if active and active.default_model:
        return "profile"
    return "settings"
```

```bash
git add src/config/providers.py tests/unit/test_providers.py
git commit -m "refactor(providers): replace model_override with profile.default_model

Delete top-level model_override field. set_default_model writes to
profiles.<active>.default_model, consistent with set_role_model.
get_effective_model now reads directly from profile.default_model.
switch_profile no longer clears a global override.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

- [ ] **Step 4: 运行测试验证通过**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_providers.py -v
```
预期：全部 PASS。

- [ ] **Step 5: 提交**

---

### Task 2: ProviderManager — 新增 fetch_models()

**文件:**
- 修改：`src/config/providers.py`（新增方法）

**接口:**
- 消费：`ProviderManager.get_active()` → 获取 `base_url`、`api_key`
- 产出：`ProviderManager.fetch_models() -> List[str]` — 返回去重排序后的模型名列表，失败返回空列表并记录日志

- [ ] **Step 1: 编写测试**

```python
# tests/unit/test_providers.py — 追加

from unittest.mock import AsyncMock, MagicMock, patch

def test_parse_models_response_dedup_and_sort():
    """_parse_models_response 去重并按名称排序。"""
    from src.config.providers import _parse_models_response

    raw = {
        "object": "list",
        "data": [
            {"id": "z-model", "object": "model"},
            {"id": "a-model", "object": "model"},
            {"id": "z-model", "object": "model"},  # dup
            {"id": "b-model", "object": "model"},
        ]
    }
    result = _parse_models_response(raw)
    assert result == ["a-model", "b-model", "z-model"]


def test_parse_models_response_empty():
    """空 data 或异常格式返回空列表。"""
    from src.config.providers import _parse_models_response

    assert _parse_models_response({}) == []
    assert _parse_models_response({"data": []}) == []
    assert _parse_models_response({"data": [{"no_id": "x"}]}) == []
```

- [ ] **Step 2: 运行测试验证失败**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_providers.py -k "parse_models" -v
```
预期：FAIL（`_parse_models_response` 未定义）。

- [ ] **Step 3: 实现 fetch_models() 和 _parse_models_response()**

```python
# src/config/providers.py — 在 imports 区域添加
import aiohttp

# 在 _CONTEXT_SUFFIX_RE 后添加
_MODELS_TIMEOUT = aiohttp.ClientTimeout(total=10)

# 在 _parse_context_suffix 函数后面、ProviderProfile 前面添加
def _parse_models_response(data: dict) -> list:
    """Extract, deduplicate, and sort model IDs from an OpenAI-compatible response."""
    models = data.get("data", [])
    ids = sorted(
        {m["id"] for m in models if isinstance(m, dict) and "id" in m}
    )
    return ids

# 在 ProviderManager 中，set_default_model 之后添加
async def fetch_models(self) -> list:
    """Fetch available models from the active provider's /v1/models endpoint.

    Returns a sorted, deduplicated list of model IDs. Returns an empty list
    on failure (network error, timeout, or unexpected response format).
    """
    active = self.get_active()
    if not active or not active.base_url:
        logger.warning("fetch_models: no active profile or base_url")
        return []

    url = active.base_url.rstrip("/") + "/v1/models"
    headers = {}
    if active.api_key:
        headers["Authorization"] = f"Bearer {active.api_key}"
    elif active.auth_token:
        headers["Authorization"] = f"Bearer {active.auth_token}"

    try:
        async with aiohttp.ClientSession(timeout=_MODELS_TIMEOUT) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    models = _parse_models_response(data)
                    logger.info(
                        "fetch_models succeeded",
                        url=url,
                        count=len(models),
                    )
                    return models
                else:
                    logger.warning(
                        "fetch_models: non-200 response",
                        url=url,
                        status=resp.status,
                    )
                    return []
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("fetch_models failed", url=url, error=str(exc))
        return []
```

- [ ] **Step 4: 运行测试验证通过**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_providers.py -k "parse_models" -v
```
预期：PASS。

- [ ] **Step 5: 提交**

```bash
git add src/config/providers.py tests/unit/test_providers.py
git commit -m "feat(providers): add fetch_models() for dynamic model list retrieval

GETs /v1/models from the active provider. Parses OpenAI-compatible format.
10-second timeout. Returns deduplicated sorted list on success, empty list
on failure.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: orchestrator — 更新 agentic_model 文本子命令使用 set_default_model

**文件:**
- 修改：`src/bot/orchestrator.py:986-1024`（`agentic_model` 中的 `set_model_override` 调用）

**接口:**
- 消费：`ProviderManager.set_default_model()`（Task 1 产出）
- 产出：文本子命令行为不变，仅改调用方法名

- [ ] **Step 1: 运行现有 orchestrator 测试确认基线**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_orchestrator.py -v
```
预期：全部通过或已有的失败（确认基线）。

- [ ] **Step 2: 修改 agentic_model 中的调用**

修改 `src/bot/orchestrator.py`：

```python
# Line 987: /model reset 分支
pm.set_default_model(None)  # 原: pm.set_model_override(None)

# Line 1018: /model <name> 分支
pm.set_default_model(first)  # 原: pm.set_model_override(first)
```

- [ ] **Step 3: 验证语法编译**

```bash
.\.venv\Scripts\python.exe -m py_compile src/bot/orchestrator.py
```
预期：Compiled successfully。

- [ ] **Step 4: 提交**

```bash
git add src/bot/orchestrator.py
git commit -m "refactor(orchestrator): use set_default_model instead of set_model_override

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: command.py — classic 模式同步改为 set_default_model

**文件:**
- 修改：`src/bot/handlers/command.py:1331, 1362`

**接口:**
- 消费：`ProviderManager.set_default_model()`（Task 1 产出）
- 产出：classic 模式 /model 文本子命令行为不变

- [ ] **Step 1: 修改 model_command**

```python
# src/bot/handlers/command.py

# Line 1331: /model reset 分支
pm.set_default_model(None)  # 原: pm.set_model_override(None)

# Line 1362: /model <name> 分支
pm.set_default_model(first)  # 原: pm.set_model_override(first)
```

- [ ] **Step 2: 验证编译**

```bash
.\.venv\Scripts\python.exe -m py_compile src/bot/handlers/command.py
```
预期：Compiled successfully。

- [ ] **Step 3: 提交**

```bash
git add src/bot/handlers/command.py
git commit -m "refactor(command): use set_default_model in classic mode model_command

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: orchestrator — /model 无参打开按钮面板（列表页）

**文件:**
- 修改：`src/bot/orchestrator.py`

**接口:**
- 消费：`ProviderManager.fetch_models()`（Task 2）、`ProviderManager.get_effective_model()`（Task 1）、`ProviderManager.get_role_models()`、`ProviderManager.get_active_name()`
- 产出：`_model_panel_state` dict、`_build_model_list_markup()`、`_show_model_list()`、`_build_panel_status_header()`

- [ ] **Step 1: 添加 MessageOrchestrator 初始化字段**

在 `MessageOrchestrator.__init__`（`orchestrator.py:244`）添加：

```python
# Model panel UI state (key = user_id)
self._model_panel_state: Dict[int, Dict[str, Any]] = {}
```

- [ ] **Step 2: 添加 _build_panel_status_header 静态方法**

```python
@staticmethod
def _build_panel_status_header(pm: Any) -> str:
    """Build the status header showing current model assignment per role."""
    active_name = pm.get_active_name() or "unknown"
    default_raw = pm.get_effective_model() or "—"
    default_name, default_win = _parse_context_suffix(default_raw)
    default_label = f"{default_name} [{'1M' if default_win >= 1_000_000 else '200K'}]"

    lines = [
        f"📋 模型列表 — {active_name}",
        "━" * 16,
        f"默认: {default_label}",
    ]

    role_emojis = {"opus": "🐙", "sonnet": "🟡", "haiku": "🟢"}
    roles = pm.get_role_models()
    for role in ("opus", "sonnet", "haiku"):
        rm = roles.get(role)
        if rm:
            name, win = _parse_context_suffix(rm)
            win_label = f"[{'1M' if win >= 1_000_000 else '200K'}]"
            lines.append(f"{role_emojis[role]} {role}: {name} {win_label}")
        else:
            lines.append(f"{role_emojis[role]} {role}: —")

    return "\n".join(lines)
```

- [ ] **Step 3: 添加 _build_model_list_markup 方法**

```python
MODELS_PER_PAGE = 20  # per spec: 超出部分通过分页展示

def _build_model_list_markup(
    self,
    models: List[str],
    page: int,
    search: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """Build inline keyboard for model list view."""
    # Filter if searching
    if search:
        models = [m for m in models if search.lower() in m.lower()]
        # Keep only the first page of filtered results (search results fit in one page)
        start, end = 0, MODELS_PER_PAGE
    else:
        total_pages = max(1, (len(models) + MODELS_PER_PAGE - 1) // MODELS_PER_PAGE)
        page = max(0, min(page, total_pages - 1))
        start = page * MODELS_PER_PAGE
        end = min(start + MODELS_PER_PAGE, len(models))

    row: List[InlineKeyboardButton] = []
    for m in models[start:end]:
        # Truncate model name for button
        label = m if len(m) <= 40 else m[:37] + "..."
        row.append(
            InlineKeyboardButton(label, callback_data=f"model:detail:{m}")
        )
    keyboard = self._chunk_buttons(row, 2)

    # Bottom row: search + pagination
    bottom_row: List[InlineKeyboardButton] = []
    bottom_row.append(InlineKeyboardButton("🔍 搜索", callback_data="model:search"))

    if search:
        bottom_row.append(InlineKeyboardButton("❌ 取消搜索", callback_data="model:search_cancel"))
    else:
        total_pages = max(1, (len(models) + MODELS_PER_PAGE - 1) // MODELS_PER_PAGE)
        if page > 0:
            bottom_row.append(InlineKeyboardButton("⬅", callback_data=f"model:page:{page - 1}"))
        bottom_row.append(InlineKeyboardButton(
            f"{page + 1}/{total_pages}",
            callback_data="model:noop"
        ))
        if page < total_pages - 1:
            bottom_row.append(InlineKeyboardButton("➡", callback_data=f"model:page:{page + 1}"))

    keyboard.append(bottom_row)
    return InlineKeyboardMarkup(keyboard)
```

- [ ] **Step 4: 添加 _show_model_list 方法**

```python
async def _show_model_list(
    self,
    update_or_query: Union[Update, CallbackQuery],
    context: ContextTypes.DEFAULT_TYPE,
    pm: Any,
    user_id: int,
    page: int = 0,
    search: Optional[str] = None,
) -> None:
    """Render the model list panel. Uses edit_message if source is callback query."""
    models = await pm.fetch_models()

    header = self._build_panel_status_header(pm)

    if not models:
        text = f"{header}\n\n⚠️ 无法拉取模型列表，请检查 Provider 连接。"
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 重试", callback_data=f"model:list:0")
        ]])
    else:
        text = header
        markup = self._build_model_list_markup(models, page, search)

    # Store panel state
    is_callback = hasattr(update_or_query, "data")
    if is_callback:
        query = update_or_query
        chat_id = query.message.chat_id
        message_id = query.message.message_id
    else:
        msg = update_or_query.message
        chat_id = msg.chat_id
        message_id = None

    self._model_panel_state[user_id] = {
        "page": page,
        "search": search,
        "chat_id": chat_id,
        "message_id": message_id,
    }

    if is_callback and message_id:
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            await query.answer("面板已过期，请重新 /model", show_alert=True)
    else:
        sent = await update_or_query.message.reply_text(text, reply_markup=markup)
        self._model_panel_state[user_id]["message_id"] = sent.message_id
        self._model_panel_state[user_id]["chat_id"] = sent.chat_id
```

- [ ] **Step 5: 修改 agentic_model 无参数分支**

在 `agentic_model` 的 `if not args:` 分支（line 923）中，替换原有显示逻辑：

```python
if not args:
    await self._show_model_list(update, context, pm, user_id=update.effective_user.id)
    return
```

- [ ] **Step 6: 验证编译**

```bash
.\.venv\Scripts\python.exe -m py_compile src/bot/orchestrator.py
```
预期：Compiled successfully。

- [ ] **Step 7: 提交**

```bash
git add src/bot/orchestrator.py
git commit -m "feat(orchestrator): add model list panel for /model without args

Adds _show_model_list, _build_model_list_markup, _build_panel_status_header.
/model with no arguments now opens an interactive model browser with
pagination instead of showing plain text.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 6: orchestrator — 注册 model: 回调处理器和分页/列表导航

**文件:**
- 修改：`src/bot/orchestrator.py`

**接口:**
- 消费：`_show_model_list()`（Task 5）、`_model_panel_state`
- 产出：`_handle_model_callback()`、CallbackQueryHandler 注册

- [ ] **Step 1: 注册 CallbackQueryHandler**

在 `_register_agentic_handlers`（provider callback 注册之后，sessions callback 之前）添加：

```python
# Model panel button callbacks
app.add_handler(
    CallbackQueryHandler(
        self._inject_deps(self._handle_model_callback),
        pattern=r"^model:",
    )
)
```

- [ ] **Step 2: 实现 _handle_model_callback 路由**

```python
async def _handle_model_callback(
    self, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle model:* callbacks for the model selection panel."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    pm = context.bot_data.get("provider_manager")
    if not pm:
        await query.edit_message_text("Provider 管理器不可用。")
        return

    data = query.data  # "model:<action>[:params...]"
    parts = data.split(":", 2)  # ["model", "action", "rest..."]

    if len(parts) < 2:
        return

    action = parts[1]
    rest = parts[2] if len(parts) > 2 else ""

    # Route by action
    if action == "detail":
        if rest:
            await self._show_model_detail(pm, query, context, user_id, model_name=rest)
    elif action == "toggle":
        await self._handle_model_toggle(query, user_id, rest)
    elif action == "1m":
        await self._handle_model_1m_toggle(query, user_id, rest)
    elif action == "save":
        await self._handle_model_save(pm, query, context, user_id, rest)
    elif action == "list" or action == "page":
        page = int(rest) if rest else 0
        await self._show_model_list(query, context, pm, user_id, page=page)
    elif action == "search":
        await self._enter_model_search(query, user_id)
    elif action == "search_cancel":
        await self._show_model_list(query, context, pm, user_id, page=0, search=None)
    elif action == "filter":
        await self._show_model_list(query, context, pm, user_id, page=0, search=rest)
    elif action == "noop":
        pass  # Page indicator button
```

- [ ] **Step 3: 验证编译**

```bash
.\.venv\Scripts\python.exe -m py_compile src/bot/orchestrator.py
```
预期：Compiled successfully（方法签名引用了尚未实现的方法，但编译检查通过）。

- [ ] **Step 4: 提交**

```bash
git add src/bot/orchestrator.py
git commit -m "feat(orchestrator): register model callback handler with routing

Handles model:list, model:page, model:detail, model:search, model:search_cancel,
model:noop, model:toggle, model:1m, model:save, model:filter.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 7: orchestrator — 模型详情页（角色勾选 + 1M 开关 + 保存）

**文件:**
- 修改：`src/bot/orchestrator.py`

**接口:**
- 消费：`_model_panel_state`、`_chunk_buttons`（已有）、`ProviderManager.get_role_models()`、`ProviderManager.set_default_model()`（Task 1）、`ProviderManager.set_role_model()`
- 产出：`_show_model_detail()`、`_handle_model_toggle()`、`_handle_model_1m_toggle()`、`_handle_model_save()`、`_build_model_detail_markup()`

- [ ] **Step 1: 添加 _build_model_detail_markup 方法**

```python
ROLES = ("default", "opus", "sonnet", "haiku")
ROLE_LABELS = {"default": "默认", "opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku"}

def _build_model_detail_markup(
    self, model_name: str, state: Dict[str, Any]
) -> InlineKeyboardMarkup:
    """Build inline keyboard for model detail view with role checkboxes + 1M toggles."""
    rows: List[List[InlineKeyboardButton]] = []

    for role in self.ROLES:
        # Role checkbox
        role_checked = state["roles"].get(role, False)
        role_icon = "✅" if role_checked else "◽"
        role_btn = InlineKeyboardButton(
            f"{role_icon} {self.ROLE_LABELS[role]}",
            callback_data=f"model:toggle:{model_name}:{role}",
        )

        # 1M toggle
        one_m_checked = state["context_1m"].get(role, False)
        one_m_icon = "✅" if one_m_checked else "◽"
        one_m_btn = InlineKeyboardButton(
            f"1M {one_m_icon}",
            callback_data=f"model:1m:{model_name}:{role}",
        )

        rows.append([role_btn, one_m_btn])

    # Bottom row: save + back
    rows.append([
        InlineKeyboardButton(
            "💾 保存",
            callback_data=f"model:save:{model_name}",
        ),
        InlineKeyboardButton(
            "📋 返回列表",
            callback_data="model:list:0",
        ),
    ])

    return InlineKeyboardMarkup(rows)
```

- [ ] **Step 2: 添加 _show_model_detail 方法**

```python
async def _show_model_detail(
    self,
    pm: Any,
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    model_name: str,
) -> None:
    """Show the model detail page with role assignment checkboxes and 1M toggles."""
    # Build initial state based on current profile settings
    current_default = pm.get_effective_model() or ""
    default_clean, _ = _parse_context_suffix(current_default)

    role_models = pm.get_role_models()
    roles_state = {}
    context_1m_state = {}

    for role in self.ROLES:
        if role == "default":
            assigned_model = default_clean
            assigned_raw = pm.get_effective_model() or ""
        else:
            assigned_raw = role_models.get(role) or ""
            assigned_model, _ = _parse_context_suffix(assigned_raw) if assigned_raw else ("", 200_000)

        is_active = (assigned_model == model_name)
        roles_state[role] = is_active

        # Check if 1M is enabled for this role's current assignment
        _, win = _parse_context_suffix(assigned_raw) if assigned_raw else ("", 200_000)
        context_1m_state[role] = (win >= 1_000_000 and is_active)

    self._model_panel_state[user_id] = {
        "active_model": model_name,
        "roles": roles_state,
        "context_1m": context_1m_state,
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
    }

    text = f"⚙️ {model_name}\n━━━━━━━━━━━━━━━━"
    markup = self._build_model_detail_markup(model_name, self._model_panel_state[user_id])

    try:
        await query.edit_message_text(text, reply_markup=markup)
    except Exception:
        await query.answer("面板已过期，请重新 /model", show_alert=True)
```

- [ ] **Step 3: 添加 _handle_model_toggle**

```python
async def _handle_model_toggle(
    self, query: CallbackQuery, user_id: int, rest: str
) -> None:
    """Flip a role checkbox in model detail view."""
    # rest format: "<model_name>:<role>"
    parts = rest.rsplit(":", 1)
    if len(parts) != 2:
        return
    model_name, role = parts

    state = self._model_panel_state.get(user_id)
    if not state or state.get("active_model") != model_name:
        await query.answer("此面板已过期，请重新 /model", show_alert=True)
        return

    if role not in self.ROLES:
        return

    state["roles"][role] = not state["roles"][role]

    markup = self._build_model_detail_markup(model_name, state)
    try:
        await query.edit_message_reply_markup(reply_markup=markup)
    except Exception:
        pass
```

- [ ] **Step 4: 添加 _handle_model_1m_toggle**

```python
async def _handle_model_1m_toggle(
    self, query: CallbackQuery, user_id: int, rest: str
) -> None:
    """Flip a 1M context window toggle in model detail view."""
    # rest format: "<model_name>:<role>"
    parts = rest.rsplit(":", 1)
    if len(parts) != 2:
        return
    model_name, role = parts

    state = self._model_panel_state.get(user_id)
    if not state or state.get("active_model") != model_name:
        await query.answer("此面板已过期，请重新 /model", show_alert=True)
        return

    if role not in self.ROLES:
        return

    state["context_1m"][role] = not state["context_1m"][role]

    markup = self._build_model_detail_markup(model_name, state)
    try:
        await query.edit_message_reply_markup(reply_markup=markup)
    except Exception:
        pass
```

- [ ] **Step 5: 添加 _handle_model_save**

```python
async def _handle_model_save(
    self,
    pm: Any,
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    rest: str,
) -> None:
    """Save role assignments and 1M settings from model detail view."""
    # rest = model_name
    state = self._model_panel_state.get(user_id)
    if not state or state.get("active_model") != rest:
        await query.answer("此面板已过期，请重新 /model", show_alert=True)
        return

    active = pm.get_active()
    if not active:
        await query.answer("Provider 配置异常，请检查", show_alert=True)
        return

    model_name = state["active_model"]

    for role in self.ROLES:
        if not state["roles"].get(role, False):
            continue

        suffix = "[1m]" if state["context_1m"].get(role, False) else ""
        full_name = f"{model_name}{suffix}"

        if role == "default":
            pm.set_default_model(full_name)
        else:
            pm.set_role_model(role, full_name)

    # Return to list page
    await self._show_model_list(query, context, pm, user_id, page=0)

    # Toast: what was set
    summary_parts = []
    for role in self.ROLES:
        if state["roles"].get(role, False):
            suffix = " [1M]" if state["context_1m"].get(role, False) else ""
            summary_parts.append(f"{self.ROLE_LABELS[role]}={model_name}{suffix}")
    await query.answer(f"已保存: {', '.join(summary_parts)}", show_alert=False)

    # Clean up panel state
    self._model_panel_state.pop(user_id, None)
```

- [ ] **Step 6: 验证编译**

```bash
.\.venv\Scripts\python.exe -m py_compile src/bot/orchestrator.py
```
预期：Compiled successfully。

- [ ] **Step 7: 提交**

```bash
git add src/bot/orchestrator.py
git commit -m "feat(orchestrator): add model detail panel with role checkboxes and 1M toggles

Model detail view shows checkboxes (✅/◽) for each role and independent
1M context window toggles. Save writes to ProviderManager and returns to
list. Toggle-only updates use edit_message_reply_markup for zero-flicker UX.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 8: orchestrator — 搜索模式（按钮入口 + 文本拦截）

**文件:**
- 修改：`src/bot/orchestrator.py`

**接口:**
- 消费：`_model_panel_state`、`_show_model_list()`（Task 5）
- 产出：`_enter_model_search()`、搜索文本拦截逻辑（在 `agentic_text` 中增加检查）

- [ ] **Step 1: 添加 _enter_model_search 方法**

```python
async def _enter_model_search(
    self, query: CallbackQuery, user_id: int
) -> None:
    """Enter search mode: prompt user to type a search keyword."""
    text = (
        "📋 模型列表  🔍 搜索中...\n"
        "━━━━━━━━━━━━━━━━\n"
        "请在聊天框中输入搜索关键词（如 deepseek），\n"
        "我会过滤匹配的模型。\n\n"
        "（回复此消息的关键词会被拦截为搜索词）\n"
        "━━━━━━━━━━━━━━━━"
    )
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ 取消搜索", callback_data="model:search_cancel"),
    ]])

    self._model_panel_state[user_id] = {
        "search": "",  # non-None string = in search mode
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
    }

    try:
        await query.edit_message_text(text, reply_markup=markup)
    except Exception:
        await query.answer("面板已过期，请重新 /model", show_alert=True)
```

- [ ] **Step 2: 在 agentic_text 中添加搜索文本拦截**

在 `agentic_text` 方法的 `session_edit` 检查和 `auq_other` 检查之间添加：

```python
# Check if user is in model search mode
search_state = self._model_panel_state.get(user_id)
if search_state and search_state.get("search") is not None:
    # Intercept text as search keyword
    keyword = message_text.strip()
    if not keyword:
        return  # empty message, stay in search mode
    # Execute search: treat as filter callback
    pm = context.bot_data.get("provider_manager")
    if pm:
        # Use edit_message on the existing panel message
        search_state["search"] = keyword
        self._model_panel_state[user_id] = search_state
        # Manually trigger the filter action
        models = await pm.fetch_models()
        if models:
            text = self._build_panel_status_header(pm)
            markup = self._build_model_list_markup(models, page=0, search=keyword)
            try:
                await context.bot.edit_message_text(
                    chat_id=search_state["chat_id"],
                    message_id=search_state["message_id"],
                    text=text,
                    reply_markup=markup,
                )
            except Exception:
                pass  # panel expired, ignore
    await update.message.delete()  # hide the search keyword from chat
    return
```

- [ ] **Step 3: 处理命令行搜索 `/model -s <keyword>` 和 `/model --search <keyword>`**

在 `agentic_model` 的参数解析中（`first = args[0].strip().lower()` 之前）添加：

```python
# Check for -s / --search flag
if len(args) >= 2 and args[0] in ("-s", "--search"):
    keyword = " ".join(args[1:])
    await self._show_model_list(update, context, pm, user_id=update.effective_user.id, search=keyword)
    return
```

- [ ] **Step 4: 验证编译**

```bash
.\.venv\Scripts\python.exe -m py_compile src/bot/orchestrator.py
```
预期：Compiled successfully。

- [ ] **Step 5: 提交**

```bash
git add src/bot/orchestrator.py
git commit -m "feat(orchestrator): add model search mode with text interception

Search button enters search mode; next text message is intercepted as
filter keyword. Also supports /model -s <keyword> and /model --search <keyword>.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 9: 集成测试 — 端到端验证

**文件:**
- 新增：`tests/unit/test_model_panel.py`

- [ ] **Step 1: 编写 ProviderManager 单元测试（mock HTTP）**

```python
"""Tests for model panel: fetch_models and data model."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.config.providers import ProviderManager, _parse_models_response


# ── Reuse _seed pattern from test_providers.py ──

def _seed(tmp_path: Path, profiles: dict, active: str):
    storage = tmp_path / "providers.json"
    storage.write_text(
        json.dumps({"active": active, "profiles": profiles}),
        encoding="utf-8",
    )
    config = SimpleNamespace(claude_model=None, anthropic_api_key_str=None)
    return ProviderManager(config, storage_path=storage)


_FULL = {
    "name": "cpa",
    "base_url": "https://cpa.example",
    "auth_token": "tok-cpa",
    "api_key": "sk-test",
    "default_model": "mimo[1m]",
    "opus_model": "mimo[1m]",
    "sonnet_model": "mimo[1m]",
    "haiku_model": "mimo[1m]",
}


class TestFetchModels:
    async def test_fetch_models_returns_sorted_deduped_ids(self, tmp_path):
        pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.json = AsyncMock(return_value={
            "data": [
                {"id": "z-model"}, {"id": "a-model"}, {"id": "z-model"}
            ]
        })

        with patch("aiohttp.ClientSession.get", return_value=mock_resp):
            result = await pm.fetch_models()

        assert result == ["a-model", "z-model"]

    async def test_fetch_models_empty_on_500(self, tmp_path):
        pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")

        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession.get", return_value=mock_resp):
            result = await pm.fetch_models()

        assert result == []

    async def test_fetch_models_empty_when_no_base_url(self, tmp_path):
        sparse = {**_FULL, "base_url": None, "name": "direct"}
        pm = _seed(tmp_path, {"direct": sparse}, active="direct")
        result = await pm.fetch_models()
        assert result == []


class TestSetDefaultModel:
    def test_set_default_model_writes_to_active_profile(self, tmp_path):
        pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")
        pm.set_default_model("new-model[1m]")

        raw = json.loads(pm._storage_path.read_text(encoding="utf-8"))
        assert "model_override" not in raw
        assert raw["profiles"]["cpa"]["default_model"] == "new-model[1m]"

    def test_set_default_model_none_clears(self, tmp_path):
        pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")
        pm.set_default_model(None)

        assert pm.get_active().default_model is None

    def test_get_effective_model_falls_back_to_config(self, tmp_path):
        sparse = {**_FULL, "default_model": None}
        config = SimpleNamespace(claude_model="from-config[1m]", anthropic_api_key_str=None)
        pm = _seed(tmp_path, {"cpa": sparse}, active="cpa")
        pm._config = config

        assert pm.get_effective_model() == "from-config[1m]"

    def test_get_model_source_no_override(self, tmp_path):
        pm = _seed(tmp_path, {"cpa": _FULL}, active="cpa")
        assert pm.get_model_source() == "profile"

        sparse = {**_FULL, "default_model": None}
        pm2 = _seed(tmp_path, {"cpa": sparse}, active="cpa")
        assert pm2.get_model_source() == "settings"
```

- [ ] **Step 2: 运行测试**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_model_panel.py -v
```
预期：全部 PASS。

- [ ] **Step 3: 运行所有相关测试确认无回归**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_providers.py tests/unit/test_model_panel.py -v
```
预期：全部 PASS。

- [ ] **Step 4: 提交**

```bash
git add tests/unit/test_model_panel.py
git commit -m "test: add unit tests for fetch_models and set_default_model

Tests cover: fetch_models parsing, error handling, set_default_model
persistence, get_effective_model fallback chain.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 10: 编译全量验证 + 清理

- [ ] **Step 1: 全量编译**

```bash
.\.venv\Scripts\python.exe -m py_compile src/config/providers.py src/bot/orchestrator.py src/bot/handlers/command.py
```
预期：全部 Compiled successfully。

- [ ] **Step 2: 全量测试**

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit -q
```
预期：全部 PASS，无回归。

- [ ] **Step 3: 搜索残留的 model_override / set_model_override 引用**

```bash
rg "model_override|set_model_override" src/ tests/ --files-with-matches
```
预期：只在 `src/config/providers.py` 的 docstring 或注释中可能残留（如有则修复），以及在 `src/bot/orchestrator.py` 的 model_test 类定义中可能有（无关紧要的 class name）。如果 `data/providers.json` 仍有旧字段则已在 Task 1 的 `_load` 中处理（不再读取该字段，下次 save 时不保留）。

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "chore: final cleanup for model panel implementation

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```
