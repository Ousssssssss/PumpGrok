"""Discover new pump.fun mints by polling Solana JSON-RPC.

``getSignaturesForAddress`` on the pump.fun program, then ``getTransaction``
for each signature, decoded with the desk's pump event decoder. Each new
mint is screened by :func:`pumpgrok.scout.screen.screen_create`. Read-only:
no tickets, no ledger append, no signing.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from pumpgrok.config.settings import SafetyThresholds, Settings
from pumpgrok.core.errors import DataStale
from pumpgrok.replay.clock import Clock, WallClock
from pumpgrok.replay.rng import make_rng
from pumpgrok.scout.decode import parse_transaction
from pumpgrok.scout.screen import screen_create
from pumpgrok.tools._common import MINT_RE
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError

# Public pump.fun program. Not a secret.
PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

PAGE_SIZE = 1000
MAX_PAGES = 1
MAX_FETCHES = 80
BATCH_SIZE = 20
MAX_LEADS = 25

_TX_OPTS: dict[str, Any] = {
    "encoding": "json",
    "commitment": "confirmed",
    "maxSupportedTransactionVersion": 0,
}
_MINT_RE = re.compile(MINT_RE)
_SKIP_KEYS = (
    "failed_signature",
    "missing_transaction",
    "failed_transaction",
    "fetch_error",
    "decode_error",
    "unknown_event",
    "not_create",
    "duplicate_mint",
    "unverifiable_mint",
)
_LIMIT_NOTES = (
    "Polls getSignaturesForAddress on the pump.fun program, then getTransaction.",
    "No websocket. One page of up to 1000 signatures; failed signatures are not fetched.",
    "At most 80 successful transactions are fetched, in batches of 20.",
    "The program's recent seconds are mostly buys, so a run can return fewer leads.",
    "Public mainnet RPC often returns 429. A keyed provider finishes more of the window.",
    "Only the pinned CreateEvent discriminator is a lead. Unknown events are skipped.",
    "Curve progress is virtual SOL reserves over the 85 SOL completion threshold.",
    "The create event has no separate real-SOL field.",
    "The bonding-curve account is not re-read; that layout is still ASSUMED.",
    "Deployer history is a signature count. Rug adjudication is unwired.",
    "Transfer tax, honeypot, and bundler-cluster checks are not run.",
    "No ticket is allocated and nothing is appended to the ledger.",
)


def _skips() -> dict[str, int]:
    return dict.fromkeys(_SKIP_KEYS, 0)


def _bump(skipped: dict[str, int], reason: str) -> None:
    skipped[reason] = skipped.get(reason, 0) + 1


async def _signature_page(rpc: RpcClient, before: str | None) -> list[dict[str, Any]]:
    opts: dict[str, Any] = {"limit": PAGE_SIZE, "commitment": "confirmed"}
    if before is not None:
        opts["before"] = before
    try:
        result = await rpc.call("getSignaturesForAddress", [PUMP_PROGRAM_ID, opts])
    except RpcError as exc:
        raise DataStale("scout discover signatures failed") from exc
    if not isinstance(result, list):
        raise DataStale("scout discover signatures malformed")
    return [
        entry
        for entry in result
        if isinstance(entry, dict) and isinstance(entry.get("signature"), str)
    ]


def _skip_reason(parsed_kind: str, known: int, errors: int, unknown: int) -> str:
    if parsed_kind != "parsed":
        return parsed_kind
    if errors and known == 0:
        return "decode_error"
    if unknown and known == 0:
        return "unknown_event"
    return "not_create"


async def _screen_fetched(
    rpc: RpcClient,
    signature: str,
    tx: object,
    seen: set[str],
    thresholds: SafetyThresholds,
    *,
    clock: Clock,
    room: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    if tx is None:
        return [], ["missing_transaction"]
    parsed = parse_transaction(signature, tx)
    if parsed.kind != "parsed" or not parsed.creates:
        reason = _skip_reason(
            parsed.kind, parsed.known_events, parsed.decode_errors, parsed.unknown_events
        )
        return [], [reason]
    leads: list[dict[str, Any]] = []
    reasons: list[str] = []
    for hit in parsed.creates:
        if len(leads) >= room:
            break
        mint = hit.event.mint
        if mint in seen:
            reasons.append("duplicate_mint")
            continue
        if _MINT_RE.fullmatch(mint) is None:
            reasons.append("unverifiable_mint")
            continue
        seen.add(mint)
        leads.append(await screen_create(hit, rpc, clock, thresholds))
    return leads, reasons


def _select_successes(
    page: list[dict[str, Any]], skipped: dict[str, int]
) -> tuple[list[dict[str, Any]], int]:
    """Keep the newest successful signatures, up to the fetch cap."""
    chosen: list[dict[str, Any]] = []
    scanned = 0
    for entry in page:
        scanned += 1
        if entry.get("err") is not None:
            _bump(skipped, "failed_signature")
            continue
        chosen.append(entry)
        if len(chosen) >= MAX_FETCHES:
            break
    return chosen, scanned


async def _fetch_chunk(rpc: RpcClient, signatures: list[str]) -> list[Any]:
    """Batch-fetch transactions. A rejected batch falls back to one call each."""
    requests = [("getTransaction", [signature, _TX_OPTS]) for signature in signatures]
    try:
        return await rpc.call_batch(requests)
    except (RpcError, DataStale):
        rows: list[Any] = []
        for signature in signatures:
            try:
                rows.append(await rpc.call("getTransaction", [signature, _TX_OPTS]))
            except (RpcError, DataStale):
                rows.append(None)
        return rows


async def _screen_selected(
    rpc: RpcClient,
    chosen: list[dict[str, Any]],
    leads: list[dict[str, Any]],
    seen: set[str],
    skipped: dict[str, int],
    *,
    clock: Clock,
    thresholds: SafetyThresholds,
    max_leads: int,
) -> str | None:
    """Screen ``chosen`` in batches. Return a truncation reason, or None."""
    for start in range(0, len(chosen), BATCH_SIZE):
        if len(leads) >= max_leads:
            break
        chunk = chosen[start : start + BATCH_SIZE]
        signatures = [str(entry["signature"]) for entry in chunk]
        txs = await _fetch_chunk(rpc, signatures)
        for signature, tx in zip(signatures, txs, strict=True):
            if len(leads) >= max_leads:
                break
            found, reasons = await _screen_fetched(
                rpc,
                signature,
                tx,
                seen,
                thresholds,
                clock=clock,
                room=max_leads - len(leads),
            )
            leads.extend(found)
            for reason in reasons:
                _bump(skipped, reason)
    return None


async def scan_launches(
    rpc: RpcClient,
    *,
    max_leads: int,
    thresholds: SafetyThresholds | None = None,
) -> dict[str, Any]:
    """Scan a bounded signature window and return the JSON lead envelope."""
    if max_leads < 1 or max_leads > MAX_LEADS:
        raise DataStale(f"scout discover max_leads must be from 1 to {MAX_LEADS}")
    active = thresholds if thresholds is not None else SafetyThresholds()
    clock = rpc.clock
    skipped = _skips()
    leads: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = await _signature_page(rpc, None)
    chosen, scanned = _select_successes(page, skipped)
    truncated = await _screen_selected(
        rpc,
        chosen,
        leads,
        seen,
        skipped,
        clock=clock,
        thresholds=active,
        max_leads=max_leads,
    )
    return {
        "ok": True,
        "verb": "scout discover",
        "read_only": True,
        "tickets_created": 0,
        "source": "solana-rpc",
        "program": PUMP_PROGRAM_ID,
        "max_leads": max_leads,
        "scanned_signatures": scanned,
        "window_exhausted": len(leads) < max_leads,
        "truncated": truncated,
        "page_size": PAGE_SIZE,
        "max_pages": MAX_PAGES,
        "leads": leads,
        "skipped": skipped,
        "limits": list(_LIMIT_NOTES),
    }


async def discover_leads(
    settings: Settings,
    *,
    max_leads: int,
    rpc: RpcClient | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Run one discover pass against ``settings.rpc`` or an injected client."""
    if rpc is not None:
        return await scan_launches(rpc, max_leads=max_leads, thresholds=settings.safety.thresholds)
    active_clock = clock if clock is not None else WallClock()
    try:
        async with httpx.AsyncClient() as client:
            built = RpcClient(
                list(settings.rpc.endpoints),
                client,
                active_clock,
                make_rng(settings.replay.seed),
                api_key=settings.rpc.api_key,
            )
            return await scan_launches(
                built, max_leads=max_leads, thresholds=settings.safety.thresholds
            )
    except RpcError as exc:
        raise DataStale("scout discover rpc failed") from exc
