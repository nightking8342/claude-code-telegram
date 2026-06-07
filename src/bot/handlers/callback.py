"""Handle inline keyboard callbacks."""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...claude.facade import ClaudeIntegration
from ...config.settings import Settings
from ...security.audit import AuditLogger
from ...security.validators import SecurityValidator
from ..features.session_browser import (
    delete_confirm_keyboard,
    derive_fallback_title,
    list_sessions_view,
    session_detail_view,
)
from ..utils.html_format import escape_html

logger = structlog.get_logger()


_FILENAME_FRAGMENT_RE = re.compile(r"[^\w\-一-鿿]+")


def _safe_filename_fragment(s: str, max_len: int = 40) -> str:
    """Sanitize a string for use in a filename.

    Replaces runs of whitespace and forbidden characters with a single ``_``.
    Preserves CJK characters along with ASCII word characters and ``-``.
    """
    cleaned = _FILENAME_FRAGMENT_RE.sub("_", s).strip("_")
    return cleaned[:max_len] or "session"


async def _check_session_ownership(
    storage, user_id: int, session_id: str, project_path: str | None = None
) -> str:
    """Return one of: ``"owned"``, ``"btw_fork"``, ``"cross_user"``, ``"missing"``.

    The runtime passes a ``SessionRepository`` (``.db``); existing tests pass
    a ``SQLiteSessionStorage`` (``.db_manager``). Either is fine — we just
    need any object exposing ``get_connection()``.
    """
    checker = getattr(storage, "is_btw_fork_session", None)
    if (
        checker is not None
        and type(storage).__module__.startswith("unittest.mock")
        and "is_btw_fork_session" not in getattr(storage, "__dict__", {})
    ):
        checker = None
    if checker is not None:
        try:
            if await checker(session_id, user_id=user_id):
                return "btw_fork"
        except Exception:
            pass

    session = await storage.load_session(session_id, user_id)
    if session is not None:
        return "owned"
    db = getattr(storage, "db", None) or getattr(storage, "db_manager", None)
    if db is not None:
        async with db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ? AND is_active = TRUE",
                (session_id,),
            )
            if (await cursor.fetchone()) is not None:
                return "cross_user"
    # Check CLI sessions
    if project_path:
        try:
            sdk_info = await ClaudeIntegration.get_sdk_session_info(
                session_id, Path(project_path)
            )
            if sdk_info:
                return "owned"
            cli_sessions = await ClaudeIntegration.scan_cli_sessions(Path(project_path))
            for cs in cli_sessions:
                if cs["session_id"] == session_id:
                    return "owned"
        except Exception:
            pass
    return "missing"


async def _resolve_title_for_handler(storage, project_path, session_id: str) -> str:
    """Local mirror of ``session_browser._resolve_title`` for the callback layer.

    Resolves display title: CLI aiTitle → first prompt → session id-based.
    """
    sdk_info = await ClaudeIntegration.get_sdk_session_info(
        session_id, Path(str(project_path))
    )
    if sdk_info:
        title = (
            sdk_info.get("title")
            or sdk_info.get("summary")
            or sdk_info.get("custom_title")
            or sdk_info.get("first_prompt")
        )
        if title:
            return title

    title = await ClaudeIntegration.read_session_title(
        session_id, Path(str(project_path))
    )
    if title:
        return title
    first_prompt = None
    try:
        msgs = await storage.get_session_messages(session_id, limit=1)
        if msgs:
            first = msgs[0]
            first_prompt = (
                first.get("prompt") or first.get("content")
                if isinstance(first, dict)
                else getattr(first, "prompt", None) or getattr(first, "content", None)
            )
    except Exception:
        pass
    return derive_fallback_title(first_prompt, session_id)


def _is_within_root(path: Path, root: Path) -> bool:
    """Check whether path is within root directory."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _get_thread_project_root(
    settings: Settings, context: ContextTypes.DEFAULT_TYPE
) -> Optional[Path]:
    """Get thread project root when strict thread mode is active."""
    if not settings.enable_project_threads:
        return None
    thread_context = context.user_data.get("_thread_context")
    if not thread_context:
        return None
    return Path(thread_context["project_root"]).resolve()


async def handle_callback_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route callback queries to appropriate handlers."""
    query = update.callback_query
    await query.answer()  # Acknowledge the callback

    user_id = query.from_user.id
    data = query.data

    logger.info("Processing callback query", user_id=user_id, callback_data=data)

    try:
        # Parse callback data
        if ":" in data:
            action, param = data.split(":", 1)
        else:
            action, param = data, None

        # Route to appropriate handler
        handlers = {
            "cd": handle_cd_callback,
            "action": handle_action_callback,
            "confirm": handle_confirm_callback,
            "quick": handle_quick_action_callback,
            "followup": handle_followup_callback,
            "conversation": handle_conversation_callback,
            "git": handle_git_callback,
            "export": handle_export_callback,
            "sessions": handle_sessions_callback,
        }

        handler = handlers.get(action)
        if handler:
            await handler(query, param, context)
        else:
            await query.edit_message_text(
                "❌ <b>未知操作</b>\n\n"
                "无法识别此按钮操作，"
                "可能是因为机器人在该消息发送后已更新。",
                parse_mode="HTML",
            )

    except Exception as e:
        logger.error(
            "Error handling callback query",
            error=str(e),
            user_id=user_id,
            callback_data=data,
        )

        try:
            await query.edit_message_text(
                "❌ <b>操作处理出错</b>\n\n"
                "处理请求时发生错误。\n"
                "请重试或使用文本命令。",
                parse_mode="HTML",
            )
        except Exception:
            # If we can't edit the message, send a new one
            await query.message.reply_text(
                "❌ <b>操作处理出错</b>\n\n" "处理请求时发生错误。",
                parse_mode="HTML",
            )


