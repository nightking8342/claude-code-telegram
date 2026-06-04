import asyncio
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import ActiveRequest, MessageOrchestrator
from src.bot.update_processor import StopAwareUpdateProcessor
from src.claude.facade import StreamUpdate


@pytest.mark.asyncio
async def test_exit_plan_feedback_marks_text_as_priority():
    orchestrator = MessageOrchestrator(MagicMock(), {})
    user_id = 123
    future = asyncio.get_running_loop().create_future()
    orchestrator._pending_plan[user_id] = future
    StopAwareUpdateProcessor.plan_feedback_waiting.discard(user_id)

    query = MagicMock()
    query.data = f"plan:{user_id}:exit_feedback"
    query.from_user.id = user_id
    query.answer = AsyncMock()
    query.edit_message_caption = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    try:
        await orchestrator._handle_plan_callback(update, MagicMock())

        assert user_id in StopAwareUpdateProcessor.plan_feedback_waiting
        assert orchestrator._pending_plan_waiting[user_id]["future"] is future
    finally:
        StopAwareUpdateProcessor.plan_feedback_waiting.discard(user_id)


@pytest.mark.asyncio
async def test_plan_feedback_text_resolves_future_and_clears_priority():
    orchestrator = MessageOrchestrator(MagicMock(), {})
    user_id = 123
    future = asyncio.get_running_loop().create_future()

    query = MagicMock()
    query.edit_message_text = AsyncMock()
    orchestrator._pending_plan_waiting[user_id] = {
        "future": future,
        "query": query,
    }
    StopAwareUpdateProcessor.plan_feedback_waiting.add(user_id)

    update = MagicMock()
    update.effective_user.id = user_id
    update.message.text = "Please simplify the implementation plan."
    update.message.reply_text = AsyncMock()

    await orchestrator.agentic_text(update, MagicMock())

    assert future.result() == {
        "action": "feedback",
        "feedback": "Please simplify the implementation plan.",
    }
    assert user_id not in StopAwareUpdateProcessor.plan_feedback_waiting
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_plan_feedback_text_switches_progress_message():
    orchestrator = MessageOrchestrator(MagicMock(), {})
    user_id = 123
    future = asyncio.get_running_loop().create_future()
    old_progress = AsyncMock()
    new_progress = AsyncMock()
    active = ActiveRequest(user_id=user_id, progress_msg=old_progress)
    orchestrator._active_requests[user_id] = active
    orchestrator._pending_plan_waiting[user_id] = {
        "future": future,
        "query": None,
    }
    StopAwareUpdateProcessor.plan_feedback_waiting.add(user_id)

    update = MagicMock()
    update.effective_user.id = user_id
    update.message.text = "Please simplify the implementation plan."
    update.message.message_id = 456
    update.message.reply_text = AsyncMock(return_value=new_progress)

    await orchestrator.agentic_text(update, MagicMock())

    assert await active.current_progress_msg() is new_progress
    assert await active.all_progress_messages() == [old_progress, new_progress]
    update.message.reply_text.assert_any_await(
        "✏️ 已收到修改意见，正在让 Claude 调整计划...",
        reply_to_message_id=456,
        reply_markup=ANY,
    )


@pytest.mark.asyncio
async def test_exit_plan_approve_switches_progress_message():
    orchestrator = MessageOrchestrator(MagicMock(), {})
    user_id = 123
    future = asyncio.get_running_loop().create_future()
    old_progress = AsyncMock()
    new_progress = AsyncMock()
    active = ActiveRequest(user_id=user_id, progress_msg=old_progress)
    orchestrator._active_requests[user_id] = active
    orchestrator._pending_plan[user_id] = future

    plan_message = MagicMock()
    plan_message.message_id = 789
    plan_message.reply_text = AsyncMock(return_value=new_progress)

    query = MagicMock()
    query.data = f"plan:{user_id}:exit_approve"
    query.from_user.id = user_id
    query.message = plan_message
    query.answer = AsyncMock()
    query.edit_message_caption = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    await orchestrator._handle_plan_callback(update, MagicMock())

    assert future.result() == {"action": "approve"}
    assert await active.current_progress_msg() is new_progress
    plan_message.reply_text.assert_awaited_once()
    assert plan_message.reply_text.await_args.kwargs["reply_to_message_id"] == 789


@pytest.mark.asyncio
async def test_stream_callback_edits_current_progress_message():
    orchestrator = MessageOrchestrator(MagicMock(), {})
    old_progress = AsyncMock()
    new_progress = AsyncMock()
    active = ActiveRequest(user_id=123, progress_msg=old_progress)
    await active.switch_progress_msg(new_progress)
    tool_log = []

    callback = orchestrator._make_stream_callback(
        verbose_level=1,
        progress_msg=old_progress,
        tool_log=tool_log,
        start_time=0.0,
        active_request=active,
    )

    await callback(
        StreamUpdate(
            type="assistant",
            content="Working on it",
            tool_calls=[],
            metadata={},
        )
    )

    old_progress.edit_text.assert_not_awaited()
    new_progress.edit_text.assert_awaited_once()
