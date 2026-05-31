"""Command handlers for bot operations."""

import json
import os
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...claude.facade import ClaudeIntegration
from ...config.settings import Settings
from ...projects import PrivateTopicsUnavailableError, load_project_registry
from ...security.audit import AuditLogger
from ...security.validators import SecurityValidator
from ...storage.models import SessionModel
from ..utils.html_format import escape_html

logger = structlog.get_logger()


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


def _is_private_chat(update: Update) -> bool:
    """Return True when update is from a private chat."""
    chat = update.effective_chat
    return bool(chat and getattr(chat, "type", "") == "private")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    user = update.effective_user
    settings: Settings = context.bot_data["settings"]
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    manager = context.bot_data.get("project_threads_manager")
    sync_section = ""

    if settings.enable_project_threads and settings.project_threads_mode == "private":
        if not _is_private_chat(update):
            await update.message.reply_text(
                "🚫 <b>私有话题模式</b>\n\n"
                "请在与机器人的私聊中运行 <code>/start</code>。",
                parse_mode="HTML",
            )
            return

    if (
        settings.enable_project_threads
        and settings.project_threads_mode == "private"
        and _is_private_chat(update)
    ):
        if manager is None:
            await update.message.reply_text(
                "❌ <b>项目话题模式配置错误</b>\n\n" "话题管理器未初始化。",
                parse_mode="HTML",
            )
            return

        try:
            sync_result = await manager.sync_topics(
                context.bot,
                chat_id=update.effective_chat.id,
            )
            sync_section = (
                "\n\n🧵 <b>项目话题已同步</b>\n"
                f"• 新建: <b>{sync_result.created}</b>\n"
                f"• 复用: <b>{sync_result.reused}</b>\n"
                f"• 重命名: <b>{sync_result.renamed}</b>\n"
                f"• 失败: <b>{sync_result.failed}</b>\n\n"
                "使用项目话题线程开始编码。"
            )
        except PrivateTopicsUnavailableError:
            await update.message.reply_text(
                manager.private_topics_unavailable_message(),
                parse_mode="HTML",
            )
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user.id,
                    command="start",
                    args=[],
                    success=False,
                )
            return
        except Exception as e:
            sync_section = (
                "\n\n⚠️ <b>话题同步警告</b>\n"
                f"{escape_html(str(e))}\n\n"
                "运行 <code>/sync_threads</code> 重试。"
            )

    welcome_message = (
        f"👋 欢迎使用 Claude Code Telegram Bot, {escape_html(user.first_name)}!\n\n"
        f"🤖 我可以帮你通过 Telegram 远程使用 Claude Code。\n\n"
        f"<b>可用命令:</b>\n"
        f"• <code>/help</code> - 查看详细帮助\n"
        f"• <code>/new</code> - 开始新的 Claude session\n"
        f"• <code>/ls</code> - 列出当前目录文件\n"
        f"• <code>/cd &lt;dir&gt;</code> - 切换目录\n"
        f"• <code>/projects</code> - 查看可用项目\n"
        f"• <code>/status</code> - 查看 session 状态\n"
        f"• <code>/actions</code> - 查看快捷操作\n"
        f"• <code>/git</code> - Git 仓库命令\n\n"
        f"<b>快速开始:</b>\n"
        f"1. 使用 <code>/projects</code> 查看可用项目\n"
        f"2. 使用 <code>/cd &lt;project&gt;</code> 进入项目目录\n"
        f"3. 发送任意消息即可开始与 Claude 协作编码!\n\n"
        f"🔒 你的访问已受保护，所有操作均有记录。\n"
        f"📊 使用 <code>/status</code> 查看用量限制。"
        f"{sync_section}"
    )

    # Add quick action buttons
    keyboard = [
        [
            InlineKeyboardButton("📁 显示项目", callback_data="action:show_projects"),
            InlineKeyboardButton("❓ 获取帮助", callback_data="action:help"),
        ],
        [
            InlineKeyboardButton("🆕 新建 Session", callback_data="action:new_session"),
            InlineKeyboardButton("📊 查看状态", callback_data="action:status"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        welcome_message, parse_mode="HTML", reply_markup=reply_markup
    )

    # Log command
    if audit_logger:
        await audit_logger.log_command(
            user_id=user.id, command="start", args=[], success=True
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    help_text = (
        "🤖 <b>Claude Code Telegram Bot 帮助</b>\n\n"
        "<b>导航命令:</b>\n"
        "• <code>/ls</code> - 列出文件和目录\n"
        "• <code>/cd &lt;directory&gt;</code> - 切换目录\n"
        "• <code>/pwd</code> - 显示当前目录\n"
        "• <code>/projects</code> - 显示可用项目\n\n"
        "<b>Session 命令:</b>\n"
        "• <code>/new</code> - 清除上下文，开始新 session\n"
        "• <code>/continue [message]</code> - 继续上一个 session\n"
        "• <code>/end</code> - 结束当前 session 并清除上下文\n"
        "• <code>/status</code> - 显示 session 和用量状态\n"
        "• <code>/export</code> - 导出 session 历史\n"
        "• <code>/actions</code> - 显示上下文快捷操作\n"
        "• <code>/git</code> - Git 仓库信息\n\n"
        "<b>Session 行为:</b>\n"
        "• 每个项目目录自动维护独立的 session\n"
        "• 使用 <code>/cd</code> 切换目录时会恢复该项目的 session\n"
        "• 使用 <code>/new</code> 或 <code>/end</code> 可显式清除 session 上下文\n"
        "• Session 在机器人重启后仍然保留\n\n"
        "<b>使用示例:</b>\n"
        "• <code>cd myproject</code> - 进入项目目录\n"
        "• <code>ls</code> - 查看当前目录内容\n"
        "• <code>创建一个简单的 Python 脚本</code> - 让 Claude 编写代码\n"
        "• 发送文件让 Claude 审查\n\n"
        "<b>文件操作:</b>\n"
        "• 发送文本文件 (.py, .js, .md 等) 进行审查\n"
        "• Claude 可以读取、修改和创建文件\n"
        "• 所有文件操作均在你的授权目录内\n\n"
        "<b>安全特性:</b>\n"
        "• 🔒 路径遍历保护\n"
        "• ⏱️ 速率限制防止滥用\n"
        "• 📊 用量追踪和限制\n"
        "• 🛡️ 输入验证和清理\n\n"
        "<b>提示:</b>\n"
        "• 使用具体、清晰的请求以获得最佳结果\n"
        "• 使用 <code>/status</code> 监控用量\n"
        "• 有快捷按钮时可直接点击使用\n"
        "• 上传的文件会自动由 Claude 处理\n\n"
        "需要更多帮助？请联系管理员。"
    )

    await update.message.reply_text(help_text, parse_mode="HTML")


async def sync_threads(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Synchronize project topics in the configured forum chat."""
    settings: Settings = context.bot_data["settings"]
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    user_id = update.effective_user.id

    if not settings.enable_project_threads:
        await update.message.reply_text(
            "ℹ️ <b>项目话题模式已禁用。</b>", parse_mode="HTML"
        )
        return

    manager = context.bot_data.get("project_threads_manager")
    if not manager:
        await update.message.reply_text(
            "❌ <b>项目话题管理器未初始化。</b>", parse_mode="HTML"
        )
        return

    status_msg = await update.message.reply_text(
        "🔄 <b>正在同步项目话题...</b>", parse_mode="HTML"
    )

    if settings.project_threads_mode == "private":
        if not _is_private_chat(update):
            await status_msg.edit_text(
                "❌ <b>私有话题模式</b>\n\n"
                "请在与机器人的私聊中运行 <code>/sync_threads</code>。",
                parse_mode="HTML",
            )
            return
        target_chat_id = update.effective_chat.id
    else:
        if settings.project_threads_chat_id is None:
            await status_msg.edit_text(
                "❌ <b>群组话题模式配置错误</b>\n\n"
                "请先设置 <code>PROJECT_THREADS_CHAT_ID</code>。",
                parse_mode="HTML",
            )
            return
        if (
            not update.effective_chat
            or update.effective_chat.id != settings.project_threads_chat_id
        ):
            await status_msg.edit_text(
                "❌ <b>群组话题模式</b>\n\n"
                "请在已配置的项目话题群组中运行 <code>/sync_threads</code>。",
                parse_mode="HTML",
            )
            return
        target_chat_id = settings.project_threads_chat_id

    try:
        if not settings.projects_config_path:
            await status_msg.edit_text(
                "❌ <b>项目话题模式配置错误</b>\n\n"
                "请将 <code>PROJECTS_CONFIG_PATH</code> 设置为有效的 YAML 文件。",
                parse_mode="HTML",
            )
            if audit_logger:
                await audit_logger.log_command(user_id, "sync_threads", [], False)
            return

        registry = load_project_registry(
            config_path=settings.projects_config_path,
            approved_directory=settings.approved_directory,
        )
        manager.registry = registry
        context.bot_data["project_registry"] = registry

        result = await manager.sync_topics(context.bot, chat_id=target_chat_id)
        await status_msg.edit_text(
            "✅ <b>项目话题同步完成</b>\n\n"
            f"• 新建: <b>{result.created}</b>\n"
            f"• 复用: <b>{result.reused}</b>\n"
            f"• 重命名: <b>{result.renamed}</b>\n"
            f"• 重新打开: <b>{result.reopened}</b>\n"
            f"• 关闭: <b>{result.closed}</b>\n"
            f"• 停用: <b>{result.deactivated}</b>\n"
            f"• 失败: <b>{result.failed}</b>",
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_threads", [], True)
    except PrivateTopicsUnavailableError:
        await status_msg.edit_text(
            manager.private_topics_unavailable_message(),
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_threads", [], False)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ <b>项目话题同步失败</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_command(user_id, "sync_threads", [], False)


async def new_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /new command - explicitly starts a fresh session, clearing previous context."""
    settings: Settings = context.bot_data["settings"]

    # Get current directory (default to approved directory)
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Track what was cleared for user feedback
    old_session_id = context.user_data.get("claude_session_id")

    # Clear existing session data - this is the explicit way to reset context
    context.user_data["claude_session_id"] = None
    context.user_data["session_started"] = True
    context.user_data["force_new_session"] = True

    cleared_info = ""
    if old_session_id:
        cleared_info = f"\n🗑️ 已清除之前的 session <code>{old_session_id}</code>。"

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

    await update.message.reply_text(
        f"🆕 <b>新 Claude Code Session</b>\n\n"
        f"📂 工作目录: <code>{relative_path}/</code>{cleared_info}\n\n"
        f"上下文已清除。发送消息开始新的对话，"
        f"或使用下方按钮:",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def continue_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /continue command with optional prompt."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")

    # Parse optional prompt from command arguments
    # If no prompt provided, use a default to continue the conversation
    prompt = " ".join(context.args) if context.args else None
    default_prompt = "请继续我们之前的对话"

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        if not claude_integration:
            await update.message.reply_text(
                "❌ <b>Claude 集成不可用</b>\n\n" "Claude 集成未正确配置。"
            )
            return

        # Check if there's an existing session in user context
        claude_session_id = context.user_data.get("claude_session_id")

        if claude_session_id:
            # We have a session in context, continue it directly
            status_msg = await update.message.reply_text(
                f"🔄 <b>继续 Session</b>\n\n"
                f"Session ID: <code>{claude_session_id}</code>\n"
                f"目录: <code>{current_dir.relative_to(settings.approved_directory)}/</code>\n\n"
                f"{'正在处理你的消息...' if prompt else '正在继续之前的对话...'}",
                parse_mode="HTML",
            )

            # Continue with the existing session
            # Use default prompt if none provided (Claude CLI requires a prompt)
            claude_response = await claude_integration.run_command(
                prompt=prompt or default_prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=claude_session_id,
            )
        else:
            # No session in context, try to find the most recent session
            status_msg = await update.message.reply_text(
                "🔍 <b>查找最近的 Session</b>\n\n"
                "正在搜索此目录中你最近的 session...",
                parse_mode="HTML",
            )

            # Use default prompt if none provided
            claude_response = await claude_integration.continue_session(
                user_id=user_id,
                working_directory=current_dir,
                prompt=prompt or default_prompt,
            )

        if claude_response:
            # Update session ID in context
            context.user_data["claude_session_id"] = claude_response.session_id

            # Delete status message and send response
            await status_msg.delete()

            # Format and send Claude's response
            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            for msg in formatted_messages:
                await update.message.reply_text(
                    msg.text,
                    parse_mode=msg.parse_mode,
                    reply_markup=msg.reply_markup,
                )

            # Log successful continue
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user_id,
                    command="continue",
                    args=context.args or [],
                    success=True,
                )

        else:
            # No session found to continue
            await status_msg.edit_text(
                "❌ <b>未找到 Session</b>\n\n"
                f"在此目录中未找到最近的 Claude session。\n"
                f"目录: <code>{current_dir.relative_to(settings.approved_directory)}/</code>\n\n"
                f"<b>你可以:</b>\n"
                f"• 使用 <code>/new</code> 开始新 session\n"
                f"• 使用 <code>/status</code> 查看你的 session\n"
                f"• 使用 <code>/cd</code> 切换到其他目录",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🆕 新建 Session", callback_data="action:new_session"
                            ),
                            InlineKeyboardButton(
                                "📊 状态", callback_data="action:status"
                            ),
                        ]
                    ]
                ),
            )

    except Exception as e:
        error_msg = str(e)
        logger.error("Error in continue command", error=error_msg, user_id=user_id)

        # Delete status message if it exists
        try:
            if "status_msg" in locals():
                await status_msg.delete()
        except Exception:
            pass

        # Send error response
        await update.message.reply_text(
            f"❌ <b>继续 Session 出错</b>\n\n"
            f"尝试继续 session 时发生错误:\n\n"
            f"<code>{error_msg}</code>\n\n"
            f"<b>建议:</b>\n"
            f"• 尝试使用 <code>/new</code> 开始新 session\n"
            f"• 使用 <code>/status</code> 检查 session 状态\n"
            f"• 如问题持续，请联系管理员",
            parse_mode="HTML",
        )

        # Log failed continue
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="continue",
                args=context.args or [],
                success=False,
            )


