import logging

import main  # noqa: F401 — importing configures logging


def test_httpx_request_logs_are_silenced():
    """httpx request lines carry the bot token in the URL (#352)."""
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
