"""Offline scout discover: recorded JSON-RPC, no network."""

from __future__ import annotations

import base64
import json
import random
import struct
from decimal import Decimal
from typing import Any

import httpx
import pytest

from pumpgrok.core.errors import DataStale
from pumpgrok.replay.clock import FixtureClock
from pumpgrok.scout.discover import PUMP_PROGRAM_ID, scan_launches
from pumpgrok.tools.onchain.curve_layout import COMPLETION_SOL_LAMPORTS
from pumpgrok.tools.onchain.pump_events import CreateEvent
from pumpgrok.tools.onchain.rpc import RpcClient

TS = 1_700_000_000
MINT = "1" * 31 + "2"
CURVE = "1" * 31 + "21"
CREATOR = "1" * 32
ATA = "Ata" + "1" * 40
H1 = "H1" + "1" * 40
H2 = "H2" + "1" * 40
RAW = {
    MINT: bytes(31) + b"\x01",
    CURVE: bytes(31) + b"\x3a",
    CREATOR: bytes(32),
}
CREATE_DISC = bytes.fromhex("1b72a94ddeeb6376")
COMPLETE_DISC = bytes.fromhex("5f72619cd42e9808")
VIRTUAL_SOL = 30_500_000_000


def _u64(value: int) -> bytes:
    return struct.pack("<Q", value)


def _i64(value: int) -> bytes:
    return struct.pack("<q", value)