async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ls command."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")

    # Get current directory
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        # List directory contents
        items = []
        directories = []
        files = []

        for item in sorted(current_dir.iterdir()):
            # Skip hidden files (starting with .)
            if item.name.startswith("."):
                continue

            # Escape HTML special characters in filenames
            safe_name = _escape_markdown(item.name)

            if item.is_dir():
                directories.append(f"📁 {safe_name}/")
            else:
                # Get file size
                try:
                    size = item.stat().st_size
                    size_str = _format_file_size(size)
                    files.append(f"📄 {safe_name} ({size_str})")
                except OSError:
                    files.append(f"📄 {safe_name}")

        # Combine directories first, then files
        items = directories + files

        # Format response
        relative_path = current_dir.relative_to(settings.approved_directory)
        if not items:
            message = f"📂 <code>{relative_path}/</code>\n\n<i>(空目录)</i>"
        else:
            message = f"📂 <code>{relative_path}/</code>\n\n"

            # Limit items shown to prevent message being too long
            max_items = 50
            if len(items) > max_items:
                shown_items = items[:max_items]
                message += "\n".join(shown_items)
                message += f"\n\n<i>... 还有 {len(items) - max_items} 个项目</i>"
            else:
                message += "\n".join(items)

        # Add navigation buttons if not at root
        keyboard = []
        if current_dir != settings.approved_directory:
            keyboard.append(
                [
                    InlineKeyboardButton("⬆️ 上级目录", callback_data="cd:.."),
                    InlineKeyboardButton("🏠 返回根目录", callback_data="cd:/"),
                ]
            )

        keyboard.append(
            [
                InlineKeyboardButton("🔄 刷新", callback_data="action:refresh_ls"),
                InlineKeyboardButton("📁 项目", callback_data="action:show_projects"),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

        await update.message.reply_text(
            message, parse_mode="HTML", reply_markup=reply_markup
        )

        # Log successful command
        if audit_logger:
            await audit_logger.log_command(user_id, "ls", [], True)

    except Exception as e:
        error_msg = f"❌ 列出目录时出错: {str(e)}"
        await update.message.reply_text(error_msg)

        # Log failed command
        if audit_logger:
            await audit_logger.log_command(user_id, "ls", [], False)

        logger.error("Error in list_files command", error=str(e), user_id=user_id)


async def change_directory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cd command."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    security_validator: SecurityValidator = context.bot_data.get("security_validator")
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")

    # Parse arguments
    if not context.args:
        await update.message.reply_text(
            "<b>用法:</b> <code>/cd &lt;directory&gt;</code>\n\n"
            "<b>示例:</b>\n"
            "• <code>/cd myproject</code> - 进入子目录\n"
            "• <code>/cd ..</code> - 返回上一级\n"
            "• <code>/cd /</code> - 返回授权目录根目录\n\n"
            "<b>提示:</b>\n"
            "• 使用 <code>/ls</code> 查看可用目录\n"
            "• 使用 <code>/projects</code> 查看所有项目",
            parse_mode="HTML",
        )
        return

    target_path = " ".join(context.args)
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    project_root = _get_thread_project_root(settings, context)
    directory_root = project_root or settings.approved_directory

    try:
        # Handle known navigation shortcuts first
        if target_path == "/":
            resolved_path = directory_root
        elif target_path == "..":
            resolved_path = current_dir.parent
            if not _is_within_root(resolved_path, directory_root):
                resolved_path = directory_root
        else:
            # Validate path using security validator
            if security_validator:
                valid, resolved_path, error = security_validator.validate_path(
                    target_path, current_dir
                )

                if not valid:
                    await update.message.reply_text(f"❌ <b>访问被拒绝</b>\n\n{error}")

                    # Log security violation
                    if audit_logger:
                        await audit_logger.log_security_violation(
                            user_id=user_id,
                            violation_type="path_traversal_attempt",
                            details=f"Attempted path: {target_path}",
                            severity="medium",
                        )
                    return
            else:
                resolved_path = current_dir / target_path
                resolved_path = resolved_path.resolve()

        if project_root and not _is_within_root(resolved_path, project_root):
            await update.message.reply_text(
                "❌ <b>访问被拒绝</b>\n\n" "在话题模式下，导航仅限于当前项目根目录。",
                parse_mode="HTML",
            )
            return

        # Check if directory exists and is actually a directory
        if not resolved_path.exists():
            await update.message.reply_text(
                f"❌ <b>目录未找到</b>\n\n<code>{target_path}</code> 不存在。"
            )
            return

        if not resolved_path.is_dir():
            await update.message.reply_text(
                f"❌ <b>不是目录</b>\n\n<code>{target_path}</code> 不是目录。"
            )
            return

        # Update current directory in user data
        context.user_data["current_directory"] = resolved_path

        # Look up existing session for the new directory instead of clearing
        claude_integration: ClaudeIntegration = context.bot_data.get(
            "claude_integration"
        )
        resumed_session_info = ""
        if claude_integration:
            existing_session = await claude_integration._find_resumable_session(
                user_id, resolved_path
            )
            if existing_session:
                context.user_data["claude_session_id"] = existing_session.session_id
                resumed_session_info = (
                    f"\n🔄 已恢复 session <code>{existing_session.session_id}</code> "
                    f"({existing_session.message_count} 条消息)"
                )
            else:
                # No session for this directory - clear the current one
                context.user_data["claude_session_id"] = None
                resumed_session_info = "\n🆕 无现有 session。发送消息开始新对话。"

        # Send confirmation
        relative_base = project_root or settings.approved_directory
        relative_path = resolved_path.relative_to(relative_base)
        relative_display = "/" if str(relative_path) == "." else f"{relative_path}/"
        await update.message.reply_text(
            f"✅ <b>目录已切换</b>\n\n"
            f"📂 当前目录: <code>{relative_display}</code>"
            f"{resumed_session_info}",
            parse_mode="HTML",
        )

        # Log successful command
        if audit_logger:
            await audit_logger.log_command(user_id, "cd", [target_path], True)

    except Exception as e:
        error_msg = f"❌ <b>切换目录出错</b>\n\n{str(e)}"
        await update.message.reply_text(error_msg, parse_mode="HTML")

        # Log failed command
        if audit_logger:
            await audit_logger.log_command(user_id, "cd", [target_path], False)

        logger.error("Error in change_directory command", error=str(e), user_id=user_id)


async def print_working_directory(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /pwd command."""
    settings: Settings = context.bot_data["settings"]
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    relative_path = current_dir.relative_to(settings.approved_directory)
    absolute_path = str(current_dir)

    # Add quick navigation buttons
    keyboard = [
        [
            InlineKeyboardButton("📁 列出文件", callback_data="action:ls"),
            InlineKeyboardButton("📋 项目", callback_data="action:show_projects"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"📍 <b>当前目录</b>\n\n"
        f"相对路径: <code>{relative_path}/</code>\n"
        f"绝对路径: <code>{absolute_path}</code>",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def show_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /projects command."""
    settings: Settings = context.bot_data["settings"]

    try:
        if settings.enable_project_threads:
            registry = context.bot_data.get("project_registry")
            manager = context.bot_data.get("project_threads_manager")
            if manager and getattr(manager, "registry", None):
                registry = manager.registry
            if not registry:
                await update.message.reply_text(
                    "❌ <b>项目注册表未初始化。</b>",
                    parse_mode="HTML",
                )
                return

            projects = registry.list_enabled()
            if not projects:
                await update.message.reply_text(
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

            await update.message.reply_text(
                f"📁 <b>已配置项目</b>\n\n{project_list}",
                parse_mode="HTML",
            )
            return

        # Get directories in approved directory (these are "projects")
        projects = []
        for item in sorted(settings.approved_directory.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                projects.append(item.name)

        if not projects:
            await update.message.reply_text(
                "📁 <b>未找到项目</b>\n\n"
                "授权目录中没有子目录。\n"
                "创建一些目录来组织你的项目吧!"
            )
            return

        # Create inline keyboard with project buttons
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
                InlineKeyboardButton("🏠 返回根目录", callback_data="cd:/"),
                InlineKeyboardButton("🔄 刷新", callback_data="action:show_projects"),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard)

        project_list = "\n".join([f"• <code>{project}/</code>" for project in projects])

        await update.message.reply_text(
            f"📁 <b>可用项目</b>\n\n" f"{project_list}\n\n" f"点击下方项目即可进入:",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    except Exception as e:
        await update.message.reply_text(f"❌ 加载项目时出错: {str(e)}")
        logger.error("Error in show_projects command", error=str(e))


async def session_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]

    # Get session info
    claude_session_id = context.user_data.get("claude_session_id")
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = current_dir.relative_to(settings.approved_directory)

    # Get rate limiter info if available
    rate_limiter = context.bot_data.get("rate_limiter")
    usage_info = ""
    if rate_limiter:
        try:
            user_status = rate_limiter.get_user_status(user_id)
            cost_usage = user_status.get("cost_usage", {})
            current_cost = cost_usage.get("current", 0.0)
            cost_limit = cost_usage.get("limit", settings.claude_max_cost_per_user)
            cost_percentage = (current_cost / cost_limit) * 100 if cost_limit > 0 else 0

            usage_info = f"💰 用量: ${current_cost:.2f} / ${cost_limit:.2f} ({cost_percentage:.0f}%)\n"
        except Exception:
            usage_info = "💰 用量: <i>无法获取</i>\n"

    # Check if there's a resumable session from the database
    resumable_info = ""
    if not claude_session_id:
        claude_integration: ClaudeIntegration = context.bot_data.get(
            "claude_integration"
        )
        if claude_integration:
            existing = await claude_integration._find_resumable_session(
                user_id, current_dir
            )
            if existing:
                resumable_info = (
                    f"🔄 可恢复: <code>{existing.session_id}</code> "
                    f"({existing.message_count} 条消息)"
                )

    # Format status message
    status_lines = [
        "📊 <b>Session 状态</b>",
        "",
        f"📂 目录: <code>{relative_path}/</code>",
        f"🤖 Claude Session: {'✅ 活跃' if claude_session_id else '❌ 无'}",
        usage_info.rstrip(),
        f"🕐 最后更新: {update.message.date.strftime('%H:%M:%S UTC')}",
    ]

    if claude_session_id:
        status_lines.append(f"🆔 Session ID: <code>{claude_session_id}</code>")
    elif resumable_info:
        status_lines.append(resumable_info)
        status_lines.append("💡 下次发消息时将自动恢复 session")

    # Add action buttons
    keyboard = []
    if claude_session_id:
        keyboard.append(
            [
                InlineKeyboardButton("🔄 继续", callback_data="action:continue"),
                InlineKeyboardButton(
                    "🆕 新建 Session", callback_data="action:new_session"
                ),
            ]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🆕 开始 Session", callback_data="action:new_session"
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton("📤 导出", callback_data="action:export"),
            InlineKeyboardButton("🔄 刷新", callback_data="action:refresh_status"),
        ]
    )

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "\n".join(status_lines), parse_mode="HTML", reply_markup=reply_markup
    )


async def export_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /export command."""
    update.effective_user.id
    features = context.bot_data.get("features")

    # Check if session export is available
    session_exporter = features.get_session_export() if features else None

    if not session_exporter:
        await update.message.reply_text(
            "📤 <b>导出 Session</b>\n\n"
            "Session 导出功能暂不可用。\n\n"
            "<b>计划功能:</b>\n"
            "• 导出对话历史\n"
            "• 保存 session 状态\n"
            "• 分享对话\n"
            "• 创建 session 备份"
        )
        return

    # Get current session
    claude_session_id = context.user_data.get("claude_session_id")

    if not claude_session_id:
        await update.message.reply_text(
            "❌ <b>无活跃 Session</b>\n\n"
            "没有可导出的活跃 Claude session。\n\n"
            "<b>你可以:</b>\n"
            "• 使用 <code>/new</code> 开始新 session\n"
            "• 使用 <code>/continue</code> 继续现有 session\n"
            "• 使用 <code>/status</code> 查看状态"
        )
        return

    # Create export format selection keyboard
    keyboard = [
        [
            InlineKeyboardButton("📝 Markdown", callback_data="export:markdown"),
            InlineKeyboardButton("🌐 HTML", callback_data="export:html"),
        ],
        [
            InlineKeyboardButton("📋 JSON", callback_data="export:json"),
            InlineKeyboardButton("❌ 取消", callback_data="export:cancel"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "📤 <b>导出 Session</b>\n\n"
        f"准备导出 session: <code>{claude_session_id}</code>\n\n"
        "<b>选择导出格式:</b>",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def end_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /end command to terminate the current session."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]

    # Check if there's an active session
    claude_session_id = context.user_data.get("claude_session_id")

    if not claude_session_id:
        await update.message.reply_text(
            "ℹ️ <b>无活跃 Session</b>\n\n"
            "没有可结束的活跃 Claude session。\n\n"
            "<b>你可以:</b>\n"
            "• 使用 <code>/new</code> 开始新 session\n"
            "• 使用 <code>/status</code> 查看 session 状态\n"
            "• 发送任意消息开始对话"
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
            InlineKeyboardButton("🆕 新建 Session", callback_data="action:new_session"),
            InlineKeyboardButton("📁 切换项目", callback_data="action:show_projects"),
        ],
        [
            InlineKeyboardButton("📊 状态", callback_data="action:status"),
            InlineKeyboardButton("❓ 帮助", callback_data="action:help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "✅ <b>Session 已结束</b>\n\n"
        f"你的 Claude session 已终止。\n\n"
        f"<b>当前状态:</b>\n"
        f"• 目录: <code>{relative_path}/</code>\n"
        f"• Session: 无\n"
        f"• 可接受新命令\n\n"
        f"<b>下一步:</b>\n"
        f"• 使用 <code>/new</code> 开始新 session\n"
        f"• 使用 <code>/status</code> 查看状态\n"
        f"• 发送任意消息开始新对话",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )

    logger.info("Session ended by user", user_id=user_id, session_id=claude_session_id)


async def quick_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /actions command to show quick actions."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    features = context.bot_data.get("features")

    if not features or not features.is_enabled("quick_actions"):
        await update.message.reply_text(
            "❌ <b>快捷操作已禁用</b>\n\n"
            "快捷操作功能未启用。\n"
            "请联系管理员启用此功能。"
        )
        return

    # Get current directory
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        quick_action_manager = features.get_quick_actions()
        if not quick_action_manager:
            await update.message.reply_text(
                "❌ <b>快捷操作不可用</b>\n\n" "快捷操作服务不可用。"
            )
            return

        # Get context-aware actions
        now = datetime.now(UTC)
        actions = await quick_action_manager.get_suggestions(
            session=SessionModel(
                session_id="",  # ephemeral session for quick actions context
                user_id=user_id,
                project_path=str(current_dir),
                created_at=now,
                last_used=now,
            )
        )

        if not actions:
            await update.message.reply_text(
                "🤖 <b>无可用操作</b>\n\n"
                "当前上下文没有可用的快捷操作。\n\n"
                "<b>试试:</b>\n"
                "• 使用 <code>/cd</code> 进入项目目录\n"
                "• 创建一些代码文件\n"
                "• 使用 <code>/new</code> 开始 Claude session"
            )
            return

        # Create inline keyboard
        keyboard = quick_action_manager.create_inline_keyboard(actions, max_columns=2)

        relative_path = current_dir.relative_to(settings.approved_directory)
        await update.message.reply_text(
            f"⚡ <b>快捷操作</b>\n\n"
            f"📂 上下文: <code>{relative_path}/</code>\n\n"
            f"选择要执行的操作:",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    except Exception as e:
        await update.message.reply_text(f"❌ <b>加载操作出错</b>\n\n{str(e)}")
        logger.error("Error in quick_actions command", error=str(e), user_id=user_id)


async def git_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /git command to show git repository information."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]
    features = context.bot_data.get("features")

    if not features or not features.is_enabled("git"):
        await update.message.reply_text(
            "❌ <b>Git 集成已禁用</b>\n\n"
            "Git 集成功能未启用。\n"
            "请联系管理员启用此功能。"
        )
        return

    # Get current directory
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        git_integration = features.get_git_integration()
        if not git_integration:
            await update.message.reply_text(
                "❌ <b>Git 集成不可用</b>\n\n" "Git 集成服务不可用。"
            )
            return

        # Check if current directory is a git repository
        if not (current_dir / ".git").exists():
            await update.message.reply_text(
                f"📂 <b>不是 Git 仓库</b>\n\n"
                f"当前目录 <code>{current_dir.relative_to(settings.approved_directory)}/</code> 不是 git 仓库。\n\n"
                f"<b>选项:</b>\n"
                f"• 使用 <code>/cd</code> 进入 git 仓库\n"
                f"• 初始化新仓库 (让 Claude 帮忙)\n"
                f"• 克隆现有仓库 (让 Claude 帮忙)"
            )
            return

        # Get git status
        git_status = await git_integration.get_status(current_dir)

        # Format status message
        relative_path = current_dir.relative_to(settings.approved_directory)
        status_message = "🔗 <b>Git 仓库状态</b>\n\n"
        status_message += f"📂 目录: <code>{relative_path}/</code>\n"
        status_message += f"🌿 分支: <code>{git_status.branch}</code>\n"

        if git_status.ahead > 0:
            status_message += f"⬆️ 领先: {git_status.ahead} 个提交\n"
        if git_status.behind > 0:
            status_message += f"⬇️ 落后: {git_status.behind} 个提交\n"

        # Show file changes
        if not git_status.is_clean:
            status_message += "\n<b>变更:</b>\n"
            if git_status.modified:
                status_message += f"📝 已修改: {len(git_status.modified)} 个文件\n"
            if git_status.added:
                status_message += f"➕ 已添加: {len(git_status.added)} 个文件\n"
            if git_status.deleted:
                status_message += f"➖ 已删除: {len(git_status.deleted)} 个文件\n"
            if git_status.untracked:
                status_message += f"❓ 未跟踪: {len(git_status.untracked)} 个文件\n"
        else:
            status_message += "\n✅ 工作目录干净\n"

        # Create action buttons
        keyboard = [
            [
                InlineKeyboardButton("📊 查看 Diff", callback_data="git:diff"),
                InlineKeyboardButton("📜 查看日志", callback_data="git:log"),
            ],
            [
                InlineKeyboardButton("🔄 刷新", callback_data="git:status"),
                InlineKeyboardButton("📁 文件", callback_data="action:ls"),
            ],
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            status_message, parse_mode="HTML", reply_markup=reply_markup
        )

    except Exception as e:
        await update.message.reply_text(f"❌ <b>Git 错误</b>\n\n{str(e)}")
        logger.error("Error in git_command", error=str(e), user_id=user_id)


def _build_provider_keyboard(pm) -> InlineKeyboardMarkup:
    """Build inline keyboard for provider selection."""
    profiles = pm.list_profiles()
    active_name = pm.get_active_name() or ""
    buttons = []
    for p in profiles:
        label = f"✅ {p.name}" if p.name == active_name else p.name
        buttons.append(InlineKeyboardButton(label, callback_data=f"provider:{p.name}"))
    return InlineKeyboardMarkup([buttons])


async def provider_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /provider command — list or switch API providers."""
    pm = context.bot_data.get("provider_manager")
    if not pm:
        await update.message.reply_text("Provider 管理器不可用。")
        return

    args = update.message.text.split()[1:] if update.message.text else []

    if not args:
        profiles = pm.list_profiles()
        active_name = pm.get_active_name() or "none"
        model = pm.get_effective_model() or "default"
        lines = [f"<b>Provider:</b> {active_name}  ·  <b>模型:</b> {model}"]
        for p in profiles:
            marker = "➡️ " if p.name == active_name else "  "
            lines.append(f"{marker}<code>{p.name}</code>")
        text = "\n".join(lines)
        keyboard = _build_provider_keyboard(pm)
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
        return

    name = args[0].strip()
    try:
        profile = pm.switch_profile(name)
        model = pm.get_effective_model() or "default"
        await update.message.reply_text(
            f"已切换到 <b>{profile.name}</b>  ·  模型: <code>{model}</code>\n"
            f"下次请求时生效。",
            parse_mode="HTML",
        )
    except KeyError as e:
        await update.message.reply_text(str(e))


async def handle_provider_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle inline button press for provider switching (classic mode)."""
    query = update.callback_query
    await query.answer()

    pm = context.bot_data.get("provider_manager")
    if not pm:
        await query.edit_message_text("Provider 管理器不可用。")
        return

    data = query.data  # "provider:<name>"
    name = data.split(":", 1)[1] if ":" in data else ""
    try:
        profile = pm.switch_profile(name)
        model = pm.get_effective_model() or "default"
        await query.edit_message_text(
            f"✅ 已切换到 <b>{profile.name}</b>  ·  模型: <code>{model}</code>\n"
            f"下次请求时生效。",
            parse_mode="HTML",
        )
    except KeyError as e:
        await query.edit_message_text(str(e))


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /model command — show or override the model (supports per-role config)."""
    from ...config.providers import _parse_context_suffix, _VALID_ROLES

    pm = context.bot_data.get("provider_manager")
    if not pm:
        await update.message.reply_text("Provider 管理器不可用。")
        return

    args = update.message.text.split()[1:] if update.message.text else []

    # No args: show current config
    if not args:
        model = pm.get_effective_model() or "default"
        source = pm.get_model_source()
        lines = [f"模型: <code>{model}</code> ({source})"]
        roles = pm.get_role_models()
        for role in _VALID_ROLES:
            rm = roles.get(role)
            if rm:
                lines.append(f"  {role}: <code>{rm}</code>")
            else:
                lines.append(f"  {role}: —")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    first = args[0].strip().lower()

    # /model reset: clear all overrides
    if first == "reset":
        pm.set_model_override(None)
        for role in _VALID_ROLES:
            pm.set_role_model(role, None)
        model = pm.get_effective_model() or "default"
        await update.message.reply_text(
            f"所有覆盖已清除。使用: <code>{model}</code>", parse_mode="HTML"
        )
        return

    # /model <role> [model|reset]: per-role config
    role = pm.resolve_role(first)
    if role:
        if len(args) < 2:
            await update.message.reply_text(f"用法: /model {first} <model|reset>")
            return
        value = args[1].strip()
        if value.lower() == "reset":
            pm.set_role_model(role, None)
            await update.message.reply_text(
                f"已清除 <b>{role}</b> 角色模型。", parse_mode="HTML"
            )
        else:
            pm.set_role_model(role, value)
            await update.message.reply_text(
                f"<b>{role}</b> 角色模型已设置为: <code>{value}</code>\n"
                f"下次请求时生效。",
                parse_mode="HTML",
            )
        return

    # /model <name>: set default model override
    pm.set_model_override(first)
    await update.message.reply_text(
        f"模型覆盖已设置: <code>{first}</code>\n"
        f"Provider: {pm.get_active_name() or 'default'}\n"
        f"下次请求时生效。",
        parse_mode="HTML",
    )


async def sessions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/sessions` — list current-directory sessions in a paginated browser.

    Classic-mode entry point. Same logic also bound in orchestrator for
    agentic mode.
    """
    from ..features.session_browser import list_sessions_view

    user_id = update.effective_user.id
    storage = context.bot_data["storage"].sessions
    settings: Settings = context.bot_data["settings"]
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    current_directory = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    text, kb = await list_sessions_view(
        storage=storage,
        user_id=user_id,
        project_path=str(current_directory),
        page=0,
    )
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    if audit_logger:
        await audit_logger.log_session_event(
            user_id=user_id,
            action="sessions_command",
            success=True,
            details={"directory": str(current_directory)},
        )


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /restart command - gracefully restart the bot process.

    Sends a confirmation message then triggers SIGTERM so systemd
    (or any process manager with restart-on-exit) brings the bot back up.

    Auth: protected by the auth middleware (group -2) which raises
    ``ApplicationHandlerStop`` for unauthenticated users before any
    handler in group 10 runs.  No per-handler check is needed.
    """
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    user_id = update.effective_user.id

    await update.message.reply_text(
        "🔄 <b>正在重启机器人...</b>\n\n马上回来。",
        parse_mode="HTML",
    )

    if audit_logger:
        await audit_logger.log_command(user_id, "restart", [], True)

    logger.info("Restart requested via /restart command", user_id=user_id)

    # Write a marker file so the new process can send a "restart complete" message.
    marker = {
        "chat_id": update.effective_chat.id,
        "user_id": user_id,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    marker_path = Path.home() / ".claude-tg-bot" / "restart_marker.json"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")

    if sys.platform == "win32":
        # Windows: no systemd, so a detached helper waits for this process to
        # exit cleanly, then starts the hidden VBS launcher. Keep the helper as
        # a small .cmd file so restart failures leave breadcrumbs on disk.
        import subprocess

        current_pid = os.getpid()
        state_dir = Path.home() / ".claude-tg-bot"
        vbs_path = state_dir / "start-bot.vbs"
        if vbs_path.exists():
            helper_path = state_dir / "restart-helper.vbs"
            helper_log_path = state_dir / "restart-helper.log"
            helper_path.write_text(
                "\r\n".join(
                    [
                        "Option Explicit",
                        "Dim args, parentPid, launcher, logPath",
                        "Dim shell, fso, svc, procs, deadline",
                        "Set args = WScript.Arguments",
                        "parentPid = args.Item(0)",
                        "launcher = args.Item(1)",
                        "logPath = args.Item(2)",
                        'Set shell = CreateObject("WScript.Shell")',
                        'Set fso = CreateObject("Scripting.FileSystemObject")',
                        "",
                        "Sub WriteLog(message)",
                        "  Dim file",
                        "  Set file = fso.OpenTextFile(logPath, 8, True)",
                        '  file.WriteLine Now & " " & message',
                        "  file.Close",
                        "End Sub",
                        "",
                        'WriteLog "helper started parent=" & parentPid & " launcher=" & launcher',
                        'Set svc = GetObject("winmgmts:\\\\.\\root\\cimv2")',
                        'deadline = DateAdd("s", 90, Now)',
                        "Do",
                        '  Set procs = svc.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE ProcessId=" & parentPid)',
                        "  If procs.Count = 0 Then Exit Do",
                        "  If Now >= deadline Then Exit Do",
                        "  WScript.Sleep 500",
                        "Loop",
                        'WriteLog "parent wait complete"',
                        "WScript.Sleep 2000",
                        "If Not fso.FileExists(launcher) Then",
                        '  WriteLog "launcher missing: " & launcher',
                        "  WScript.Quit 1",
                        "End If",
                        'WriteLog "starting launcher"',
                        'shell.Run "wscript.exe //B //NoLogo " & Chr(34) & launcher & Chr(34), 0, False',
                        'WriteLog "launcher dispatched"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            subprocess.Popen(
                [
                    "wscript.exe",
                    "//B",
                    "//NoLogo",
                    str(helper_path),
                    str(current_pid),
                    str(vbs_path),
                    str(helper_log_path),
                ],
                cwd=str(state_dir),
                close_fds=True,
                creationflags=subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        signal.raise_signal(signal.SIGTERM)
    else:
        # SIGTERM triggers the existing graceful-shutdown handler in main.py;
        # systemd Restart=always will bring the process back up.
        os.kill(os.getpid(), signal.SIGTERM)


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
