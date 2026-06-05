"""Small text-input flows for editing local Claude sessions."""

from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from src.claude.facade import ClaudeIntegration
from src.config.settings import Settings

from ..utils.html_format import escape_html

MAX_SESSION_TITLE_LEN = 80
MAX_SESSION_TAG_LEN = 40


async def handle_pending_session_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Handle a pending /sessions rename/tag text input.

    Returns True when the message was consumed and should not be sent to
    Claude as a normal prompt.
    """
    pending = context.user_data.get("session_edit_action")
    if not pending:
        return False

    message_text = (update.message.text or "").strip()
    session_id = pending.get("session_id")
    action = pending.get("action")
    project_path = pending.get("project_path")

    context.user_data.pop("session_edit_action", None)

    if not session_id or not action or not project_path:
        await update.message.reply_text("session 编辑状态已过期，请重新操作。")
        return True

    if message_text.lower() in {"/cancel", "cancel"}:
        await update.message.reply_text("已取消 session 编辑。")
        return True

    settings: Settings = context.bot_data["settings"]
    path = Path(project_path)
    try:
        path.resolve().relative_to(settings.approved_directory.resolve())
    except ValueError:
        await update.message.reply_text("该 session 所在项目不在允许访问的目录内。")
        return True

    if action == "rename":
        title = message_text[:MAX_SESSION_TITLE_LEN].strip()
        if not title:
            await update.message.reply_text("标题不能为空。")
            return True
        ok = await ClaudeIntegration.rename_sdk_session(session_id, path, title)
        if ok:
            await update.message.reply_text(
                f"session 已重命名为 <b>{escape_html(title)}</b>。",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                "重命名 session 失败，本地 transcript 可能不存在。"
            )
        return True

    if action == "tag":
        tag = message_text[:MAX_SESSION_TAG_LEN].strip()
        if not tag:
            await update.message.reply_text("标签不能为空。")
            return True
        ok = await ClaudeIntegration.tag_sdk_session(session_id, path, tag)
        if ok:
            await update.message.reply_text(
                f"session 标签已设为 <b>{escape_html(tag)}</b>。",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                "设置 session 标签失败，本地 transcript 可能不存在。"
            )
        return True

    await update.message.reply_text("未知的 session 编辑动作。")
    return True