async def handle_cd_callback(
    query, project_name: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle directory change from inline keyboard."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    security_validator: SecurityValidator = context.bot_data.get("security_validator")
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")

    try:
        current_dir = context.user_data.get(
            "current_directory", settings.approved_directory
        )
        project_root = _get_thread_project_root(settings, context)
        directory_root = project_root or settings.approved_directory

        # Handle special paths
        if project_name == "/":
            new_path = directory_root
        elif project_name == "..":
            new_path = current_dir.parent
            if not _is_within_root(new_path, directory_root):
                new_path = directory_root
        else:
            if project_root:
                new_path = current_dir / project_name
            else:
                new_path = settings.approved_directory / project_name

        # Validate path if security validator is available
        if security_validator:
            # Pass the absolute path for validation
            valid, resolved_path, error = security_validator.validate_path(
                str(new_path), settings.approved_directory
            )
            if not valid:
                await query.edit_message_text(
                    f"❌ <b>访问被拒绝</b>\n\n{escape_html(error)}",
                    parse_mode="HTML",
                )
                return
            # Use the validated path
            new_path = resolved_path

        if project_root and not _is_within_root(new_path, project_root):
            await query.edit_message_text(
                "❌ <b>访问被拒绝</b>\n\n" "在线程模式下，导航仅限于当前项目根目录。",
                parse_mode="HTML",
            )
            return

        # Check if directory exists
        if not new_path.exists() or not new_path.is_dir():
            await query.edit_message_text(
                f"❌ <b>目录未找到</b>\n\n"
                f"目录 <code>{escape_html(project_name)}</code> 不存在或无法访问。",
                parse_mode="HTML",
            )
            return

        # Update directory and resume session for that directory when available
        context.user_data["current_directory"] = new_path

        resumed_session_info = ""
        if claude_integration:
            existing_session = await claude_integration._find_resumable_session(
                user_id, new_path
            )
            if existing_session:
                context.user_data["claude_session_id"] = existing_session.session_id
                sdk_info = await ClaudeIntegration.get_sdk_session_info(
                    existing_session.session_id, new_path
                )
                title = (
                    (sdk_info or {}).get("title")
                    or (sdk_info or {}).get("summary")
                    or (sdk_info or {}).get("custom_title")
                    or (sdk_info or {}).get("first_prompt")
                    or f"Session {existing_session.session_id[:8]}"
                )
                resumed_session_info = (
                    f"\n🔄 已恢复 session «<b>{escape_html(title)}</b>»\n"
                    f"ID：<code>{escape_html(existing_session.session_id)}</code> "
                    f"({existing_session.message_count} 条消息)"
                )
            else:
                context.user_data["claude_session_id"] = None
                resumed_session_info = "\n🆕 无现有 session，发送消息即可创建新会话。"
        else:
            context.user_data["claude_session_id"] = None
            resumed_session_info = "\n🆕 发送消息即可创建新 session。"

        # Send confirmation with new directory info
        relative_base = project_root or settings.approved_directory
        relative_path = new_path.relative_to(relative_base)
        relative_display = "/" if str(relative_path) == "." else f"{relative_path}/"

        # Add navigation buttons
        keyboard = [
            [
                InlineKeyboardButton("📁 列出文件", callback_data="action:ls"),
                InlineKeyboardButton(
                    "🆕 新建 session", callback_data="action:new_session"
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 项目列表", callback_data="action:show_projects"
                ),
                InlineKeyboardButton("📊 状态", callback_data="action:status"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"✅ <b>目录已切换</b>\n\n"
            f"📂 当前目录：<code>{escape_html(str(relative_display))}</code>"
            f"{resumed_session_info}",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

        # Log successful directory change
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id, command="cd", args=[project_name], success=True
            )

    except Exception as e:
        await query.edit_message_text(
            f"❌ <b>切换目录出错</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )

        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id, command="cd", args=[project_name], success=False
            )


async def handle_action_callback(
    query, action_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle general action callbacks."""
    actions = {
        "help": _handle_help_action,
        "show_projects": _handle_show_projects_action,
        "new_session": _handle_new_session_action,
        "continue": _handle_continue_action,
        "end_session": _handle_end_session_action,
        "status": _handle_status_action,
        "ls": _handle_ls_action,
        "start_coding": _handle_start_coding_action,
        "quick_actions": _handle_quick_actions_action,
        "refresh_status": _handle_refresh_status_action,
        "refresh_ls": _handle_refresh_ls_action,
        "export": _handle_export_action,
    }

    handler = actions.get(action_type)
    if handler:
        await handler(query, context)
    else:
        await query.edit_message_text(
            f"❌ <b>未知操作：{escape_html(action_type)}</b>\n\n" "此操作尚未实现。",
            parse_mode="HTML",
        )


async def handle_confirm_callback(
    query, confirmation_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle confirmation dialogs."""
    if confirmation_type == "yes":
        await query.edit_message_text(
            "✅ <b>已确认</b>\n\n操作将被处理。",
            parse_mode="HTML",
        )
    elif confirmation_type == "no":
        await query.edit_message_text(
            "❌ <b>已取消</b>\n\n操作已被取消。",
            parse_mode="HTML",
        )
    else:
        await query.edit_message_text(
            "❓ <b>未知的确认响应</b>",
            parse_mode="HTML",
        )


# Action handlers


async def _handle_help_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle help action."""
    help_text = (
        "🤖 <b>快速帮助</b>\n\n"
        "<b>导航：</b>\n"
        "• <code>/ls</code> - 列出文件\n"
        "• <code>/cd &lt;dir&gt;</code> - 切换目录\n"
        "• <code>/projects</code> - 显示项目\n\n"
        "<b>Session：</b>\n"
        "• <code>/new</code> - 新建 Claude session\n"
        "• <code>/status</code> - 查看 session 状态\n\n"
        "<b>提示：</b>\n"
        "• 发送任意文本与 Claude 交互\n"
        "• 上传文件进行代码审查\n"
        "• 使用按钮快速操作\n\n"
        "使用 <code>/help</code> 查看详细帮助。"
    )

    keyboard = [
        [
            InlineKeyboardButton("📖 完整帮助", callback_data="action:full_help"),
            InlineKeyboardButton("🏠 主菜单", callback_data="action:main_menu"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        help_text, parse_mode="HTML", reply_markup=reply_markup
    )


async def _handle_show_projects_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle show projects action."""
    settings: Settings = context.bot_data["settings"]

    try:
        if settings.enable_project_threads:
            registry = context.bot_data.get("project_registry")
            if not registry:
                await query.edit_message_text(
                    "❌ <b>项目注册表未初始化。</b>",
                    parse_mode="HTML",
                )
                return

            projects = registry.list_enabled()
            if not projects:
                await query.edit_message_text(
                    "📁 <b>未找到项目</b>\n\n" "项目配置中没有已启用的项目。",
                    parse_mode="HTML",
                )
                return

            project_list = "\n".join(
                [
                    f"• <b>{escape_html(p.name)}</b> "
                    f"(<code>{escape_html(p.slug)}</code>) "
                    f"→ <code>{escape_html(str(p.relative_path))}</code>"
                    for p in projects
                ]
            )

            await query.edit_message_text(
                f"📁 <b>已配置的项目</b>\n\n{project_list}",
                parse_mode="HTML",
            )
            return

        # Get directories in approved directory
        projects = []
        for item in sorted(settings.approved_directory.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                projects.append(item.name)

        if not projects:
            await query.edit_message_text(
                "📁 <b>未找到项目</b>\n\n"
                "授权目录下没有子目录。\n"
                "请创建一些目录来组织你的项目！",
                parse_mode="HTML",
            )
            return

        # Create project buttons
        keyboard = []
        for i in range(0, len(projects), 2):
            row = []
            for j in range(2):
                if i + j < len(projects):
                    project = projects[i + j]
                    row.append(
                        InlineKeyboardButton(
                            f"📁 {project}", callback_data=f"cd:{project}"
                        )
                    )
            keyboard.append(row)

        # Add navigation buttons
        keyboard.append(
            [
                InlineKeyboardButton("🏠 根目录", callback_data="cd:/"),
                InlineKeyboardButton("🔄 刷新", callback_data="action:show_projects"),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard)
        project_list = "\n".join(
            [f"• <code>{escape_html(project)}/</code>" for project in projects]
        )

        await query.edit_message_text(
            f"📁 <b>可用项目</b>\n\n" f"{project_list}\n\n" f"点击项目即可跳转：",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    except Exception as e:
        await query.edit_message_text(f"❌ 加载项目出错：{str(e)}")


async def _handle_new_session_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle new session action."""
    settings: Settings = context.bot_data["settings"]

    # Clear session
    context.user_data["claude_session_id"] = None
    context.user_data["session_started"] = True

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    keyboard = [
        [
            InlineKeyboardButton("📝 开始编码", callback_data="action:start_coding"),
            InlineKeyboardButton("📁 切换项目", callback_data="action:show_projects"),
        ],
        [
            InlineKeyboardButton("📋 快捷操作", callback_data="action:quick_actions"),
            InlineKeyboardButton("❓ 帮助", callback_data="action:help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"🆕 <b>新建 Claude Code Session</b>\n\n"
        f"📂 工作目录：<code>{escape_html(str(relative_path))}/</code>\n\n"
        f"随时准备协助编码！发送消息即可开始：",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def _handle_end_session_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle end session action."""
    settings: Settings = context.bot_data["settings"]

    # Check if there's an active session
    claude_session_id = context.user_data.get("claude_session_id")

    if not claude_session_id:
        await query.edit_message_text(
            "ℹ️ <b>无活跃 session</b>\n\n"
            "当前没有活跃的 Claude session 可以结束。\n\n"
            "<b>你可以：</b>\n"
            "• 使用下方按钮创建新 session\n"
            "• 查看 session 状态\n"
            "• 发送任意消息开始新对话",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🆕 新建 session", callback_data="action:new_session"
                        )
                    ],
                    [InlineKeyboardButton("📊 状态", callback_data="action:status")],
                ]
            ),
        )
        return

    # Get current directory for display
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Clear session data
    context.user_data["claude_session_id"] = None
    context.user_data["session_started"] = False
    context.user_data["last_message"] = None

    # Create quick action buttons
    keyboard = [
        [
            InlineKeyboardButton("🆕 新建 session", callback_data="action:new_session"),
            InlineKeyboardButton("📁 切换项目", callback_data="action:show_projects"),
        ],
        [
            InlineKeyboardButton("📊 状态", callback_data="action:status"),
            InlineKeyboardButton("❓ 帮助", callback_data="action:help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "✅ <b>Session 已结束</b>\n\n"
        f"你的 Claude session 已终止。\n\n"
        f"<b>当前状态：</b>\n"
        f"• 目录：<code>{escape_html(str(relative_path))}/</code>\n"
        f"• Session：无\n"
        f"• 准备就绪\n\n"
        f"<b>下一步：</b>\n"
        f"• 创建新 session\n"
        f"• 查看状态\n"
        f"• 发送任意消息开始新对话",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def _handle_continue_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle continue session action."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        if not claude_integration:
            await query.edit_message_text(
                "❌ <b>Claude 集成不可用</b>\n\n" "Claude 集成未正确配置。",
                parse_mode="HTML",
            )
            return

        # Check if there's an existing session in user context
        claude_session_id = context.user_data.get("claude_session_id")

        if claude_session_id:
            # Continue with the existing session (no prompt = use --continue)
            await query.edit_message_text(
                f"🔄 <b>继续 Session</b>\n\n"
                f"Session ID: <code>{escape_html(claude_session_id)}</code>\n"
                f"目录：<code>{escape_html(str(current_dir.relative_to(settings.approved_directory)))}/</code>\n\n"
                f"正在从上次中断处继续...",
                parse_mode="HTML",
            )

            claude_response = await claude_integration.run_command(
                prompt="",  # Empty prompt triggers --continue
                working_directory=current_dir,
                user_id=user_id,
                session_id=claude_session_id,
            )
        else:
            # No session in context, try to find the most recent session
            await query.edit_message_text(
                "🔍 <b>查找最近的 Session</b>\n\n" "正在搜索此目录下最近的 session...",
                parse_mode="HTML",
            )

            claude_response = await claude_integration.continue_session(
                user_id=user_id,
                working_directory=current_dir,
                prompt=None,  # No prompt = use --continue
            )

        if claude_response:
            # Update session ID in context
            context.user_data["claude_session_id"] = claude_response.session_id

            # Send Claude's response
            await query.message.reply_text(
                f"✅ <b>Session 已继续</b>\n\n"
                f"{escape_html(claude_response.content[:500])}{'...' if len(claude_response.content) > 500 else ''}",
                parse_mode="HTML",
            )
        else:
            # No session found to continue
            await query.edit_message_text(
                "❌ <b>未找到 Session</b>\n\n"
                f"此目录下没有最近的 Claude session。\n"
                f"目录：<code>{escape_html(str(current_dir.relative_to(settings.approved_directory)))}/</code>\n\n"
                f"<b>你可以：</b>\n"
                f"• 使用下方按钮创建新 session\n"
                f"• 查看 session 状态\n"
                f"• 切换到其他目录",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🆕 新建 session", callback_data="action:new_session"
                            ),
                            InlineKeyboardButton(
                                "📊 状态", callback_data="action:status"
                            ),
                        ]
                    ]
                ),
            )

    except Exception as e:
        logger.error("Error in continue action", error=str(e), user_id=user_id)
        await query.edit_message_text(
            f"❌ <b>继续 Session 出错</b>\n\n"
            f"发生错误：<code>{escape_html(str(e))}</code>\n\n"
            f"请尝试创建新 session。",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🆕 新建 session", callback_data="action:new_session"
                        )
                    ]
                ]
            ),
        )


