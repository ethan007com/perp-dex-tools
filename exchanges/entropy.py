from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Optional

from exchanges.base import (
    BaseExchangeClient,
    OrderInfo,
    OrderResult,
)

from exchanges.entropy_order_gateway import (
    EntropyOrderGateway,
)

def _get_config(config, name, default=None):
    if isinstance(config, dict):
        return config.get(name, default)
    return getattr(config, name, default)

def _d(value, default=Decimal("0")):
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default

class EntropyClient(BaseExchangeClient):
    """
    Hyperliquid HIP-3 Entropy adapter.

    固定市场：
        dex  = io
        coin = io:NVDA

    写请求统一由 EntropyOrderGateway 保护。
    """

    def __init__(self, config):
        self.gateway: Optional[
            EntropyOrderGateway
        ] = None
        self._order_update_handler = None
        self.contract_id = "io:NVDA"
        self._market = None
        super().__init__(config)

    def _validate_config(self) -> None:
        dex = _get_config(
            self.config,
            "dex",
            _get_config(
                self.config,
                "entropy_dex",
                "io",
            ),
        )

        ticker = _get_config(
            self.config,
            "ticker",
            _get_config(
                self.config,
                "symbol",
                "NVDA",
            ),
        )

        if str(dex).lower() != "io":
            raise ValueError(
                "Entropy requires dex=io"
            )

        if str(ticker).upper() != "NVDA":
            raise ValueError(
                "Entropy requires ticker=NVDA"
            )

    async def connect(self) -> None:
        self.gateway = await asyncio.to_thread(
            EntropyOrderGateway,
            ".env.entropy",
        )

        self._market = self.gateway.market
        self.contract_id = "io:NVDA"

    async def disconnect(self) -> None:
        self.gateway = None
        self._market = None

    def _require_gateway(self) -> EntropyOrderGateway:
        if self.gateway is None:
            raise RuntimeError(
                "Entropy client is not connected"
            )
        return self.gateway

    async def _require_connected(self):
        if self.gateway is None:
            await self.connect()
        return self._require_gateway()

    async def get_contract_attributes(self):
        gateway = await self._require_connected()

        # TradingBot expects exactly:
        # contract_id, tick_size
        #
        # io:NVDA is quoted to 2 decimal places
        # around the current price range.
        tick_size = Decimal("0.01")

        return gateway.coin, tick_size

    async def fetch_bbo_prices(self, contract_id):
        gateway = await self._require_connected()
        if contract_id != gateway.coin:
            raise ValueError("Unsupported Entropy contract")

        book = await asyncio.to_thread(
            gateway.info.l2_snapshot, gateway.coin
        )
        levels = book.get("levels", [])
        if len(levels) != 2 or not levels[0] or not levels[1]:
            raise RuntimeError("Entropy order book is empty or invalid")

        bid = Decimal(str(levels[0][0]["px"]))
        ask = Decimal(str(levels[1][0]["px"]))
        if (
            not bid.is_finite()
            or not ask.is_finite()
            or bid <= 0
            or ask <= 0
            or bid >= ask
        ):
            raise RuntimeError("Entropy BBO is invalid")

        # TradingBot expects exactly: best_bid, best_ask.
        return bid, ask

    async def get_order_price(self, direction):
        bid, ask = await self.fetch_bbo_prices(self.contract_id)
        direction = str(direction).lower()
        if direction in ("buy", "long"):
            return bid
        if direction in ("sell", "short"):
            return ask
        raise ValueError("Unsupported order direction")

    async def get_account_positions(self) -> Decimal:
        gateway = await self._require_connected()

        state = await asyncio.to_thread(
            gateway.info.user_state,
            gateway.account_address,
            dex=gateway.dex,
        )

        total = Decimal("0")

        for item in state.get(
            "assetPositions",
            [],
        ):
            position = item.get("position", {})
            coin = position.get("coin")

            if coin != gateway.coin:
                continue

            total += abs(
                _d(position.get("szi"))
            )

        return total

    async def get_active_orders(self, contract_id):
        gateway = await self._require_connected()
        if contract_id != gateway.coin:
            raise ValueError("Unsupported Entropy contract")

        orders = await asyncio.to_thread(gateway.open_orders)
        if not isinstance(orders, list):
            raise RuntimeError("Active orders response invalid")

        result = []
        for order in orders:
            if order.get("coin") != gateway.coin:
                continue

            side = {"B": "BUY", "A": "SELL"}.get(
                str(order.get("side", "")).upper()
            )
            if side is None:
                raise RuntimeError("Unknown active order side")
            if order.get("oid") is None:
                raise RuntimeError("Active order id missing")

            try:
                remaining = Decimal(str(order["sz"]))
                original = Decimal(str(order.get("origSz", order["sz"])))
                price = Decimal(str(order["limitPx"]))
            except Exception:
                raise RuntimeError("Active order numeric fields invalid") from None

            if (
                not all(x.is_finite() for x in (remaining, original, price))
                or remaining < 0
                or original <= 0
                or remaining > original
                or price <= 0
            ):
                raise RuntimeError("Active order quantities or price invalid")

            filled = original - remaining
            result.append(OrderInfo(
                order_id=str(order["oid"]),
                side=side,
                size=original,
                price=price,
                status="PARTIALLY_FILLED" if filled > 0 else "OPEN",
                filled_size=filled,
                remaining_size=remaining,
            ))
        return result

    async def get_account_snapshot(self):
        gateway = await self._require_connected()

        state = await asyncio.to_thread(
            gateway.info.user_state,
            gateway.account_address,
            dex=gateway.dex,
        )

        margin = state.get(
            "marginSummary",
            {},
        )

        return {
            "account_value": _d(
                margin.get("accountValue")
            ),
            "total_margin_used": _d(
                margin.get("totalMarginUsed")
            ),
            "total_notional": _d(
                margin.get("totalNtlPos")
            ),
            "withdrawable": _d(
                state.get("withdrawable")
            ),
            "position_size": await self.get_account_positions(),
        }

    async def can_open_new_orders(self) -> bool:
        gateway = await self._require_connected()

        if not gateway.trading_enabled:
            return False

        # Hyperliquid Unified Account / HIP-3 may report
        # withdrawable=0 in the selected dex state even when
        # Spot USDC is usable as unified collateral.
        if gateway.dex == "io":
            spot_state = await asyncio.to_thread(
                gateway.info.spot_user_state,
                gateway.account_address,
            )

            balances = spot_state.get(
                "balances",
                [],
            )

            for balance in balances:
                if str(
                    balance.get("coin", "")
                ).upper() != "USDC":
                    continue

                total = Decimal(
                    str(
                        balance.get(
                            "total",
                            "0",
                        )
                    )
                )

                hold = Decimal(
                    str(
                        balance.get(
                            "hold",
                            "0",
                        )
                    )
                )

                available = total - hold

                # io:NVDA has a minimum order notional
                # of approximately 10 USDC.
                return available >= Decimal("10")

            return False

        snapshot = await self.get_account_snapshot()

        return (
            snapshot["withdrawable"]
            > Decimal("0")
        )

    @staticmethod
    def _extract_order_status(response):
        if not isinstance(response, dict):
            return None

        outer = response.get("response")
        if not isinstance(outer, dict):
            return None

        data = outer.get("data")
        if not isinstance(data, dict):
            return None

        statuses = data.get("statuses")
        if not isinstance(statuses, list):
            return None

        if not statuses:
            return None

        return statuses[0]

    @classmethod
    def _order_result(
        cls,
        response,
        side,
        size,
        price,
    ) -> OrderResult:
        status = cls._extract_order_status(
            response
        )

        if not isinstance(status, dict):
            return OrderResult(
                success=False,
                side=side,
                size=size,
                price=price,
                status="UNKNOWN",
                error_message=str(response)[:500],
            )

        if "resting" in status:
            oid = status["resting"].get("oid")

            return OrderResult(
                success=True,
                order_id=str(oid),
                side=side,
                size=size,
                price=price,
                status="OPEN",
                filled_size=Decimal("0"),
            )

        if "filled" in status:
            filled = status["filled"]
            oid = filled.get("oid")
            total_filled = _d(
                filled.get("totalSz")
            )

            return OrderResult(
                success=True,
                order_id=str(oid),
                side=side,
                size=size,
                price=price,
                status="FILLED",
                filled_size=total_filled,
            )

        if "error" in status:
            return OrderResult(
                success=False,
                side=side,
                size=size,
                price=price,
                status="REJECTED",
                error_message=str(
                    status["error"]
                ),
            )

        return OrderResult(
            success=False,
            side=side,
            size=size,
            price=price,
            status="UNKNOWN",
            error_message=str(status),
        )

    async def place_open_order(
        self,
        contract_id: str,
        quantity: Decimal,
        direction: str,
    ) -> OrderResult:
        gateway = await self._require_connected()

        if contract_id != gateway.coin:
            raise ValueError(
                f"Unsupported contract: {contract_id}"
            )

        direction = str(direction).lower()

        if direction in ("buy", "long"):
            is_buy = True
            side = "BUY"
        elif direction in ("sell", "short"):
            is_buy = False
            side = "SELL"
        else:
            raise ValueError(
                f"Unsupported direction: {direction}"
            )

        price = await self.get_order_price(
            direction
        )

        response = await asyncio.to_thread(
            gateway.place_open_order,
            is_buy,
            quantity,
            price,
            True,
        )

        return self._order_result(
            response,
            side,
            _d(quantity),
            price,
        )

    async def place_close_order(
        self,
        contract_id: str,
        quantity: Decimal,
        price: Decimal,
        side: str,
    ) -> OrderResult:
        gateway = await self._require_connected()

        if contract_id != gateway.coin:
            raise ValueError(
                f"Unsupported contract: {contract_id}"
            )

        side = str(side).upper()

        if side == "SELL":
            is_buy = False
        elif side == "BUY":
            is_buy = True
        else:
            raise ValueError(
                f"Unsupported close side: {side}"
            )

        response = await asyncio.to_thread(
            gateway.place_reduce_only_close,
            is_buy,
            quantity,
            price,
            True,
        )

        return self._order_result(
            response,
            side,
            _d(quantity),
            _d(price),
        )

    async def cancel_order(
        self,
        order_id: str,
    ) -> OrderResult:
        gateway = await self._require_connected()

        oid = int(order_id)

        response = await asyncio.to_thread(
            gateway.cancel_order,
            oid,
        )

        success = (
            isinstance(response, dict)
            and response.get("status") == "ok"
        )

        return OrderResult(
            success=success,
            order_id=str(oid),
            status="CANCELED" if success else "FAILED",
            error_message=None
            if success
            else str(response)[:500],
        )

    async def get_order_info(self, order_id):
        gateway = await self._require_connected()
        response = await asyncio.to_thread(
            gateway.query_order, int(order_id)
        )

        # Unknown order is not equivalent to canceled or unfilled.
        if not isinstance(response, dict):
            raise RuntimeError("Order query response invalid; reconciliation required")
        if response.get("status") != "order":
            raise RuntimeError("Order state unknown; do not resubmit automatically")

        envelope = response.get("order")
        if not isinstance(envelope, dict):
            raise RuntimeError("Order query envelope invalid")

        # Standard nested response:
        # response.order.status
        # response.order.order.{oid, coin, side, origSz, sz, limitPx}
        # Also accept the earlier flat test fixture.
        details = envelope.get("order", envelope)
        if not isinstance(details, dict):
            raise RuntimeError("Order details invalid")

        oid = details.get("oid")
        if oid is not None and str(oid) != str(order_id):
            raise RuntimeError("Order id mismatch")

        coin = details.get("coin")
        if coin is not None and coin != gateway.coin:
            raise RuntimeError("Order belongs to another market")

        raw_side = str(details.get("side", "")).upper()
        sides = {"B": "BUY", "A": "SELL", "BUY": "BUY", "SELL": "SELL"}
        if raw_side not in sides:
            raise RuntimeError("Unknown order side")

        def number(key):
            try:
                value = Decimal(str(details[key]))
            except Exception:
                raise RuntimeError("Order numeric field missing or invalid") from None
            if not value.is_finite() or value < 0:
                raise RuntimeError("Order numeric field outside valid range")
            return value

        # Do not use `sz or origSz`: numeric zero is a valid remaining size.
        original = number("origSz")
        remaining = number("sz")
        price = number("limitPx")
        if original <= 0 or price <= 0 or remaining > original:
            raise RuntimeError("Order quantities or price are inconsistent")

        raw_status = str(envelope.get("status", "")).lower()
        if raw_status == "filled":
            filled = original
            remaining = Decimal("0")
            status = "FILLED"
        else:
            filled = original - remaining
            if raw_status == "open":
                status = "PARTIALLY_FILLED" if filled > 0 else "OPEN"
            elif raw_status in ("canceled", "cancelled") or raw_status.endswith("canceled"):
                status = "CANCELED"
            elif raw_status == "rejected" or raw_status.endswith("rejected"):
                status = "REJECTED"
            else:
                raise RuntimeError("Unrecognized order state; reconciliation required")

        return OrderInfo(
            order_id=str(order_id),
            side=sides[raw_side],
            size=original,
            price=price,
            status=status,
            filled_size=filled,
            remaining_size=remaining,
        )

    def setup_order_update_handler(
        self,
        handler,
    ) -> None:
        self._order_update_handler = handler

    def get_exchange_name(self) -> str:
        return "entropy"
