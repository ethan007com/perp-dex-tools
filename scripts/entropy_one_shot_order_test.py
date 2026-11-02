from __future__ import annotations

import argparse
import os
import time
import uuid
from decimal import Decimal

from hyperliquid.utils.types import Cloid

from exchanges.entropy_order_gateway import (
    EntropyOrderGateway,
)

COIN = "io:NVDA"
DEFAULT_SIZE = Decimal("0.001")
PRICE_TICK = Decimal("0.01")
MIN_NOTIONAL = Decimal("10")
SIZE_STEP = Decimal("0.001")

def parse_args():
    parser = argparse.ArgumentParser(
        description="Entropy one-shot post-only order test"
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="required for real order submission",
    )
    parser.add_argument(
        "--env-file",
        default=os.environ.get(
            "PERP_DEX_ENV_FILE",
            ".env",
        ),
        help="selected dotenv configuration file",
    )
    parser.add_argument(
        "--size",
        default=str(DEFAULT_SIZE),
        help="order size in NVDA",
    )
    return parser.parse_args()

def extract_status(response):
    if not isinstance(response, dict):
        return None

    outer = response.get("response")
    if not isinstance(outer, dict):
        return None

    data = outer.get("data")
    if not isinstance(data, dict):
        return None

    statuses = data.get("statuses")
    if not isinstance(statuses, list) or not statuses:
        return None

    return statuses[0]

def extract_oid(response):
    status = extract_status(response)

    if not isinstance(status, dict):
        return None

    if "resting" in status:
        return status["resting"].get("oid")

    if "filled" in status:
        return status["filled"].get("oid")

    return None

def mask(address):
    return address[:6] + "..." + address[-4:]

def main():
    args = parse_args()
    gateway = EntropyOrderGateway(
        args.env_file
    )

    size = Decimal(str(args.size))

    if (
        not size.is_finite()
        or size <= 0
    ):
        raise SystemExit(
            "invalid order size"
        )

    if (
        size % SIZE_STEP
        != Decimal("0")
    ):
        raise SystemExit(
            "size must use 0.001 step"
        )

    print("env_file:", args.env_file)
    print("main account:", mask(gateway.account_address))
    print("signer:", mask(gateway.wallet.address))
    print("dex:", gateway.dex)
    print("coin:", gateway.coin)
    print("size:", size)
    print("trading_enabled:", gateway.trading_enabled)

    if gateway.coin != COIN:
        raise SystemExit(
            f"wrong market: {gateway.coin}"
        )

    if not gateway.trading_enabled:
        raise SystemExit(
            "ENTROPY_TRADING_ENABLED=false; "
            "real order was not sent"
        )

    if not args.confirm_live:
        raise SystemExit(
            "missing --confirm-live; "
            "real order was not sent"
        )

    state = gateway.info.user_state(
        gateway.account_address,
        dex=gateway.dex,
    )

    positions = state.get(
        "assetPositions",
        [],
    )

    if positions:
        raise SystemExit(
            "existing positions found; "
            "one-shot test aborted"
        )

    active_orders = gateway.open_orders()

    if active_orders:
        raise SystemExit(
            "existing active orders found; "
            "one-shot test aborted"
        )

    book = gateway.info.l2_snapshot(
        gateway.coin
    )
    levels = book.get("levels", [])

    if len(levels) != 2:
        raise SystemExit(
            "invalid order book"
        )

    if not levels[0] or not levels[1]:
        raise SystemExit(
            "empty order book"
        )

    bid = Decimal(str(levels[0][0]["px"]))
    ask = Decimal(str(levels[1][0]["px"]))

    # Place one tick below bid to reduce
    # accidental immediate execution.
    price = bid - PRICE_TICK

    if price <= 0 or price >= ask:
        raise SystemExit(
            "invalid test price"
        )

    notional = price * size
    print("estimated_notional:", notional)

    if notional < MIN_NOTIONAL:
        minimum_size = (
            MIN_NOTIONAL / price
        ).quantize(
            SIZE_STEP
        )

        if minimum_size * price < MIN_NOTIONAL:
            minimum_size += SIZE_STEP

        raise SystemExit(
            "order value below exchange minimum: "
            f"{notional} < {MIN_NOTIONAL}; "
            f"use --size at least "
            f"{minimum_size}"
        )

    cloid_text = (
        "0x"
        + uuid.uuid4().hex[:32]
    )
    cloid = Cloid.from_str(cloid_text)

    print("best_bid:", bid)
    print("best_ask:", ask)
    print("test_price:", price)
    print("cloid:", cloid_text)
    print("submitting one post-only BUY")

    response = gateway.exchange.order(
        gateway.coin,
        True,
        float(size),
        float(price),
        {"limit": {"tif": "Alo"}},
        reduce_only=False,
        cloid=cloid,
    )

    print("order response received")
    status = extract_status(response)
    print("status_type:", type(status).__name__)

    if isinstance(status, dict) and "error" in status:
        raise SystemExit(
            "order rejected: "
            + str(status["error"])
        )

    oid = extract_oid(response)

    if oid is None:
        raise SystemExit(
            "order result has no oid; "
            "do not resend automatically"
        )

    print("order_id:", oid)

    if isinstance(status, dict) and "filled" in status:
        raise SystemExit(
            "order filled immediately; "
            "stop and reconcile position before continuing"
        )

    time.sleep(1)

    queried = gateway.query_order(int(oid))
    print("query_after_submit:", queried)

    print("canceling test order")
    cancel_response = gateway.exchange.cancel(
        gateway.coin,
        int(oid),
    )
    print("cancel_response:", cancel_response)

    time.sleep(1)

    final_state = gateway.query_order(int(oid))
    print("query_after_cancel:", final_state)

    print("one-shot order/cancel test finished")
    print("writes executed: order=1, cancel=1")

if __name__ == "__main__":
    main()
