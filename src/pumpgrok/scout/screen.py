"""Read-only safety screen for one pump.fun create.

Uses the desk's mint check, holder snapshot, and deployer history. Holder
concentration drops the bonding-curve token account and is scored against
supply excluding that account (top-10 ex-LP, warn 20%, fail 25%). Nothing
here signs, sends, or allocates a ticket.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from pumpgrok.config.settings import SafetyThresholds
from pumpgrok.core.errors import DataStale
from pumpgrok.core.units import lamports_to_sol
from pumpgrok.replay.clock import Clock
from pumpgrok.scout.decode import CreateHit
from pumpgrok.tools._common import MINT_RE
from pumpgrok.tools.onchain.authorities import check_mint
from pumpgrok.tools.onchain.curve_layout import COMPLETION_SOL_LAMPORTS
from pumpgrok.tools.onchain.deployer_history import DeployerHistoryIn, run_deployer_history
from pumpgrok.tools.onchain.holders import holder_snapshot
from pumpgrok.tools.onchain.pump_events import CreateEvent
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError
from pumpgrok.tools.safety.checks.readers import check_deployer_history

_MINT_RE = re.compile(MINT_RE)


def _blind_concentration(reason: str, excluded: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "status": "blind",
        "pct": None,
        "reason": reason,
        "excluded": list(excluded),
        "sampled_top10_pct": None,
    }


def _visible(value: str, limit: int = 64) -> str:
    """Printable slice of untrusted on-chain text. Control characters are dropped."""
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned[:limit]


def _extension_label(item: object) -> str:
    if isinstance(item, str):
        return _visible(item, 40)
    if not isinstance(item, dict) or not item:
        return ""
    named = item.get("extension")
    if isinstance(named, str) and named:
        return _visible(named, 40)
    key = next(iter(item))
    return _visible(key, 40) if isinstance(key, str) else ""


def _extension_names(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(label for item in raw[:8] if (label := _extension_label(item)))


def _age_minutes(created: int, now: float) -> str | None:
    if created <= 0:
        return None
    delta = Decimal(str(now)) - Decimal(created)
    if delta < 0:
        delta = Decimal(0)
    minutes = (delta / Decimal(60)).quantize(Decimal("0.1"))
    return format(minutes, "f")


def _curve_text(event: CreateEvent) -> dict[str, Any]:
    """Virtual SOL from the create event. That event has no separate real-SOL field."""
    sol = lamports_to_sol(Decimal(event.virtual_sol_reserves))
    progress = Decimal(event.virtual_sol_reserves) / Decimal(COMPLETION_SOL_LAMPORTS)
    return {
        "liquidity_lamports": event.virtual_sol_reserves,
        "liquidity_sol": format(sol.normalize(), "f"),
        "progress": format(progress.quantize(Decimal("0.0001")), "f"),
        "basis": "virtual_sol_reserves",
    }


async def _authority_fields(mint: str, rpc: RpcClient) -> dict[str, Any]:
    result = await check_mint(mint, rpc)
    if not result.get("ok"):
        return {
            "mint_authority": "blind",
            "freeze_authority": "blind",
            "has_mint_authority": None,
            "has_freeze_authority": None,
            "extensions": [],
        }
    has_mint = bool(result.get("hasMintAuthority"))
    has_freeze = bool(result.get("hasFreezeAuthority"))
    return {
        "mint_authority": "set" if has_mint else "null",
        "freeze_authority": "set" if has_freeze else "null",
        "has_mint_authority": has_mint,
        "has_freeze_authority": has_freeze,
        "extensions": list(_extension_names(result.get("extensions"))),
    }


def _concentration_status(pct: Decimal, thresholds: SafetyThresholds) -> str:
    if pct >= Decimal(str(thresholds.top10_concentration_fail_pct)):
        return "fail"
    if pct >= Decimal(str(thresholds.top10_concentration_warn_pct)):
        return "warn"
    return "pass"


def _supply_pct(snap: dict[str, Any], supply: int) -> Decimal | None:
    excluded_units = snap.get("excludedAmountUnits")
    if not isinstance(excluded_units, int):
        return None
    top = 0
    for holder in snap.get("holders", [])[:10]:
        amount = holder.get("amount") if isinstance(holder, dict) else None
        if not isinstance(amount, int):
            return None
        top += amount
    base = supply - excluded_units
    if base <= 0:
        return None
    return (Decimal(top) / Decimal(base) * Decimal(100)).quantize(Decimal("0.01"))


async def _supply_units(mint: str, rpc: RpcClient) -> int | None:
    try:
        result = await rpc.call("getTokenSupply", [mint])
    except (RpcError, DataStale):
        return None
    try:
        return int(result["value"]["amount"])
    except (KeyError, TypeError, ValueError):
        return None


async def _concentration(
    mint: str,
    rpc: RpcClient,
    excluded: tuple[str, ...],
    thresholds: SafetyThresholds,
) -> dict[str, Any]:
    if not excluded:
        return _blind_concentration("bonding-curve token account was not in the create transaction")
    snap = await holder_snapshot(mint, rpc, exclude=excluded)
    if not snap.get("ok"):
        return _blind_concentration("holder snapshot failed", excluded)
    found = [item for item in (snap.get("excluded") or []) if isinstance(item, str)]
    if not set(found).intersection(excluded):
        return _blind_concentration(
            "bonding-curve token account was not in the largest-account sample",
            excluded,
        )
    supply = await _supply_units(mint, rpc)
    pct = _supply_pct(snap, supply) if supply is not None else None
    if pct is None:
        return _blind_concentration("supply or holder amounts were unreadable", excluded)
    status = _concentration_status(pct, thresholds)
    return {
        "status": status,
        "pct": format(pct, "f"),
        "reason": f"top-10 holders ex-curve own {format(pct, 'f')}% of supply ex-curve",
        "excluded": sorted(found),
        "sampled_top10_pct": str(snap.get("top10Pct")),
    }


async def _deployer_fields(creator: str, rpc: RpcClient) -> dict[str, Any]:
    blind = {
        "tokens_seen": None,
        "status": "blind",
        "reason": "deployer history unavailable",
    }
    if not _MINT_RE.fullmatch(creator):
        return blind
    try:
        payload = await run_deployer_history(DeployerHistoryIn(creator=creator), rpc)
    except DataStale:
        return blind
    seen = payload.get("tokens_seen")
    if not isinstance(seen, int):
        return blind
    row = check_deployer_history(payload)
    return {"tokens_seen": seen, "status": row.status, "reason": row.reason}


def _signals(
    event: CreateEvent,
    auth: dict[str, Any],
    conc: dict[str, Any],
    dep: dict[str, Any],
    curve: dict[str, Any],
    *,
    age: str | None,
) -> list[str]:
    signals = [
        f"untrusted name: {_visible(event.name) or 'none'}",
        f"untrusted symbol: {_visible(event.symbol) or 'none'}",
        f"curve progress (virtual SOL / 85 SOL): {curve['progress']}",
        "liquidity basis: virtual_sol_reserves (create event has no real-SOL field)",
        f"mint authority: {auth['mint_authority']}",
        f"freeze authority: {auth['freeze_authority']}",
        f"top-10 ex-curve: {conc['status']} ({conc['reason']})",
        f"deployer: {dep['status']} ({dep['reason']})",
    ]
    if auth["extensions"]:
        signals.append("token extensions: " + ", ".join(auth["extensions"]))
    if age is None:
        signals.append("age: blind")
    return signals


def _why(signature: str, mint: str, auth: dict[str, Any], conc: dict[str, Any]) -> str:
    return (
        f"On-chain pump.fun create {signature} minted {mint}. "
        f"Mint authority is {auth['mint_authority']}; "
        f"top-10 ex-curve concentration is {conc['status']}. "
        "This lead does not clear risk and does not size a buy."
    )


def _lead_block(
    hit: CreateHit,
    auth: dict[str, Any],
    curve: dict[str, Any],
    age: str | None,
    signals: list[str],
    *,
    why: str,
    stamp: str,
) -> str:
    lines = [
        "LEAD-ID: (not allocated)",
        f"Mint: {hit.event.mint}",
        f"Source: {hit.signature}",
        f"Age: {age if age is not None else 'blind'}",
        f"Initial Liquidity: {curve['liquidity_sol']} SOL virtual reserves",
        f"Mint Authority (visible): {auth['mint_authority']}",
        "Early Signals:",
    ]
    lines.extend(f"- {item}" for item in signals)
    lines.extend(
        [
            f"Why this lead: {why}",
            "Hand-off to RISK: YES",
            f"UTC Timestamp: {stamp}",
        ]
    )
    return "\n".join(lines)


def _lead_dict(
    hit: CreateHit,
    auth: dict[str, Any],
    conc: dict[str, Any],
    dep: dict[str, Any],
    curve: dict[str, Any],
    *,
    age: str | None,
    signals: list[str],
    why: str,
    block: str,
    stamp: str,
) -> dict[str, Any]:
    event = hit.event
    return {
        "lead_id": None,
        "mint": event.mint,
        "source": hit.signature,
        "name_untrusted": _visible(event.name) or None,
        "symbol_untrusted": _visible(event.symbol) or None,
        "age_minutes": age,
        "initial_liquidity_lamports": curve["liquidity_lamports"],
        "initial_liquidity_sol": curve["liquidity_sol"],
        "liquidity_basis": curve["basis"],
        "mint_authority": auth["mint_authority"],
        "freeze_authority": auth["freeze_authority"],
        "has_mint_authority": auth["has_mint_authority"],
        "has_freeze_authority": auth["has_freeze_authority"],
        "extensions": auth["extensions"],
        "curve_progress": curve["progress"],
        "bonding_curve": event.bonding_curve,
        "creator": event.creator,
        "excluded_accounts": conc["excluded"],
        "top10_ex_curve_pct": conc["pct"],
        "top10_ex_curve_status": conc["status"],
        "sampled_top10_pct": conc["sampled_top10_pct"],
        "deployer_tokens_seen": dep["tokens_seen"],
        "deployer_status": dep["status"],
        "early_signals": signals,
        "why": why,
        "hand_off_to_risk": True,
        "utc_timestamp": stamp,
        "lead_block": block,
        "journal_note": {
            "source": "paper",
            "seat": "scout",
            "note_kind": "lead",
            "text": block,
            "refs": [hit.signature, event.mint],
        },
    }


async def screen_create(
    hit: CreateHit,
    rpc: RpcClient,
    clock: Clock,
    thresholds: SafetyThresholds,
) -> dict[str, Any]:
    """Screen ``hit`` and return one lead object. Per-check failures stay blind."""
    event = hit.event
    auth = await _authority_fields(event.mint, rpc)
    conc = await _concentration(event.mint, rpc, hit.excluded_accounts, thresholds)
    dep = await _deployer_fields(event.creator, rpc)
    curve = _curve_text(event)
    age = _age_minutes(event.timestamp, clock.now())
    signals = _signals(event, auth, conc, dep, curve, age=age)
    why = _why(hit.signature, event.mint, auth, conc)
    stamp = clock.utc_iso()
    block = _lead_block(hit, auth, curve, age, signals, why=why, stamp=stamp)
    return _lead_dict(
        hit,
        auth,
        conc,
        dep,
        curve,
        age=age,
        signals=signals,
        why=why,
        block=block,
        stamp=stamp,
    )