def _str(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def _pk(value: str) -> bytes:
    return RAW[value]


def _encode_create(event: CreateEvent) -> bytes:
    body = (
        _str(event.name)
        + _str(event.symbol)
        + _str(event.uri)
        + _pk(event.mint)
        + _pk(event.bonding_curve)
        + _pk(event.user)
        + _pk(event.creator)
        + _i64(event.timestamp)
        + _u64(event.virtual_token_reserves)
        + _u64(event.virtual_sol_reserves)
        + _u64(event.real_token_reserves)
        + _u64(event.token_total_supply)
    )
    return CREATE_DISC + body


def _program_data(raw: bytes) -> str:
    return "Program data: " + base64.b64encode(raw).decode("ascii")


def _create_event() -> CreateEvent:
    return CreateEvent(
        name="Pump\nToken",
        symbol="PMP",
        uri="https://example.invalid/meta.json?api-key=sekret",
        mint=MINT,
        bonding_curve=CURVE,
        user=CREATOR,
        creator=CREATOR,
        timestamp=TS,
        virtual_token_reserves=1_000,
        virtual_sol_reserves=VIRTUAL_SOL,
        real_token_reserves=1_000,
        token_total_supply=1_000,
    )


def _tx(
    logs: list[str], *, err: object = None, balances: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "blockTime": TS,
        "meta": {
            "err": err,
            "logMessages": logs,
            "postTokenBalances": balances or [],
        },
        "transaction": {"message": {"accountKeys": [CREATOR, MINT, ATA, CURVE]}},
    }


def _create_tx() -> dict[str, Any]:
    balances = [
        {
            "accountIndex": 2,
            "mint": MINT,
            "owner": CURVE,
            "uiTokenAmount": {"amount": "800", "decimals": 0},
        }
    ]
    return _tx([_program_data(_encode_create(_create_event()))], balances=balances)


def _complete_tx() -> dict[str, Any]:
    body = _pk(CREATOR) + _pk(MINT) + _pk(CURVE) + _i64(TS)
    return _tx([_program_data(COMPLETE_DISC + body)])


def _unknown_tx() -> dict[str, Any]:
    return _tx([_program_data(bytes.fromhex("0011223344556677") + b"\x00" * 4)])


def _holders() -> dict[str, Any]:
    return {
        "value": [
            {"address": ATA, "amount": "800", "decimals": 0, "uiAmount": 800.0},
            {"address": H1, "amount": "150", "decimals": 0, "uiAmount": 150.0},
            {"address": H2, "amount": "50", "decimals": 0, "uiAmount": 50.0},
        ]
    }


def _mint_account() -> dict[str, Any]:
    return {
        "value": {
            "data": {
                "parsed": {
                    "info": {
                        "mintAuthority": None,
                        "freezeAuthority": None,
                        "decimals": 6,
                        "supply": "1000",
                        "isInitialized": True,
                    }
                }
            }
        }
    }


def _rpc(handler: object) -> RpcClient:
    return RpcClient(
        ["https://rpc.invalid"],
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
        FixtureClock(start=TS + 120),
        rng=random.Random(7),
        max_attempts_per_endpoint=1,
    )


def _serve(
    request: httpx.Request,
    calls: list[tuple[str, Any]],
    result_of: Any,
) -> httpx.Response:
    """Answer one JSON-RPC call or a batch, recording each method."""
    body = json.loads(request.content)
    items = body if isinstance(body, list) else [body]
    rows = []
    for item in items:
        method = item["method"]
        params = item["params"]
        calls.append((method, params))
        rows.append(
            {"jsonrpc": "2.0", "id": item.get("id", 1), "result": result_of(method, params)}
        )
    if isinstance(body, list):
        return httpx.Response(200, json=rows)
    return httpx.Response(200, json=rows[0])


def _route(txs: dict[str, Any], calls: list[tuple[str, Any]]) -> object:
    def result_of(method: str, params: list[Any]) -> object:
        if method == "getSignaturesForAddress" and params[0] == PUMP_PROGRAM_ID:
            return [
                {"signature": "createSig", "err": None, "blockTime": TS},
                {"signature": "completeSig", "err": None, "blockTime": TS},
                {
                    "signature": "errSig",
                    "err": {"InstructionError": [0, "Custom"]},
                    "blockTime": TS,
                },
                {"signature": "unknownSig", "err": None, "blockTime": TS},
                {"signature": "metaErrSig", "err": None, "blockTime": TS},
            ]
        if method == "getSignaturesForAddress":
            return [{"signature": "depSig", "blockTime": TS}]
        if method == "getTransaction":
            return txs[params[0]]
        if method == "getAccountInfo":
            return _mint_account()
        if method == "getTokenLargestAccounts":
            return _holders()
        if method == "getTokenSupply":
            return {"value": {"amount": "1000", "decimals": 0}}
        raise AssertionError(method)

    def handler(request: httpx.Request) -> httpx.Response:
        return _serve(request, calls, result_of)

    return handler


def _progress() -> str:
    ratio = Decimal(VIRTUAL_SOL) / Decimal(COMPLETION_SOL_LAMPORTS)
    return format(ratio.quantize(Decimal("0.0001")), "f")


async def test_discover_screens_one_create_and_skips_the_rest() -> None:
    txs = {
        "createSig": _create_tx(),
        "completeSig": _complete_tx(),
        "unknownSig": _unknown_tx(),
        "metaErrSig": _tx(["Program log: ignored"], err={"InstructionError": [0, "Custom"]}),
    }
    calls: list[tuple[str, Any]] = []
    payload = await scan_launches(_rpc(_route(txs, calls)), max_leads=5)
    assert payload["ok"] is True
    assert payload["tickets_created"] == 0
    assert payload["read_only"] is True
    assert payload["program"] == PUMP_PROGRAM_ID
    assert payload["scanned_signatures"] == 5
    assert len(payload["leads"]) == 1
    lead = payload["leads"][0]
    assert lead["mint"] == MINT
    assert lead["source"] == "createSig"
    assert lead["lead_id"] is None
    assert lead["name_untrusted"] == "PumpToken"
    assert lead["age_minutes"] == "2.0"
    assert lead["initial_liquidity_sol"] == "30.5"
    assert lead["liquidity_basis"] == "virtual_sol_reserves"
    assert lead["curve_progress"] == _progress()
    assert lead["mint_authority"] == "null"
    assert lead["freeze_authority"] == "null"
    assert lead["has_mint_authority"] is False
    assert lead["excluded_accounts"] == [ATA]
    assert lead["top10_ex_curve_pct"] == "100.00"
    assert lead["top10_ex_curve_status"] == "fail"
    assert lead["deployer_tokens_seen"] == 1
    assert lead["deployer_status"] == "pass"
    assert lead["hand_off_to_risk"] is True
    assert lead["journal_note"]["note_kind"] == "lead"
    assert lead["journal_note"]["seat"] == "scout"
    assert lead["journal_note"]["refs"] == ["createSig", MINT]
    assert "LEAD-ID: (not allocated)" in lead["lead_block"]
    assert "Hand-off to RISK: YES" in lead["lead_block"]
    assert "does not clear risk" in lead["why"]
    assert payload["skipped"]["not_create"] == 1
    assert payload["skipped"]["failed_signature"] == 1
    assert payload["skipped"]["unknown_event"] == 1
    assert payload["skipped"]["failed_transaction"] == 1
    fetched = [params[0] for method, params in calls if method == "getTransaction"]
    assert fetched == ["createSig", "completeSig", "unknownSig", "metaErrSig"]
    rendered = json.dumps(payload)
    assert "sekret" not in rendered
    assert "example.invalid" not in rendered
    assert "rpc.invalid" not in rendered
    assert "Bearer" not in rendered


async def test_discover_stops_at_max_leads() -> None:
    calls: list[tuple[str, Any]] = []

    def result_of(method: str, params: list[Any]) -> object:
        if method == "getSignaturesForAddress" and params[0] == PUMP_PROGRAM_ID:
            return [
                {"signature": "createSig", "err": None, "blockTime": TS},
                {"signature": "secondSig", "err": None, "blockTime": TS},
            ]
        if method == "getSignaturesForAddress":
            return []
        if method == "getTransaction":
            return _create_tx()
        if method == "getAccountInfo":
            return _mint_account()
        if method == "getTokenLargestAccounts":
            return _holders()
        return {"value": {"amount": "1000", "decimals": 0}}

    def handler(request: httpx.Request) -> httpx.Response:
        return _serve(request, calls, result_of)

    payload = await scan_launches(_rpc(handler), max_leads=1)
    assert len(payload["leads"]) == 1
    assert payload["leads"][0]["source"] == "createSig"
    assert payload["window_exhausted"] is False
    fetched = [params[0] for method, params in calls if method == "getTransaction"]
    assert "createSig" in fetched


async def test_missing_curve_account_is_blind_not_a_pass() -> None:
    calls: list[tuple[str, Any]] = []

    def result_of(method: str, params: list[Any]) -> object:
        if method == "getSignaturesForAddress" and params[0] == PUMP_PROGRAM_ID:
            return [{"signature": "createSig", "err": None, "blockTime": TS}]
        if method == "getTransaction":
            return _tx([_program_data(_encode_create(_create_event()))])
        if method == "getAccountInfo":
            return _mint_account()
        if method == "getTokenLargestAccounts":
            return _holders()
        return []

    def handler(request: httpx.Request) -> httpx.Response:
        return _serve(request, calls, result_of)

    payload = await scan_launches(_rpc(handler), max_leads=1)
    lead = payload["leads"][0]
    assert lead["mint"] == MINT
    assert lead["excluded_accounts"] == []
    assert lead["top10_ex_curve_status"] == "blind"
    assert lead["top10_ex_curve_pct"] is None
    assert "not in the create transaction" in " ".join(lead["early_signals"])


async def test_signature_rpc_failure_is_data_stale() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    with pytest.raises(DataStale, match="signatures failed") as excinfo:
        await scan_launches(_rpc(handler), max_leads=1)
    assert "rpc.invalid" not in excinfo.value.message
