from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal

from exchanges.entropy_order_gateway import (
    EntropyOrderGateway,
)

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--env-file",
        default=os.environ.get(
            "PERP_DEX_ENV_FILE",
            ".env",
        ),
    )
    return parser.parse_args()

def dump(label, value):
    print(f"\n========== {label} ==========")
    print(
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            default=str,
        )
    )

def main():
    args = parse_args()
    gateway = EntropyOrderGateway(
        args.env_file
    )

    address = gateway.account_address
    info = gateway.info

    print("env_file:", args.env_file)
    print("dex:", gateway.dex)
    print("coin:", gateway.coin)
    print(
        "account:",
        address[:6] + "..." + address[-4:],
    )
    print(
        "writes executed: 0"
    )

    spot = info.spot_user_state(address)
    dump("Spot User State", spot)

    default_perp = info.user_state(address)
    dump("Default Perp User State", default_perp)

    entropy_perp = info.user_state(
        address,
        dex=gateway.dex,
    )
    dump("Entropy io User State", entropy_perp)

    abstraction = info.query_user_abstraction_state(
        address
    )
    dump("Unified Account Abstraction", abstraction)

    dex_abstraction_method = getattr(
        info,
        "query_user_dex_abstraction_state",
        None,
    )

    if dex_abstraction_method is not None:
        try:
            dex_abstraction = (
                dex_abstraction_method(address)
            )
        except Exception as exc:
            print(
                "\n========== DEX Abstraction State =========="
            )
            print(
                "unavailable:",
                type(exc).__name__,
            )
            print(
                "reason:",
                str(exc)[:300],
            )
        else:
            dump(
                "DEX Abstraction State",
                dex_abstraction,
            )

    print("\n========== Summary ==========")

    print(
        "spot_state_type:",
        type(spot).__name__,
    )
    print(
        "default_perp_type:",
        type(default_perp).__name__,
    )
    print(
        "entropy_perp_type:",
        type(entropy_perp).__name__,
    )
    print(
        "abstraction_type:",
        type(abstraction).__name__,
    )

    print(
        "\nNo order, cancel, transfer, "
        "or withdrawal was executed."
    )

if __name__ == "__main__":
    main()
