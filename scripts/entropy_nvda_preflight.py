from pathlib import Path
from decimal import Decimal

from dotenv import dotenv_values
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.utils.constants import MAINNET_API_URL

ROOT = Path.cwd().resolve()
EXPECTED = (
    Path.home() / "perp-dex-tools-entropy"
).resolve()

if ROOT != EXPECTED:
    raise SystemExit("Entropy 目录错误")

cfg = dotenv_values(
    ROOT / ".env.entropy",
    interpolate=False,
)

if cfg.get("ENTROPY_NETWORK") != "mainnet":
    raise SystemExit("ENTROPY_NETWORK 必须是 mainnet")

if cfg.get("ENTROPY_DEX") != "io":
    raise SystemExit("ENTROPY_DEX 必须是 io")

if cfg.get("ENTROPY_TICKER") != "NVDA":
    raise SystemExit("ENTROPY_TICKER 必须是 NVDA")

if cfg.get("ENTROPY_COIN") != "io:NVDA":
    raise SystemExit("ENTROPY_COIN 必须是 io:NVDA")

if cfg.get("ENTROPY_TRADING_ENABLED") != "false":
    raise SystemExit(
        "交易开关不是 false，停止预检"
    )

main = cfg.get(
    "ENTROPY_ACCOUNT_ADDRESS",
    "",
).strip()

secret = cfg.get(
    "ENTROPY_SECRET_KEY",
    "",
).strip()

if not main or not secret:
    raise SystemExit(
        "主账户或私钥配置缺失"
    )

signer = Account.from_key(secret)

def mask(address):
    return address[:6] + "..." + address[-4:]

print("主账户:", mask(main))
print("签名账户:", mask(signer.address))
print(
    "主账户与签名账户不同:",
    signer.address.lower() != main.lower(),
)
print("DEX: io")
print("Coin: io:NVDA")
print("交易开关:", cfg["ENTROPY_TRADING_ENABLED"])

info = Info(
    MAINNET_API_URL,
    skip_ws=True,
    perp_dexs=["io", "xyz"],
    timeout=20,
)

def show_perp(label, dex):
    state = info.user_state(
        main,
        dex=dex,
    )

    margin = state.get(
        "marginSummary",
        {},
    )

    print(f"\n========== {label} ==========")
    print("dex:", repr(dex))
    print(
        "accountValue:",
        margin.get("accountValue"),
    )
    print(
        "totalMarginUsed:",
        margin.get("totalMarginUsed"),
    )
    print(
        "withdrawable:",
        state.get("withdrawable"),
    )
    print(
        "positions:",
        len(state.get("assetPositions", [])),
    )

print("\n========== Spot USDC ==========")
spot = info.spot_user_state(main)

for balance in spot.get("balances", []):
    if balance.get("coin") == "USDC":
        print("total:", balance.get("total"))
        print("hold:", balance.get("hold"))

show_perp("Default Perp", "")
show_perp("Entropy io", "io")
show_perp("XYZ", "xyz")

print("\n========== io:NVDA Market ==========")

meta = info.meta(dex="io")

market = next(
    (
        item
        for item in meta.get("universe", [])
        if item.get("name") == "io:NVDA"
    ),
    None,
)

if market is None:
    raise SystemExit(
        "找不到 io:NVDA 市场"
    )

print("market:", market.get("name"))
print(
    "szDecimals:",
    market.get("szDecimals"),
)
print(
    "maxLeverage:",
    market.get("maxLeverage"),
)
print(
    "onlyIsolated:",
    market.get("onlyIsolated"),
)
print(
    "marginMode:",
    market.get("marginMode"),
)
print(
    "isDelisted:",
    market.get("isDelisted"),
)

if market.get("isDelisted"):
    raise SystemExit(
        "io:NVDA 已下架，停止"
    )

book = info.l2_snapshot("io:NVDA")
levels = book.get("levels", [])

if len(levels) != 2:
    raise SystemExit(
        "io:NVDA 盘口格式异常"
    )

if not levels[0] or not levels[1]:
    raise SystemExit(
        "io:NVDA 盘口为空"
    )

bid = Decimal(levels[0][0]["px"])
ask = Decimal(levels[1][0]["px"])

print("best_bid:", bid)
print("best_ask:", ask)
print("spread:", ask - bid)

size_decimals = int(
    market["szDecimals"]
)
size_step = Decimal(1).scaleb(
    -size_decimals
)

print("size_step:", size_step)
print("\nwrites executed: 0")
print("Entropy NVDA preflight: OK")
