"""Tests for application logging configuration."""

import logging

from src.main import setup_logging


def test_setup_logging_rotates_and_sanitizes_tokens(tmp_path, monkeypatch):
    log_file = tmp_path / "bot.log"
    token_url = (
        "https://api.telegram.org/bot123456789:"
        "abcdefghijklmnopqrstuvwxyzABCDE/getUpdates"
    )

    monkeypatch.setenv("LOG_TO_FILE", "1")
    monkeypatch.setenv("LOG_TO_CONSOLE", "0")
    monkeypatch.setenv("LOG_FILE", str(log_file))
    monkeypatch.setenv("LOG_MAX_BYTES", "220")
    monkeypatch.setenv("LOG_BACKUP_COUNT", "2")

    setup_logging(debug=False)
    logger = logging.getLogger("tests.logging")
    for _ in range(8):
        logger.warning("HTTP Request: POST %s", token_url)

    logging.shutdown()

    log_files = list(tmp_path.glob("bot.log*"))
    assert len(log_files) >= 2
    combined = "\n".join(path.read_text(encoding="utf-8") for path in log_files)
    assert "123456789:abcdefghijklmnopqrstuvwxyzABCDE" not in combined
    assert "https://api.telegram.org/bot<telegram-token>/getUpdates" in combined
