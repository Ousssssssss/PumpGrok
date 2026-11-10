"""``scout discover``: on-chain pump.fun leads as JSON. Read-only."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from pumpgrok.cli.codes import EXIT_OK
from pumpgrok.cli.errors import UsageError, _say, _tool_error
from pumpgrok.config.settings import Settings
from pumpgrok.core.errors import DataStale
from pumpgrok.scout.discover import MAX_LEADS, discover_leads


@contextmanager
def _quiet_http_access_logs() -> Iterator[None]:
    """httpx logs the request URL at INFO. Keep endpoint URLs out of this verb."""
    loggers = [logging.getLogger("httpx"), logging.getLogger("httpcore")]
    previous = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for logger, level in zip(loggers, previous, strict=True):
            logger.setLevel(level)


def scout_discover_verb(args: argparse.Namespace, settings: Settings) -> int:
    """Print screened leads. Does not allocate tickets or touch the ledger."""
    max_leads = args.max_leads
    if not isinstance(max_leads, int) or isinstance(max_leads, bool):
        raise UsageError(f"--max-leads must be an integer from 1 to {MAX_LEADS}")
    if max_leads < 1 or max_leads > MAX_LEADS:
        raise UsageError(f"--max-leads must be an integer from 1 to {MAX_LEADS}")
    try:
        with _quiet_http_access_logs():
            payload = asyncio.run(discover_leads(settings, max_leads=max_leads))
    except DataStale as exc:
        return _tool_error("scout discover", exc.code, exc.message)
    _say(sys.stdout, json.dumps(payload))
    return EXIT_OK
