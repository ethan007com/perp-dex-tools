"""Entropy HIP-3 adapter through the Hyperliquid Python SDK.

This first version is intentionally read-only for integration validation.
Trading order methods will be enabled only after market/account checks pass.
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, List, Optional, Tuple

from hyperliquid.info import Info
from hyperliquid.utils.constants import MAINNET_API_URL

from .base import BaseExchangeClient, OrderInfo, OrderResult

def dec(value: Any) -> Decimal:
    return Decimal(str(value))

class EntropyClient(BaseExchangeClient):
    """Read-only Entropy HIP-3 client.

    Entropy is the Hyperliquid deployer dex named ``io``.
    Coins are represented as ``io:SYMBOL``.
    """

    def __init__(self, config):
        self.dex = os.getenv("ENTROPY_DEX", "io")
        self.network = os.getenv("ENTROPY_NETWORK", "mainnet").lower()
        self.account_address = os.getenv(
            "ENTROPY_ACCOUNT_ADDRESS",
            "",
        ).lower()

        self.info: Optional[Info] = None
        self.meta_data: Optional[dict] = None
        self.market_info: Optional[dict] = None
        self.orderbook: Optional[dict] = None

        super().__init__(config)

    def _validate_config(self) -> None:
        ticker = str(
            getattr(self.config, "ticker", "")
        ).upper().removesuffix("-USD")

        if not ticker:
            raise ValueError("Entropy ticker is required")

        self.config.ticker = ticker
        self.config.contract_id = f"{self.dex}:{ticker}"

        if self.network != "mainnet":
            raise ValueError(
                "Entropy integration currently requires "
                "ENTROPY_NETWORK=mainnet"
            )

        if not self.account_address:
            raise ValueError(
                "ENTROPY_ACCOUNT_ADDRESS is required"
            )

        if not (
            self.account_address.startswith("0x")
            and len(self.account_address) == 42
        ):
            raise ValueError(
                "ENTROPY_ACCOUNT_ADDRESS must be a valid "
                "0x address"
            )

    async def connect(self) -> None:
        self.info = await asyncio.to_thread(
            Info,
            MAINNET_API_URL,
            True,
            None,
            None,
            [self.dex],
        )

        self.meta_data = await asyncio.to_thread(
            self.info.meta,
            self.dex,
        )

        markets = self.meta_data.get("universe", [])
        matches = [
            item
            for item in markets
            if item.get("name") == self.config.contract_id
            and not item.get("isDelisted", False)
        ]

        if len(matches) != 1:
            available = [
                item.get("name")
                for item in markets
                if not item.get("isDelisted", False)
            ]
            raise RuntimeError(
                f"Entropy market not found: "
                f"{self.config.contract_id}; "
                f"available={available}"
            )

        self.market_info = matches[0]

    async def disconnect(self) -> None:
        if self.info is not None:
            try:
                await asyncio.to_thread(
                    self.info.disconnect_websocket
                )
            except RuntimeError:
                pass

        self.info = None

    def _require_info(self) -> Info:
        if self.info is None:
            raise RuntimeError(
                "Entropy client is not connected"
            )
        return self.info

    def _require_market(self) -> dict:
        if self.market_info is None:
            raise RuntimeError(
                "Entropy market metadata is not loaded"
            )
        return self.market_info

    async def get_contract_attributes(
        self,
    ) -> Tuple[str, Decimal]:
        market = self._require_market()
        sz_decimals = int(market["szDecimals"])

        # Hyperliquid does not expose a universal price tick in meta.
        # Keep a conservative price precision for the read-only stage.
        tick_size = Decimal(
            "1e-" + str(max(0, sz_decimals))
        )

        self.config.tick_size = tick_size
        return self.config.contract_id, tick_size

    async def fetch_bbo_prices(
        self,
        contract_id: str,
    ) -> Tuple[Decimal, Decimal]:
        info = self._require_info()

        book = await asyncio.to_thread(
            info.l2_snapshot,
            contract_id,
        )

        levels = book.get("levels", [])
        if len(levels) != 2:
            raise RuntimeError(
                f"Entropy returned invalid L2 book: {contract_id}"
            )

        bids = levels[0]
        asks = levels[1]

        if not bids or not asks:
            raise RuntimeError(
                f"Entropy order book is empty: {contract_id}"
            )

        best_bid = dec(bids[0]["px"])
        best_ask = dec(asks[0]["px"])

        if not (best_bid > 0 and best_ask > best_bid):
            raise RuntimeError(
                f"Entropy returned invalid BBO: "
                f"{best_bid}/{best_ask}"
            )

        self.orderbook = book
        return best_bid, best_ask

    async def get_order_price(
        self,
        direction: str,
    ) -> Decimal:
        bid, ask = await self.fetch_bbo_prices(
            self.config.contract_id
        )

        if direction.lower() == "buy":
            return bid

        return ask

    async def get_account_positions(self) -> Decimal:
        info = self._require_info()

        state = await asyncio.to_thread(
            info.user_state,
            self.account_address,
            self.dex,
        )

        total = Decimal("0")

        for item in state.get("assetPositions", []):
            position = item.get("position", {})
            if position.get("coin") != self.config.contract_id:
                continue

            total += abs(dec(position.get("szi", "0")))

        return total

    async def get_active_orders(
        self,
        contract_id: str,
    ) -> List[OrderInfo]:
        info = self._require_info()

        rows = await asyncio.to_thread(
            info.open_orders,
            self.account_address,
            self.dex,
        )

        result = []

        for row in rows:
            if row.get("coin") != contract_id:
                continue

            side = (
                "buy"
                if row.get("side") == "B"
                else "sell"
            )
            size = dec(row.get("sz", "0"))
            price = dec(row.get("limitPx", "0"))

            result.append(
                OrderInfo(
                    order_id=str(row.get("oid")),
                    side=side,
                    size=size,
                    price=price,
                    status="OPEN",
                    filled_size=Decimal("0"),
                    remaining_size=size,
                    cancel_reason="",
                )
            )

        return result

    async def get_account_snapshot(self) -> dict:
        info = self._require_info()

        state = await asyncio.to_thread(
            info.user_state,
            self.account_address,
            self.dex,
        )

        return state

    async def can_open_new_orders(self) -> bool:
        # Entropy equity perpetuals trade continuously.
        return True

    def setup_order_update_handler(self, handler) -> None:
        # The first adapter version uses REST polling.
        self._order_update_handler = handler

    def get_exchange_name(self) -> str:
        return "entropy"

    async def place_open_order(
        self,
        contract_id: str,
        quantity: Decimal,
        direction: str,
    ) -> OrderResult:
        raise NotImplementedError(
            "Entropy order submission is disabled until "
            "read-only validation passes"
        )

    async def place_close_order(
        self,
        contract_id: str,
        quantity: Decimal,
        price: Decimal,
        side: str,
    ) -> OrderResult:
        raise NotImplementedError(
            "Entropy order submission is disabled until "
            "read-only validation passes"
        )

    async def cancel_order(
        self,
        order_id: str,
    ) -> OrderResult:
        raise NotImplementedError(
            "Entropy order cancellation is disabled until "
            "trading methods are enabled"
        )

    async def get_order_info(
        self,
        order_id: str,
    ) -> Optional[OrderInfo]:
        active = await self.get_active_orders(
            self.config.contract_id
        )

        for order in active:
            if order.order_id == str(order_id):
                return order

        return None
