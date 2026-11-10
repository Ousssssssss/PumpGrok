"""Top-holder concentration snapshot over :class:`RpcClient` (STRUCT-002).

The default call is the retired ``holder_check`` oracle ``holder_snapshot``,
including the sampled-only disclosure ``note`` byte-identical. A non-empty
``exclude`` drops those token accounts (bonding curve / pool) before the
percentages. Uses ``getTokenLargestAccounts`` on public RPC (R47).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pumpgrok.core.errors import DataStale
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError

# Payload shapes the holder parser cannot walk; each becomes a failed check.
_MALFORMED = (ArithmeticError, AttributeError, KeyError, TypeError, ValueError)

_NOTE_SAMPLED = (
    "Percentages are of the sampled largest accounts only. "
    "LP and locked tokens are not auto-excluded. "
    "Cross-check with Solscan / gmgn for production decisions."
)
_NOTE_EXCLUDED = (
    "Percentages are of the sampled largest accounts only, "
    "after excluding the supplied bonding-curve or pool accounts. "
    "Cross-check with Solscan / gmgn for production decisions."
)


def _amount_units(item: dict[str, Any]) -> int | None:
    """Base-unit amount from a largest-accounts row, or None when absent."""
    raw = item.get("amount")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdecimal():
        return int(raw)
    return None


def _exclude_set(exclude: Sequence[str] | None) -> set[str]:
    if not exclude:
        return set()
    return {item for item in exclude if isinstance(item, str) and item}


def _percentages(holders: list[dict[str, Any]], total_ui: float) -> tuple[float, float, float]:
    """Top-1/5/10 shares of ``total_ui``, matching the oracle's rounded percents."""
    if not holders or not total_ui:
        return 0.0, 0.0, 0.0
    top1 = holders[0]["uiAmount"] / total_ui
    top5 = sum(holder["uiAmount"] for holder in holders[:5]) / total_ui
    top10 = sum(holder["uiAmount"] for holder in holders[:10]) / total_ui
    return round(top1 * 100, 2), round(top5 * 100, 2), round(top10 * 100, 2)


def _excluded_units(items: list[dict[str, Any]]) -> int | None:
    total = 0
    for item in items:
        units = _amount_units(item)
        if units is None:
            return None
        total += units
    return total


def _snapshot(
    mint: str, value: list[Any], limit: int, exclude: Sequence[str] | None
) -> dict[str, Any]:
    """Concentration over ``value[:limit]``, optionally dropping ``exclude`` accounts."""
    dropped_addresses = _exclude_set(exclude)
    holders: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    total_ui = 0.0
    for item in value[:limit]:
        ui = float(item.get("uiAmount") or 0)
        address = item.get("address")
        if dropped_addresses and isinstance(address, str) and address in dropped_addresses:
            dropped.append(item)
            continue
        total_ui += ui
        row: dict[str, Any] = {
            "address": address,
            "uiAmount": ui,
            "decimals": item.get("decimals"),
        }
        if dropped_addresses:
            row["amount"] = _amount_units(item)
        holders.append(row)
    top1, top5, top10 = _percentages(holders, total_ui)
    out: dict[str, Any] = {
        "ok": True,
        "mint": mint,
        "holderCountReturned": len(holders),
        "totalUiAmountSampled": total_ui,
        "top1Pct": top1,
        "top5Pct": top5,
        "top10Pct": top10,
        "holders": holders,
        "note": _NOTE_EXCLUDED if dropped_addresses else _NOTE_SAMPLED,
    }
    if dropped_addresses:
        out["excluded"] = sorted(
            address for item in dropped if isinstance(address := item.get("address"), str)
        )
        out["excludedAmountUnits"] = _excluded_units(dropped)
    return out


async def holder_snapshot(
    mint: str,
    rpc: RpcClient,
    limit: int = 20,
    exclude: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Return concentration stats over the largest accounts sample.

    ``exclude`` lists token-account addresses (the bonding-curve or pool
    account) to drop before the percentages. The default call does not
    exclude anything and keeps the oracle note.
    """
    try:
        result = await rpc.call("getTokenLargestAccounts", [mint])
        value = result.get("value") if isinstance(result, dict) else result
        if not value:
            return {"ok": False, "error": "No largest accounts returned", "mint": mint}
        return _snapshot(mint, value, limit, exclude)
    except (RpcError, DataStale) as exc:
        return {"ok": False, "error": str(exc), "mint": mint}
    except _MALFORMED:
        return {"ok": False, "error": "holder check failed", "mint": mint}
