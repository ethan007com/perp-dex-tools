import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if not (ROOT / "exchanges").is_dir():
    raise SystemExit("无法定位项目根目录")

sys.path.insert(0, str(ROOT))

from exchanges.entropy_order_gateway import (
    EntropyOrderGateway,
)

def mask(address: str) -> str:
    return address[:6] + "..." + address[-4:]

env_file = os.environ.get(
    "PERP_DEX_ENV_FILE",
    str(ROOT / ".env"),
)

gateway = EntropyOrderGateway(env_file)

status = gateway.status()

print("Entropy gateway: initialized")
print("main account:", mask(status["main_account"]))
print("signer:", mask(status["signer"]))
print("dex:", status["dex"])
print("coin:", status["coin"])
print("ticker:", status["ticker"])
print(
    "trading_enabled:",
    status["trading_enabled"],
)
print("sz_decimals:", status["sz_decimals"])
print("max_leverage:", status["max_leverage"])
print("only_isolated:", status["only_isolated"])
print("margin_mode:", status["margin_mode"])
print("account_value:", status["account_value"])
print("withdrawable:", status["withdrawable"])
print("positions:", status["positions"])

orders = gateway.open_orders()
nvda_orders = [
    order
    for order in orders
    if order.get("coin") == "io:NVDA"
]

print("io orders:", len(orders))
print("io:NVDA orders:", len(nvda_orders))
print("writes executed: 0")

if status["trading_enabled"]:
    raise SystemExit(
        "交易开关必须保持 false"
    )

print("Entropy gateway read-only test: OK")
