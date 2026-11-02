from __future__ import annotations

import os
from decimal import Decimal, ROUND_DOWN
from typing import Any

from dotenv import dotenv_values
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.constants import MAINNET_API_URL

class EntropyOrderGateway:
    """
    Hyperliquid Unified Account order gateway.

    当前固定：
        dex  = io
        coin = io:NVDA

    写请求必须满足：
        ENTROPY_TRADING_ENABLED=true

    交易开关为 false 时：
        order / cancel / leverage 全部拒绝
    """

    def __init__(self, env_file: str = ".env.entropy"):
        # runbot.py sets PERP_DEX_ENV_FILE from
        # --env-file. Direct gateway tests may still
        # use the explicitly supplied env_file.
        selected_env_file = os.environ.get(
            "PERP_DEX_ENV_FILE",
            env_file,
        )

        cfg = dotenv_values(
            selected_env_file,
            interpolate=False,
        )

        # Credentials are loaded exclusively from
        # the selected env file. Stale shell
        # ENTROPY_* values must not override it.

        self.network = cfg.get(
            "ENTROPY_NETWORK",
            "mainnet",
        )

        self.dex = cfg.get(
            "ENTROPY_DEX",
            "io",
        )

        self.coin = cfg.get(
            "ENTROPY_COIN",
            "io:NVDA",
        )

        self.ticker = cfg.get(
            "ENTROPY_TICKER",
            "NVDA",
        )

        self.account_address = (
            cfg.get("ENTROPY_ACCOUNT_ADDRESS") or ""
        ).strip()

        secret_key = (
            cfg.get("ENTROPY_SECRET_KEY") or ""
        ).strip()

        self.trading_enabled = (
            str(
                cfg.get(
                    "ENTROPY_TRADING_ENABLED",
                    "false",
                )
            ).lower()
            == "true"
        )

        if self.network != "mainnet":
            raise RuntimeError(
                "Entropy network must be mainnet"
            )

        if self.dex != "io":
            raise RuntimeError(
                "Entropy dex must be io"
            )

        if self.ticker != "NVDA":
            raise RuntimeError(
                "Entropy ticker must be NVDA"
            )

        if self.coin != "io:NVDA":
            raise RuntimeError(
                "Entropy coin must be io:NVDA"
            )

        if not self.account_address:
            raise RuntimeError(
                "ENTROPY_ACCOUNT_ADDRESS is missing"
            )

        if not secret_key:
            raise RuntimeError(
                "ENTROPY_SECRET_KEY is missing"
            )

        self.wallet = Account.from_key(secret_key)

        if (
            self.wallet.address.lower()
            == self.account_address.lower()
        ):
            raise RuntimeError(
                "API wallet must differ from main account"
            )

        self.info = Info(
            MAINNET_API_URL,
            skip_ws=True,
            perp_dexs=[self.dex],
            timeout=20,
        )

        self.exchange = Exchange(
            self.wallet,
            MAINNET_API_URL,
            account_address=self.account_address,
            perp_dexs=[self.dex],
            timeout=20,
        )

        meta = self.info.meta(dex=self.dex)

        self.market = next(
            (
                item
                for item in meta.get("universe", [])
                if item.get("name") == self.coin
            ),
            None,
        )

        if self.market is None:
            raise RuntimeError(
                f"Market not found: {self.coin}"
            )

        if self.market.get("isDelisted"):
            raise RuntimeError(
                f"Market is delisted: {self.coin}"
            )

        self.sz_decimals = int(
            self.market["szDecimals"]
        )

        self.size_step = Decimal("1").scaleb(
            -self.sz_decimals
        )

    def status(self) -> dict[str, Any]:
        """
        只读状态，不发送写请求。
        """
        state = self.info.user_state(
            self.account_address,
            dex=self.dex,
        )

        margin = state.get(
            "marginSummary",
            {},
        )

        return {
            "network": self.network,
            "dex": self.dex,
            "coin": self.coin,
            "ticker": self.ticker,
            "main_account": self.account_address,
            "signer": self.wallet.address,
            "trading_enabled": self.trading_enabled,
            "sz_decimals": self.sz_decimals,
            "max_leverage": self.market.get(
                "maxLeverage"
            ),
            "only_isolated": self.market.get(
                "onlyIsolated"
            ),
            "margin_mode": self.market.get(
                "marginMode"
            ),
            "account_value": margin.get(
                "accountValue"
            ),
            "withdrawable": state.get(
                "withdrawable"
            ),
            "positions": len(
                state.get("assetPositions", [])
            ),
        }

    def normalize_size(
        self,
        size: Decimal | float | str,
    ) -> Decimal:
        """
        严格按 szDecimals 向下取整。
        不允许自动扩大数量。
        """
        value = Decimal(str(size))

        if value <= 0:
            raise ValueError(
                "Order size must be positive"
            )

        normalized = value.quantize(
            self.size_step,
            rounding=ROUND_DOWN,
        )

        if normalized <= 0:
            raise ValueError(
                "Order size becomes zero after rounding"
            )

        return normalized

    def _require_trading(self) -> None:
        if not self.trading_enabled:
            raise RuntimeError(
                "Entropy trading is disabled; "
                "set ENTROPY_TRADING_ENABLED=true "
                "only after final approval"
            )

    @staticmethod
    def _limit_order_type(
        post_only: bool,
    ) -> dict[str, dict[str, str]]:
        if post_only:
            return {
                "limit": {
                    "tif": "Alo",
                }
            }

        return {
            "limit": {
                "tif": "Gtc",
            }
        }

    def place_open_order(
        self,
        is_buy: bool,
        size: Decimal | float | str,
        price: Decimal | float | str,
        post_only: bool = True,
    ) -> Any:
        """
        开仓订单。

        当前交易开关为 false 时一定拒绝。
        """
        self._require_trading()

        normalized_size = self.normalize_size(size)
        limit_price = float(
            Decimal(str(price))
        )

        if limit_price <= 0:
            raise ValueError(
                "Limit price must be positive"
            )

        return self.exchange.order(
            self.coin,
            is_buy,
            float(normalized_size),
            limit_price,
            self._limit_order_type(post_only),
            reduce_only=False,
        )

    def place_reduce_only_close(
        self,
        is_buy: bool,
        size: Decimal | float | str,
        price: Decimal | float | str,
        post_only: bool = True,
    ) -> Any:
        """
        reduceOnly 平仓。

        平多仓通常使用 is_buy=False。
        平空仓通常使用 is_buy=True。
        """
        self._require_trading()

        normalized_size = self.normalize_size(size)
        limit_price = float(
            Decimal(str(price))
        )

        if limit_price <= 0:
            raise ValueError(
                "Limit price must be positive"
            )

        return self.exchange.order(
            self.coin,
            is_buy,
            float(normalized_size),
            limit_price,
            self._limit_order_type(post_only),
            reduce_only=True,
        )

    def cancel_order(
        self,
        oid: int,
    ) -> Any:
        """
        撤单。
        """
        self._require_trading()

        if int(oid) <= 0:
            raise ValueError(
                "Order id must be positive"
            )

        return self.exchange.cancel(
            self.coin,
            int(oid),
        )

    def query_order(
        self,
        oid: int,
    ) -> Any:
        """
        只读查单，不需要交易开关。
        """
        if int(oid) <= 0:
            raise ValueError(
                "Order id must be positive"
            )

        return self.info.query_order_by_oid(
            self.account_address,
            int(oid),
        )

    def open_orders(self) -> list[dict[str, Any]]:
        """
        只读查询 io 全部活动订单。
        """
        return self.info.frontend_open_orders(
            self.account_address,
            dex=self.dex,
        )

    def user_fills(self) -> list[dict[str, Any]]:
        """
        只读查询账户成交。
        """
        return self.info.user_fills(
            self.account_address,
        )
