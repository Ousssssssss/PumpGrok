"""Holder snapshot drops an explicit bonding-curve or pool account.

The default call stays the oracle port (sampled note, no exclusion). Exclusion
is opt-in so characterization pins keep their numbers.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import httpx

from pumpgrok.replay.clock import FixtureClock
from pumpgrok.tools.onchain.holders import holder_snapshot
from pumpgrok.tools.onchain.rpc import RpcClient

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "http"
MINT = "MintMintMintMintMintMintMintMintMintMintMint"
CURVE = "Holder1111111111111111111111111111111111111"
_NOTE = (
    "Percentages are of the sampled largest accounts only. "
    "LP and locked tokens are not auto-excluded. "
    "Cross-check with Solscan / gmgn for production decisions."
)


def _rpc() -> RpcClient:
    body = json.loads((FIXTURES / "holders.json").read_text(encoding="utf-8"))["http"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    return RpcClient(
        ["https://rpc.invalid"],
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        FixtureClock(),
        rng=random.Random(7),
        max_attempts_per_endpoint=1,
    )


async def test_default_snapshot_keeps_the_oracle_note() -> None:
    result = await holder_snapshot(MINT, _rpc())
    assert result["top1Pct"] == 60.0
    assert result["note"] == _NOTE
    assert "amount" not in result["holders"][0]
    assert "excluded" not in result


async def test_exclude_drops_curve_before_percentages() -> None:
    result = await holder_snapshot(MINT, _rpc(), exclude=[CURVE])
    assert result["ok"] is True
    assert result["holderCountReturned"] == 4
    assert result["totalUiAmountSampled"] == 400.0
    assert result["top1Pct"] == 50.0
    assert result["top5Pct"] == 100.0
    assert result["top10Pct"] == 100.0
    assert result["excluded"] == [CURVE]
    assert result["excludedAmountUnits"] == 600_000_000_000
    assert CURVE not in [row["address"] for row in result["holders"]]
    assert result["holders"][0]["amount"] == 200_000_000_000
    assert "after excluding" in result["note"]
    assert "rpc.invalid" not in json.dumps(result)
