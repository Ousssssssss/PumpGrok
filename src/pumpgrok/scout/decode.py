"""Pull pump.fun create events out of a ``getTransaction`` result.

The logs source is the ``Program data:`` lines the pump program emits.
:func:`pumpgrok.tools.onchain.pump_events.decode_program_data` owns the
layout. The bonding-curve token account is the post-token balance whose
owner is the create event's bonding-curve address; that account is what
holder concentration must drop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pumpgrok.tools.onchain.pump_events import (
    CreateEvent,
    EventDecodeError,
    UnknownEventError,
    decode_program_data,
)

_PROGRAM_DATA = "Program data: "


@dataclass(frozen=True, slots=True)
class CreateHit:
    """One decoded create plus the curve token accounts seen in that transaction."""

    event: CreateEvent
    signature: str
    excluded_accounts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParsedTx:
    """Creates found in one transaction, plus how many payloads failed to decode."""

    creates: tuple[CreateHit, ...]
    known_events: int
    decode_errors: int
    unknown_events: int
    kind: str


def account_keys(tx: dict[str, Any]) -> list[str]:
    """Static message keys plus address-lookup-table loads, in account-index order."""
    message = tx.get("transaction", {})
    message = message.get("message", {}) if isinstance(message, dict) else {}
    keys: list[str] = []
    raw_keys = message.get("accountKeys", []) if isinstance(message, dict) else []
    if isinstance(raw_keys, list):
        for key in raw_keys:
            if isinstance(key, str):
                keys.append(key)
            elif isinstance(key, dict) and isinstance(key.get("pubkey"), str):
                keys.append(key["pubkey"])
    meta = tx.get("meta")
    loaded = meta.get("loadedAddresses") if isinstance(meta, dict) else None
    if isinstance(loaded, dict):
        for bucket in ("writable", "readonly"):
            extra = loaded.get(bucket)
            if isinstance(extra, list):
                keys.extend(item for item in extra if isinstance(item, str))
    return keys


def program_data_payloads(logs: object) -> list[str]:
    """Base64 bodies of ``Program data:`` log lines, in order."""
    if not isinstance(logs, list):
        return []
    payloads: list[str] = []
    for line in logs:
        if not isinstance(line, str):
            continue
        marker = line.find(_PROGRAM_DATA)
        if marker < 0:
            continue
        payload = line[marker + len(_PROGRAM_DATA) :].strip()
        if payload:
            payloads.append(payload)
    return payloads


def curve_token_accounts(tx: dict[str, Any], mint: str, bonding_curve: str) -> tuple[str, ...]:
    """Token accounts in ``tx`` owned by ``bonding_curve`` for ``mint``."""
    meta = tx.get("meta")
    if not isinstance(meta, dict):
        return ()
    keys = account_keys(tx)
    found: list[str] = []
    for side in ("postTokenBalances", "preTokenBalances"):
        balances = meta.get(side)
        if not isinstance(balances, list):
            continue
        for entry in balances:
            address = _balance_address(entry, keys, mint, bonding_curve)
            if address is not None and address not in found:
                found.append(address)
    return tuple(found)


def _balance_address(entry: object, keys: list[str], mint: str, bonding_curve: str) -> str | None:
    if not isinstance(entry, dict):
        return None
    if entry.get("mint") != mint or entry.get("owner") != bonding_curve:
        return None
    index = entry.get("accountIndex")
    if isinstance(index, int) and 0 <= index < len(keys):
        return keys[index]
    return None


def _empty(kind: str) -> ParsedTx:
    return ParsedTx((), 0, 0, 0, kind)


def parse_transaction(signature: str, tx: object) -> ParsedTx:
    """Decode create events in ``tx``. Non-creates stay in the counters."""
    if not isinstance(tx, dict):
        return _empty("missing_transaction")
    meta = tx.get("meta")
    if not isinstance(meta, dict) or meta.get("err") is not None:
        return _empty("failed_transaction")
    return _from_logs(signature, tx, program_data_payloads(meta.get("logMessages")))


def _from_logs(signature: str, tx: dict[str, Any], payloads: list[str]) -> ParsedTx:
    creates: list[CreateHit] = []
    known = 0
    errors = 0
    unknown = 0
    for payload in payloads:
        event, flag = _one_payload(payload)
        if flag == "unknown":
            unknown += 1
            continue
        if flag == "error":
            errors += 1
            continue
        known += 1
        if isinstance(event, CreateEvent):
            excluded = curve_token_accounts(tx, event.mint, event.bonding_curve)
            creates.append(CreateHit(event, signature, excluded))
    return ParsedTx(tuple(creates), known, errors, unknown, "parsed")


def _one_payload(payload: str) -> tuple[object, str]:
    try:
        return decode_program_data(payload), "ok"
    except UnknownEventError:
        return None, "unknown"
    except EventDecodeError:
        return None, "error"
