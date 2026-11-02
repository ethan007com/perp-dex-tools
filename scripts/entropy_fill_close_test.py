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
SIZE = Decimal("0.050")
PRICE_OFFSET = Decimal("1.00")
POLL_SECONDS = 8


def parse_args():
    parser = argparse.ArgumentParser(
        description="Entropy live fill and reduce-only close test"
    )
    parser.add_argument(
        "--env-file",
        default=os.environ.get(
            "PERP_DEX_ENV_FILE",
            ".env",
        ),
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="required for real order submission",
    )
    parser.add_argument(
        "--size",
        default=str(SIZE),
    )
    return parser.parse_args()


def get_status(response):
    try:
        return response["response"]["data"]["statuses"][0]
    except (KeyError, IndexError, TypeError):
        return None


def get_oid(response):
    status = get_status(response)

    if not isinstance(status, dict):
        return None

    for key in ("resting", "filled"):
        value = status.get(key)
        if isinstance(value, dict):
            return value.get("oid")

    return None


def get_nested_order(response):
    if not isinstance(response, dict):
        return None

    order = response.get("order")
    if not isinstance(order, dict):
        return None

    order = order.get("order")
    if not isinstance(order, dict):
        return None

    return order


def get_order_status(response):
    if not isinstance(response, dict):
        return "UNKNOWN"

    order = response.get("order")
    if not isinstance(order, dict):
        return "UNKNOWN"

    value = order.get("status")
    if isinstance(value, str):
        return value.lower()

    return "UNKNOWN"


def mask(address):
    return address[:6] + "..." + address[-4:]


def ensure_clean_account(gateway):
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
            "已有仓位，停止测试，不重复开仓"
        )

    orders = gateway.open_orders()

    if orders:
        raise SystemExit(
            "已有活动订单，停止测试"
        )


def get_bid_ask(gateway):
    book = gateway.info.l2_snapshot(
        gateway.coin
    )

    levels = book.get("levels", [])

    if len(levels) != 2:
        raise SystemExit(
            "订单簿格式错误"
        )

    if not levels[0] or not levels[1]:
        raise SystemExit(
            "订单簿为空"
        )

    bid = Decimal(
        str(levels[0][0]["px"])
    )
    ask = Decimal(
        str(levels[1][0]["px"])
    )

    return bid, ask


def main():
    args = parse_args()
    size = Decimal(str(args.size))

    gateway = EntropyOrderGateway(
        args.env_file
    )

    print("env_file:", args.env_file)
    print(
        "main account:",
        mask(gateway.account_address),
    )
    print("signer:", mask(gateway.wallet.address))
    print("dex:", gateway.dex)
    print("coin:", gateway.coin)
    print("size:", size)
    print(
        "trading_enabled:",
        gateway.trading_enabled,
    )

    if not args.confirm_live:
        raise SystemExit(
            "缺少 --confirm-live，未发送订单"
        )

    if not gateway.trading_enabled:
        raise SystemExit(
            "ENTROPY_TRADING_ENABLED=false，"
            "未发送订单"
        )

    if gateway.coin != COIN:
        raise SystemExit(
            f"错误市场：{gateway.coin}"
        )

    if size <= 0:
        raise SystemExit(
            "订单数量必须大于 0"
        )

    ensure_clean_account(gateway)

    bid, ask = get_bid_ask(gateway)

    open_price = ask + PRICE_OFFSET
    close_price = bid - PRICE_OFFSET

    print("best_bid:", bid)
    print("best_ask:", ask)
    print("open_price:", open_price)
    print("close_price:", close_price)
    print(
        "open_notional:",
        open_price * size,
    )

    open_cloid_text = (
        "0x" + uuid.uuid4().hex[:32]
    )
    open_cloid = Cloid.from_str(
        open_cloid_text
    )

    print("opening long position")

    open_response = gateway.exchange.order(
        gateway.coin,
        True,
        float(size),
        float(open_price),
        {
            "limit": {
                "tif": "Ioc",
            }
        },
        reduce_only=False,
        cloid=open_cloid,
    )

    print("open_response_status:", get_status(open_response))

    open_oid = get_oid(open_response)

    if open_oid is None:
        raise SystemExit(
            "开仓没有返回订单 ID，"
            "禁止重试，先人工查单"
        )

    print("open_order_id:", open_oid)

    filled = False
    final_open_response = None

    for index in range(POLL_SECONDS):
        time.sleep(1)

        final_open_response = (
            gateway.query_order(
                int(open_oid)
            )
        )

        status = get_order_status(
            final_open_response
        )

        print(
            f"open_query_{index + 1}:",
            status,
        )

        if status == "filled":
            filled = True
            break

        if status in (
            "canceled",
            "rejected",
        ):
            break

    if not filled:
        raise SystemExit(
            "开仓没有确认 FILLED，"
            "未执行平仓；请人工查询订单"
        )

    print("开仓已确认 FILLED")

    time.sleep(1)

    state = gateway.info.user_state(
        gateway.account_address,
        dex=gateway.dex,
    )

    positions = state.get(
        "assetPositions",
        [],
    )

    if not positions:
        raise SystemExit(
            "订单显示 FILLED，"
            "但账户没有仓位；停止并人工核对"
        )

    close_cloid_text = (
        "0x" + uuid.uuid4().hex[:32]
    )
    close_cloid = Cloid.from_str(
        close_cloid_text
    )

    print("closing with reduceOnly SELL")

    close_response = gateway.exchange.order(
        gateway.coin,
        False,
        float(size),
        float(close_price),
        {
            "limit": {
                "tif": "Ioc",
            }
        },
        reduce_only=True,
        cloid=close_cloid,
    )

    print(
        "close_response_status:",
        get_status(close_response),
    )

    close_oid = get_oid(close_response)

    if close_oid is None:
        raise SystemExit(
            "平仓没有返回订单 ID，"
            "禁止重试，先人工查单"
        )

    print("close_order_id:", close_oid)

    close_filled = False

    for index in range(POLL_SECONDS):
        time.sleep(1)

        response = gateway.query_order(
            int(close_oid)
        )

        status = get_order_status(response)

        print(
            f"close_query_{index + 1}:",
            status,
        )

        if status == "filled":
            close_filled = True
            break

        if status in (
            "canceled",
            "rejected",
        ):
            break

    if not close_filled:
        raise SystemExit(
            "reduceOnly 平仓没有确认 FILLED，"
            "停止自动操作，需人工处理"
        )

    time.sleep(1)

    final_state = gateway.info.user_state(
        gateway.account_address,
        dex=gateway.dex,
    )

    final_positions = final_state.get(
        "assetPositions",
        [],
    )

    print(
        "final_positions:",
        final_positions,
    )

    if final_positions:
        raise SystemExit(
            "平仓后仍存在仓位，"
            "不要启动机器人，先人工处理"
        )

    active_orders = gateway.open_orders()

    if active_orders:
        raise SystemExit(
            "测试结束仍有活动订单"
        )

    print("================================")
    print("OPEN FILLED: OK")
    print("REDUCE-ONLY CLOSE FILLED: OK")
    print("FINAL POSITION: 0")
    print("ACTIVE ORDERS: 0")
    print("Entropy fill/close test: OK")
    print("================================")


if __name__ == "__main__":
    main()