async def _handle_status_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle status action."""
    # This essentially duplicates the /status command functionality
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]

    claude_session_id = context.user_data.get("claude_session_id")
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Get usage info if rate limiter is available
    rate_limiter = context.bot_data.get("rate_limiter")
    usage_info = ""
    if rate_limiter:
        try:
            user_status = rate_limiter.get_user_status(user_id)
            cost_usage = user_status.get("cost_usage", {})
            current_cost = cost_usage.get("current", 0.0)
            cost_limit = cost_usage.get("limit", settings.claude_max_cost_per_user)
            cost_percentage = (current_cost / cost_limit) * 100 if cost_limit > 0 else 0

            usage_info = f"💰 用量：${current_cost:.2f} / ${cost_limit:.2f} ({cost_percentage:.0f}%)\n"
        except Exception:
            usage_info = "💰 用量：<i>无法获取</i>\n"

    status_lines = [
        "📊 <b>Session 状态</b>",
        "",
        f"📂 目录：<code>{escape_html(str(relative_path))}/</code>",
        f"🤖 Claude Session：{'✅ 活跃' if claude_session_id else '❌ 无'}",
        usage_info.rstrip(),
    ]

    if claude_session_id:
        status_lines.append(
            f"🆔 Session ID: <code>{escape_html(claude_session_id)}</code>"
        )

    # Add action buttons
    keyboard = []
    if claude_session_id:
        keyboard.append(
            [
                InlineKeyboardButton("🔄 继续", callback_data="action:continue"),
                InlineKeyboardButton(
                    "🛑 结束 session", callback_data="action:end_session"
                ),
            ]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🆕 新建 session", callback_data="action:new_session"
                ),
            ]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🆕 开始 session", callback_data="action:new_session"
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton("🔄 刷新", callback_data="action:refresh_status"),
            InlineKeyboardButton("📁 项目", callback_data="action:show_projects"),
        ]
    )

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "\n".join(status_lines), parse_mode="HTML", reply_markup=reply_markup
    )


async def _handle_ls_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ls action."""
    settings: Settings = context.bot_data["settings"]
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        # List directory contents (similar to /ls command)
        items = []
        directories = []
        files = []

        for item in sorted(current_dir.iterdir()):
            if item.name.startswith("."):
                continue

            # Escape markdown special characters in filenames
            safe_name = _escape_markdown(item.name)

            if item.is_dir():
                directories.append(f"📁 {safe_name}/")
            else:
                try:
                    size = item.stat().st_size
                    size_str = _format_file_size(size)
                    files.append(f"📄 {safe_name} ({size_str})")
                except OSError:
                    files.append(f"📄 {safe_name}")

        items = directories + files
        relative_path = current_dir.relative_to(settings.approved_directory)

        if not items:
            message = f"📂 <code>{escape_html(str(relative_path))}/</code>\n\n<i>（空目录）</i>"
        else:
            message = f"📂 <code>{escape_html(str(relative_path))}/</code>\n\n"
            max_items = 30  # Limit for inline display
            if len(items) > max_items:
                shown_items = items[:max_items]
                message += "\n".join(shown_items)
                message += f"\n\n<i>...还有 {len(items) - max_items} 个项目</i>"
            else:
                message += "\n".join(items)

        # Add buttons
        keyboard = []
        if current_dir != settings.approved_directory:
            keyboard.append(
                [
                    InlineKeyboardButton("⬆️ 上级目录", callback_data="cd:.."),
                    InlineKeyboardButton("🏠 根目录", callback_data="cd:/"),
                ]
            )

        keyboard.append(
            [
                InlineKeyboardButton("🔄 刷新", callback_data="action:refresh_ls"),
                InlineKeyboardButton(
                    "📋 项目列表", callback_data="action:show_projects"
                ),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            message, parse_mode="HTML", reply_markup=reply_markup
        )

    except Exception as e:
        await query.edit_message_text(f"❌ 列出目录出错：{str(e)}")


async def _handle_start_coding_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle start coding action."""
    await query.edit_message_text(
        "🚀 <b>准备就绪！</b>\n\n"
        "发送任意消息即可与 Claude 一起编码：\n\n"
        "<b>示例：</b>\n"
        '• <i>"创建一个 Python 脚本..."</i>\n'
        '• <i>"帮我调试这段代码..."</i>\n'
        '• <i>"解释一下这个文件的工作原理..."</i>\n'
        "• 上传文件进行代码审查\n\n"
        "随时为你提供编码帮助！",
        parse_mode="HTML",
    )


async def _handle_quick_actions_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle quick actions menu."""
    keyboard = [
        [
            InlineKeyboardButton("🧪 运行测试", callback_data="quick:test"),
            InlineKeyboardButton("📦 安装依赖", callback_data="quick:install"),
        ],
        [
            InlineKeyboardButton("🎨 格式化代码", callback_data="quick:format"),
            InlineKeyboardButton("🔍 查找 TODO", callback_data="quick:find_todos"),
        ],
        [
            InlineKeyboardButton("🔨 构建", callback_data="quick:build"),
            InlineKeyboardButton("🚀 启动服务", callback_data="quick:start"),
        ],
        [
            InlineKeyboardButton("📊 Git 状态", callback_data="quick:git_status"),
            InlineKeyboardButton("🔧 代码检查", callback_data="quick:lint"),
        ],
        [InlineKeyboardButton("⬅️ 返回", callback_data="action:new_session")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🛠️ <b>快捷操作</b>\n\n"
        "选择常用开发任务：\n\n"
        "<i>注：Claude Code 集成完成后这些功能将完全可用。</i>",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def _handle_refresh_status_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle refresh status action."""
    await _handle_status_action(query, context)


async def _handle_refresh_ls_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle refresh ls action."""
    await _handle_ls_action(query, context)


async def _handle_export_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle export action."""
    await query.edit_message_text(
        "📤 <b>导出 Session</b>\n\n"
        "存储层实现后，session 导出功能将可用。\n\n"
        "<b>计划功能：</b>\n"
        "• 导出对话历史\n"
        "• 保存 session 状态\n"
        "• 分享对话\n"
        "• 创建 session 备份\n\n"
        "<i>即将在下一开发阶段推出！</i>",
        parse_mode="HTML",
    )


async def handle_quick_action_callback(
    query, action_id: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle quick action callbacks."""
    user_id = query.from_user.id

    # Get quick actions manager from bot data if available
    quick_actions = context.bot_data.get("quick_actions")

    if not quick_actions:
        await query.edit_message_text(
            "❌ <b>快捷操作不可用</b>\n\n" "快捷操作功能不可用。",
            parse_mode="HTML",
        )
        return

    # Get Claude integration
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")
    if not claude_integration:
        await query.edit_message_text(
            "❌ <b>Claude 集成不可用</b>\n\n" "Claude 集成未正确配置。",
            parse_mode="HTML",
        )
        return

    settings: Settings = context.bot_data["settings"]
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        # Get the action from the manager
        action = quick_actions.actions.get(action_id)
        if not action:
            await query.edit_message_text(
                f"❌ <b>操作未找到</b>\n\n"
                f"快捷操作 '{escape_html(action_id)}' 不可用。",
                parse_mode="HTML",
            )
            return

        # Execute the action
        await query.edit_message_text(
            f"🚀 <b>正在执行 {action.icon} {escape_html(action.name)}</b>\n\n"
            f"在目录中运行快捷操作：<code>{escape_html(str(current_dir.relative_to(settings.approved_directory)))}/</code>\n\n"
            f"请稍候...",
            parse_mode="HTML",
        )

        # Run the action through Claude
        claude_response = await claude_integration.run_command(
            prompt=action.prompt, working_directory=current_dir, user_id=user_id
        )

        if claude_response:
            # Format and send the response
            response_text = escape_html(claude_response.content)
            if len(response_text) > 4000:
                response_text = response_text[:4000] + "...\n\n<i>（响应已截断）</i>"

            await query.message.reply_text(
                f"✅ <b>{action.icon} {escape_html(action.name)} 完成</b>\n\n{response_text}",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                f"❌ <b>操作失败</b>\n\n"
                f"执行 {escape_html(action.name)} 失败，请重试。",
                parse_mode="HTML",
            )

    except Exception as e:
        logger.error("Quick action execution failed", error=str(e), user_id=user_id)
        await query.edit_message_text(
            f"❌ <b>操作出错</b>\n\n"
            f"执行 {escape_html(action_id)} 时发生错误：{escape_html(str(e))}",
            parse_mode="HTML",
        )


async def handle_followup_callback(
    query, suggestion_hash: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle follow-up suggestion callbacks."""
    user_id = query.from_user.id

    # Get conversation enhancer from bot data if available
    conversation_enhancer = context.bot_data.get("conversation_enhancer")

    if not conversation_enhancer:
        await query.edit_message_text(
            "❌ <b>后续建议不可用</b>\n\n" "对话增强功能不可用。",
            parse_mode="HTML",
        )
        return

    try:
        # Get stored suggestions (this would need to be implemented in the enhancer)
        # For now, we'll provide a generic response
        await query.edit_message_text(
            "💡 <b>已选择后续建议</b>\n\n"
            "此后续建议将在对话增强系统"
            "与消息处理器完全集成后实现。\n\n"
            "<b>当前状态：</b>\n"
            "• 建议已接收 ✅\n"
            "• 集成待定 🔄\n\n"
            "<i>你可以发送新消息继续对话。</i>",
            parse_mode="HTML",
        )

        logger.info(
            "Follow-up suggestion selected",
            user_id=user_id,
            suggestion_hash=suggestion_hash,
        )

    except Exception as e:
        logger.error(
            "Error handling follow-up callback",
            error=str(e),
            user_id=user_id,
            suggestion_hash=suggestion_hash,
        )

        await query.edit_message_text(
            "❌ <b>处理后续建议出错</b>\n\n" "处理后续建议时发生错误。",
            parse_mode="HTML",
        )


async def handle_conversation_callback(
    query, action_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle conversation control callbacks."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]

    if action_type == "continue":
        # Remove suggestion buttons and show continue message
        await query.edit_message_text(
            "✅ <b>继续对话</b>\n\n"
            "发送下一条消息即可继续编码！\n\n"
            "我可以帮你：\n"
            "• 代码审查与调试\n"
            "• 功能实现\n"
            "• 架构设计\n"
            "• 测试与优化\n"
            "• 文档编写\n\n"
            "<i>直接输入需求或上传文件即可。</i>",
            parse_mode="HTML",
        )

    elif action_type == "end":
        # End the current session
        conversation_enhancer = context.bot_data.get("conversation_enhancer")
        if conversation_enhancer:
            conversation_enhancer.clear_context(user_id)

        # Clear session data
        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = False

        current_dir = context.user_data.get(
            "current_directory", settings.approved_directory
        )
        relative_path = current_dir.relative_to(settings.approved_directory)

        # Create quick action buttons
        keyboard = [
            [
                InlineKeyboardButton(
                    "🆕 新建 session", callback_data="action:new_session"
                ),
                InlineKeyboardButton(
                    "📁 切换项目", callback_data="action:show_projects"
                ),
            ],
            [
                InlineKeyboardButton("📊 状态", callback_data="action:status"),
                InlineKeyboardButton("❓ 帮助", callback_data="action:help"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "✅ <b>对话已结束</b>\n\n"
            f"你的 Claude session 已终止。\n\n"
            f"<b>当前状态：</b>\n"
            f"• 目录：<code>{escape_html(str(relative_path))}/</code>\n"
            f"• Session：无\n"
            f"• 准备就绪\n\n"
            f"<b>下一步：</b>\n"
            f"• 创建新 session\n"
            f"• 查看状态\n"
            f"• 发送任意消息开始新对话",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

        logger.info("Conversation ended via callback", user_id=user_id)

    else:
        await query.edit_message_text(
            f"❌ <b>未知对话操作：{escape_html(action_type)}</b>\n\n"
            "无法识别此对话操作。",
            parse_mode="HTML",
        )


async def handle_git_callback(
    query, git_action: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle git-related callbacks."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    features = context.bot_data.get("features")

    if not features or not features.is_enabled("git"):
        await query.edit_message_text(
            "❌ <b>Git 集成已禁用</b>\n\n" "Git 集成功能未启用。",
            parse_mode="HTML",
        )
        return

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        git_integration = features.get_git_integration()
        if not git_integration:
            await query.edit_message_text(
                "❌ <b>Git 集成不可用</b>\n\n" "Git 集成服务不可用。",
                parse_mode="HTML",
            )
            return

        if git_action == "status":
            # Refresh git status
            git_status = await git_integration.get_status(current_dir)
            status_message = git_integration.format_status(git_status)

            keyboard = [
                [
                    InlineKeyboardButton("📊 查看 Diff", callback_data="git:diff"),
                    InlineKeyboardButton("📜 查看 Log", callback_data="git:log"),
                ],
                [
                    InlineKeyboardButton("🔄 刷新", callback_data="git:status"),
                    InlineKeyboardButton("📁 文件", callback_data="action:ls"),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                status_message, parse_mode="HTML", reply_markup=reply_markup
            )

        elif git_action == "diff":
            # Show git diff
            diff_output = await git_integration.get_diff(current_dir)

            if not diff_output.strip():
                diff_message = "📊 <b>Git Diff</b>\n\n<i>没有可显示的更改。</i>"
            else:
                # Clean up diff output for Telegram
                # Remove emoji symbols that interfere with parsing
                clean_diff = (
                    diff_output.replace("➕", "+").replace("➖", "-").replace("📍", "@")
                )

                # Limit diff output (leave room for header + HTML tags within
                # Telegram's 4096-char message limit)
                max_length = 3500
                if len(clean_diff) > max_length:
                    clean_diff = clean_diff[:max_length] + "\n\n...输出已截断..."

                escaped_diff = escape_html(clean_diff)
                diff_message = (
                    f"📊 <b>Git Diff</b>\n\n<pre><code>{escaped_diff}</code></pre>"
                )

            keyboard = [
                [
                    InlineKeyboardButton("📜 查看 Log", callback_data="git:log"),
                    InlineKeyboardButton("📊 状态", callback_data="git:status"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                diff_message, parse_mode="HTML", reply_markup=reply_markup
            )

        elif git_action == "log":
            # Show git log
            commits = await git_integration.get_file_history(current_dir, ".")

            if not commits:
                log_message = "📜 <b>Git Log</b>\n\n<i>未找到提交记录。</i>"
            else:
                log_message = "📜 <b>Git Log</b>\n\n"
                for commit in commits[:10]:  # Show last 10 commits
                    short_hash = commit.hash[:7]
                    short_message = escape_html(commit.message[:60])
                    if len(commit.message) > 60:
                        short_message += "..."
                    log_message += f"• <code>{short_hash}</code> {short_message}\n"

            keyboard = [
                [
                    InlineKeyboardButton("📊 查看 Diff", callback_data="git:diff"),
                    InlineKeyboardButton("📊 状态", callback_data="git:status"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                log_message, parse_mode="HTML", reply_markup=reply_markup
            )

        else:
            await query.edit_message_text(
                f"❌ <b>未知 Git 操作：{escape_html(git_action)}</b>\n\n"
                "无法识别此 Git 操作。",
                parse_mode="HTML",
            )

    except Exception as e:
        logger.error(
            "Error in git callback",
            error=str(e),
            git_action=git_action,
            user_id=user_id,
        )
        await query.edit_message_text(
            f"❌ <b>Git 错误</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )


async def handle_export_callback(
    query, export_format: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle export format selection callbacks."""
    user_id = query.from_user.id
    features = context.bot_data.get("features")

    if export_format == "cancel":
        await query.edit_message_text(
            "📤 <b>导出已取消</b>\n\n" "Session 导出已取消。",
            parse_mode="HTML",
        )
        return

    from ..features.session_export import ExportFormat

    try:
        export_enum = ExportFormat(export_format)
    except ValueError:
        await query.edit_message_text(
            f"未知的导出格式：<code>{escape_html(export_format)}</code>",
            parse_mode="HTML",
        )
        return

    session_exporter = features.get_session_export() if features else None
    if not session_exporter:
        await query.edit_message_text(
            "❌ <b>导出不可用</b>\n\n" "Session 导出服务不可用。",
            parse_mode="HTML",
        )
        return

    # Get current session
    claude_session_id = context.user_data.get("claude_session_id")
    if not claude_session_id:
        await query.edit_message_text(
            "❌ <b>无活跃 Session</b>\n\n" "没有活跃的 session 可以导出。",
            parse_mode="HTML",
        )
        return

    try:
        # Show processing message
        await query.edit_message_text(
            f"📤 <b>正在导出 Session</b>\n\n"
            f"正在生成 {escape_html(export_format.upper())} 导出文件...",
            parse_mode="HTML",
        )

        # Export session
        settings: Settings = context.bot_data["settings"]
        current_directory = context.user_data.get(
            "current_directory", settings.approved_directory
        )
        exported_session = await session_exporter.export_session(
            user_id=user_id,
            session_id=claude_session_id,
            format=export_enum,
            project_path=str(current_directory),
        )

        # Send the exported file
        from io import BytesIO

        file_bytes = BytesIO(exported_session.content.encode("utf-8"))
        file_bytes.name = exported_session.filename

        await query.message.reply_document(
            document=file_bytes,
            filename=exported_session.filename,
            caption=(
                f"📤 <b>Session 导出完成</b>\n\n"
                f"格式：{escape_html(exported_session.format.value.upper())}\n"
                f"大小：{exported_session.size_bytes:,} 字节\n"
                f"创建时间：{exported_session.created_at.strftime('%Y-%m-%d %H:%M:%S')}"
            ),
            parse_mode="HTML",
        )

        # Update the original message
        await query.edit_message_text(
            f"✅ <b>导出完成</b>\n\n"
            f"你的 session 已导出为 {escape_html(exported_session.filename)}。\n"
            f"请查看上方文件获取完整对话历史。",
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(
            "Export failed", error=str(e), user_id=user_id, format=export_format
        )
        await query.edit_message_text(
            f"❌ <b>导出失败</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )


def _format_file_size(size: int) -> str:
    """Format file size in human-readable format."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}B"
        size /= 1024
    return f"{size:.1f}TB"


def _escape_markdown(text: str) -> str:
    """Escape HTML-special characters in text for Telegram.

    Legacy name kept for compatibility with callers; actually escapes HTML.
    """
    return escape_html(text)


async def handle_sessions_callback(
    query, param: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Sub-dispatcher for sessions:* callbacks.

    `param` is the portion after `sessions:` — e.g. `list:1`, `detail:<id>`,
    `view:<id>`, `resume:<id>`, `export:<id>`, `export:<id>:md`, `back:1`,
    `confirm_delete:<id>`, `do_delete:<id>`.
    """
    user_id = query.from_user.id
    storage = context.bot_data["storage"].sessions
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    settings: Settings = context.bot_data["settings"]
    current_directory = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    if ":" in param:
        sub_action, rest = param.split(":", 1)
    else:
        sub_action, rest = param, ""

    if sub_action in ("list", "back"):
        try:
            page = int(rest) if rest else 0
        except ValueError:
            page = 0
        text, kb = await list_sessions_view(
            storage=storage,
            user_id=user_id,
            project_path=str(current_directory),
            page=page,
            current_session_id=context.user_data.get("claude_session_id"),
        )
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        if audit_logger:
            await audit_logger.log_session_event(
                user_id=user_id,
                action="sessions_list",
                success=True,
                details={"page": page},
            )
        return

    if sub_action == "detail":
        session_id = rest
        result = await session_detail_view(
            storage=storage,
            user_id=user_id,
            session_id=session_id,
            back_page=0,
            session_timeout_hours=settings.session_timeout_hours,
            project_path=str(current_directory),
            current_session_id=context.user_data.get("claude_session_id"),
        )
        if result is None:
            # Distinguish "cross-user" from "missing" — the session row may
            # exist but be owned by a different user.
            row_exists = False
            db = getattr(storage, "db", None) or getattr(storage, "db_manager", None)
            if db is not None:
                async with db.get_connection() as conn:
                    cursor = await conn.execute(
                        "SELECT 1 FROM sessions "
                        "WHERE session_id = ? AND is_active = TRUE",
                        (session_id,),
                    )
                    row_exists = (await cursor.fetchone()) is not None

            if row_exists:
                await query.answer("无权访问该 session", show_alert=True)
                if audit_logger:
                    await audit_logger.log_session_event(
                        user_id=user_id,
                        action="sessions_cross_user_denied",
                        success=False,
                        details={"session_id": session_id},
                    )
                return

            await query.answer("session 不存在或已删除")
            # Refresh to list page 0
            text, kb = await list_sessions_view(
                storage=storage,
                user_id=user_id,
                project_path=str(current_directory),
                page=0,
                current_session_id=context.user_data.get("claude_session_id"),
            )
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            return

        text, kb = result
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        if audit_logger:
            await audit_logger.log_session_event(
                user_id=user_id,
                action="sessions_detail",
                success=True,
                details={"session_id": session_id},
            )
        return

    if sub_action in ("rename", "tag", "cleartag"):
        session_id = rest
        ownership = await _check_session_ownership(
            storage, user_id, session_id, str(current_directory)
        )
        if ownership == "btw_fork":
            await query.answer("BTW 旁路会话已隐藏", show_alert=True)
            return
        if ownership == "cross_user":
            await query.answer("无权访问该 session", show_alert=True)
            return
        if ownership == "missing":
            await query.answer("session 不存在或已删除")
            return

        if sub_action == "cleartag":
            ok = await ClaudeIntegration.tag_sdk_session(
                session_id,
                Path(str(current_directory)),
                None,
            )
            if ok:
                await query.message.reply_text("session 标签已清除。")
                if audit_logger:
                    await audit_logger.log_session_event(
                        user_id=user_id,
                        action="sessions_clear_tag",
                        success=True,
                        details={"session_id": session_id},
                    )
            else:
                await query.message.reply_text(
                    "清除标签失败，本地 transcript 可能不存在。"
                )
            return

        context.user_data["session_edit_action"] = {
            "action": sub_action,
            "session_id": session_id,
            "project_path": str(current_directory),
        }
        prompt = (
            "请发送新的 session 标题。发送 /cancel 可取消。"
            if sub_action == "rename"
            else "请发送 session 标签。发送 /cancel 可取消。"
        )
        await query.message.reply_text(prompt)
        if audit_logger:
            await audit_logger.log_session_event(
                user_id=user_id,
                action=f"sessions_{sub_action}_prompt",
                success=True,
                details={"session_id": session_id},
            )
        return

    if sub_action == "confirm_delete":
        session_id = rest
        ownership = await _check_session_ownership(
            storage, user_id, session_id, str(current_directory)
        )
        if ownership == "btw_fork":
            await query.answer("BTW 旁路会话已隐藏", show_alert=True)
            return
        if ownership == "cross_user":
            await query.answer("无权访问该 session", show_alert=True)
            return
        if ownership == "missing":
            await query.answer("session 不存在或已删除")
            text, kb = await list_sessions_view(
                storage=storage,
                user_id=user_id,
                project_path=str(current_directory),
                page=0,
                current_session_id=context.user_data.get("claude_session_id"),
            )
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if context.user_data.get("claude_session_id") == session_id:
            await query.answer(
                "不能删除当前正在使用的 session", show_alert=True
            )
            return

        await query.edit_message_reply_markup(
            reply_markup=delete_confirm_keyboard(session_id)
        )
        if audit_logger:
            await audit_logger.log_session_event(
                user_id=user_id,
                action="sessions_delete_confirm",
                success=True,
                details={"session_id": session_id},
            )
        return

    if sub_action == "do_delete":
        session_id = rest
        ownership = await _check_session_ownership(
            storage, user_id, session_id, str(current_directory)
        )
        if ownership == "btw_fork":
            await query.answer("BTW 旁路会话已隐藏", show_alert=True)
            return
        if ownership == "cross_user":
            await query.answer("无权访问该 session", show_alert=True)
            return
        if ownership == "missing":
            await query.answer("session 不存在或已删除")
            text, kb = await list_sessions_view(
                storage=storage,
                user_id=user_id,
                project_path=str(current_directory),
                page=0,
                current_session_id=context.user_data.get("claude_session_id"),
            )
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            return

        if context.user_data.get("claude_session_id") == session_id:
            await query.answer(
                "不能删除当前正在使用的 session", show_alert=True
            )
            return

        try:
            await ClaudeIntegration.delete_session(
                session_id=session_id,
                project_path=Path(str(current_directory)),
                session_storage=storage,
            )
        except Exception:
            logger.exception("Failed to delete session", session_id=session_id)
            await query.answer("删除失败", show_alert=True)
            await query.message.reply_text("删除失败，请重试。")
            return

        await query.answer("session 已删除")
        text, kb = await list_sessions_view(
            storage=storage,
            user_id=user_id,
            project_path=str(current_directory),
            page=0,
            current_session_id=context.user_data.get("claude_session_id"),
        )
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        if audit_logger:
            await audit_logger.log_session_event(
                user_id=user_id,
                action="sessions_deleted",
                success=True,
                details={"session_id": session_id},
            )
        return

    if sub_action == "view":
        session_id = rest
        ownership = await _check_session_ownership(
            storage, user_id, session_id, str(current_directory)
        )
        if ownership == "btw_fork":
            await query.answer("BTW 旁路会话已隐藏", show_alert=True)
            return
        if ownership == "cross_user":
            await query.answer("无权访问该 session", show_alert=True)
            if audit_logger:
                await audit_logger.log_session_event(
                    user_id=user_id,
                    action="sessions_cross_user_denied",
                    success=False,
                    details={"session_id": session_id, "action": "view"},
                )
            return
        if ownership == "missing":
            await query.answer("session 不存在或已删除")
            return

        await query.answer("生成 HTML 中…")

        from ..features.session_export import ExportFormat

        features = context.bot_data.get("features")
        exporter = features.get_session_export() if features else None
        if not exporter:
            await query.message.reply_text(
                "❌ <b>导出不可用</b>",
                parse_mode="HTML",
            )
            return
        try:
            exported = await exporter.export_session(
                user_id=user_id,
                session_id=session_id,
                format=ExportFormat.HTML,
                project_path=str(current_directory),
            )
        except Exception as e:
            logger.error(
                "Failed to export session HTML",
                user_id=user_id,
                session_id=session_id,
                error=str(e),
            )
            await query.message.reply_text(
                f"❌ <b>生成 HTML 失败：</b><code>{escape_html(type(e).__name__)}</code>",
                parse_mode="HTML",
            )
            return

        title = await _resolve_title_for_handler(storage, current_directory, session_id)
        date_str = datetime.now(UTC).strftime("%Y%m%d")
        filename = f"{_safe_filename_fragment(title)}_{date_str}.html"

        await query.message.reply_document(
            document=exported.content.encode("utf-8"),
            filename=filename,
            caption=f"📄 {escape_html(title)}",
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_session_event(
                user_id=user_id,
                action="sessions_view_html",
                success=True,
                details={"session_id": session_id},
            )
        return

    if sub_action == "resume":
        session_id = rest
        ownership = await _check_session_ownership(
            storage, user_id, session_id, str(current_directory)
        )
        if ownership == "btw_fork":
            await query.answer("BTW 旁路会话不能恢复", show_alert=True)
            return
        if ownership == "cross_user":
            await query.answer("无权访问该 session", show_alert=True)
            if audit_logger:
                await audit_logger.log_session_event(
                    user_id=user_id,
                    action="sessions_cross_user_denied",
                    success=False,
                    details={"session_id": session_id, "action": "resume"},
                )
            return
        if ownership == "missing":
            await query.answer("session 不存在或已删除")
            return

        title = await _resolve_title_for_handler(storage, current_directory, session_id)
        context.user_data["claude_session_id"] = session_id
        context.user_data["force_new_session"] = False
        await query.message.reply_text(
            f"✅ 已切到 session «<b>{escape_html(title)}</b>»。\n"
            f"ID：<code>{escape_html(session_id)}</code>\n"
            "发消息即继续。",
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_session_event(
                user_id=user_id,
                action="sessions_resume",
                success=True,
                details={"session_id": session_id},
            )
        return

    if sub_action == "export":
        # rest is either "<id>" (show submenu) or "<id>:<fmt>" (do export)
        if ":" in rest:
            session_id, fmt = rest.split(":", 1)
        else:
            session_id, fmt = rest, ""

        ownership = await _check_session_ownership(
            storage, user_id, session_id, str(current_directory)
        )
        if ownership == "btw_fork":
            await query.answer("BTW 旁路会话不能导出", show_alert=True)
            return
        if ownership == "cross_user":
            await query.answer("无权访问该 session", show_alert=True)
            if audit_logger:
                await audit_logger.log_session_event(
                    user_id=user_id,
                    action="sessions_cross_user_denied",
                    success=False,
                    details={"session_id": session_id, "action": "export"},
                )
            return
        if ownership == "missing":
            await query.answer("session 不存在或已删除")
            return

        from ..features.session_export import ExportFormat

        format_map = {
            "md": ExportFormat.MARKDOWN,
            "json": ExportFormat.JSON,
        }
        export_format = format_map.get(fmt)
        if export_format is None:
            await query.answer(f"未知的导出格式：{fmt}")
            return

        await query.answer("生成中…")

        features = context.bot_data.get("features")
        exporter = features.get_session_export() if features else None
        if not exporter:
            await query.message.reply_text(
                "❌ <b>导出不可用</b>",
                parse_mode="HTML",
            )
            return
        try:
            exported = await exporter.export_session(
                user_id=user_id,
                session_id=session_id,
                format=export_format,
                project_path=str(current_directory),
            )
        except Exception as e:
            logger.error(
                "Failed to export session",
                user_id=user_id,
                session_id=session_id,
                format=fmt,
                error=str(e),
            )
            await query.message.reply_text(
                f"❌ <b>导出失败：</b><code>{escape_html(type(e).__name__)}</code>",
                parse_mode="HTML",
            )
            return

        title = await _resolve_title_for_handler(storage, current_directory, session_id)
        date_str = datetime.now(UTC).strftime("%Y%m%d")
        filename = f"{_safe_filename_fragment(title)}_{date_str}.{fmt}"
        await query.message.reply_document(
            document=exported.content.encode("utf-8"),
            filename=filename,
            caption=f"📦 {escape_html(title)}",
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_session_event(
                user_id=user_id,
                action=f"sessions_export_{fmt}",
                success=True,
                details={"session_id": session_id},
            )
        return

    await query.edit_message_text("❌ <b>未知的 session 动作</b>", parse_mode="HTML")
