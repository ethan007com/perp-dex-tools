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

    async def fetch_bbo_prices(
        self,
        contract_id: str,
    ):
        gateway = await self._require_connected()

        if contract_id != gateway.coin:
            raise ValueError(
                f"Unsupported contract: {contract_id}"
            )

        book = await asyncio.to_thread(
            gateway.info.l2_snapshot,
            gateway.coin,
        )

        levels = book.get("levels", [])

        if len(levels) != 2:
            raise RuntimeError(
                "Entropy order book format invalid"
            )

        if not levels[0] or not levels[1]:
            raise RuntimeError(
                "Entropy order book is empty"
            )

        bid = Decimal(
            str(levels[0][0]["px"])
        )
        ask = Decimal(
            str(levels[1][0]["px"])
        )

        return {
            "best_bid": bid,
            "best_ask": ask,
            "bid": bid,
            "ask": ask,
        }

    async def get_order_price(
        self,
        direction: str,
    ):
        bbo = await self.fetch_bbo_prices(
            self.contract_id
        )

        direction = str(direction).lower()

        if direction in ("buy", "long"):
            return bbo["best_bid"]

        if direction in ("sell", "short"):
            return bbo["best_ask"]

        raise ValueError(
            f"Unsupported direction: {direction}"
        )

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

    async def get_active_orders(
        self,
        contract_id: str,
    ):
        gateway = await self._require_connected()

        if contract_id != gateway.coin:
            raise ValueError(
                f"Unsupported contract: {contract_id}"
            )

        orders = await asyncio.to_thread(
            gateway.open_orders
        )

        result = []

        for order in orders:
            if order.get("coin") != gateway.coin:
                continue

            oid = order.get("oid")
            side = str(
                order.get("side", "")
            ).upper()

            size = _d(
                order.get("sz")
                or order.get("origSz")
            )

            price = _d(
                order.get("limitPx")
                or order.get("px")
            )

            result.append(
                OrderInfo(
                    order_id=str(oid),
                    side=side,
                    size=size,
                    price=price,
                    status="open",
                    filled_size=Decimal("0"),
                    remaining_size=size,
                )
            )

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
                status="unknown",
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
                status="resting",
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
                status="filled",
                filled_size=total_filled,
            )

        if "error" in status:
            return OrderResult(
                success=False,
                side=side,
                size=size,
                price=price,
                status="rejected",
                error_message=str(
                    status["error"]
                ),
            )

        return OrderResult(
            success=False,
            side=side,
            size=size,
            price=price,
            status="unknown",
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
            status="cancelled" if success else "failed",
            error_message=None
            if success
            else str(response)[:500],
        )

    async def get_order_info(
        self,
        order_id: str,
    ) -> Optional[OrderInfo]:
        gateway = await self._require_connected()

        response = await asyncio.to_thread(
            gateway.query_order,
            int(order_id),
        )

        if not isinstance(response, dict):
            return None

        status = response.get("status")
        order = response.get("order", response)

        if not isinstance(order, dict):
            return None

        side = str(
            order.get("side", "")
        ).upper()

        original_size = _d(
            order.get("origSz")
            or order.get("sz")
        )

        remaining_size = _d(
            order.get("sz")
            or order.get("origSz")
        )

        price = _d(
            order.get("limitPx")
            or order.get("px")
        )

        filled = max(
            Decimal("0"),
            original_size - remaining_size,
        )

        if status == "order":
            normalized_status = str(
                order.get("status", "unknown")
            ).lower()
        else:
            normalized_status = str(
                status or "unknown"
            ).lower()

        return OrderInfo(
            order_id=str(order_id),
            side=side,
            size=original_size,
            price=price,
            status=normalized_status,
            filled_size=filled,
            remaining_size=max(
                Decimal("0"),
                remaining_size,
            ),
        )

    def setup_order_update_handler(
        self,
        handler,
    ) -> None:
        self._order_update_handler = handler

    def get_exchange_name(self) -> str:
        return "entropy"
