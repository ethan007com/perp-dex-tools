import asyncio
import unittest
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from exchanges.entropy import EntropyClient

def order_response(status="open", remaining="0.004"):
    return {
        "status": "order",
        "order": {
            "status": status,
            "order": {
                "oid": 12345,
                "coin": "io:NVDA",
                "side": "B",
                "origSz": "0.010",
                "sz": remaining,
                "limitPx": "207.43",
            },
        },
    }

def client_with(response):
    client = object.__new__(EntropyClient)
    client.contract_id = "io:NVDA"
    client.gateway = SimpleNamespace(
        coin="io:NVDA",
        query_order=Mock(return_value=deepcopy(response)),
        open_orders=Mock(return_value=[]),
        info=SimpleNamespace(l2_snapshot=Mock(return_value={
            "levels": [
                [{"px": "207.43"}],
                [{"px": "207.48"}],
            ],
        })),
    )
    return client

class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_bbo_tuple(self):
        client = client_with(order_response())
        bid, ask = await client.fetch_bbo_prices("io:NVDA")
        self.assertEqual(bid, Decimal("207.43"))
        self.assertEqual(ask, Decimal("207.48"))
        self.assertEqual(await client.get_order_price("buy"), bid)
        self.assertEqual(await client.get_order_price("sell"), ask)

    async def test_partial_fill_nested(self):
        result = await client_with(order_response()).get_order_info("12345")
        self.assertEqual(result.side, "BUY")
        self.assertEqual(result.status, "PARTIALLY_FILLED")
        self.assertEqual(result.size, Decimal("0.010"))
        self.assertEqual(result.filled_size, Decimal("0.006"))
        self.assertEqual(result.remaining_size, Decimal("0.004"))

    async def test_open(self):
        result = await client_with(
            order_response("open", "0.010")
        ).get_order_info("12345")
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(result.filled_size, Decimal("0"))

    async def test_filled_numeric_zero(self):
        result = await client_with(
            order_response("filled", 0)
        ).get_order_info("12345")
        self.assertEqual(result.status, "FILLED")
        self.assertEqual(result.filled_size, Decimal("0.010"))
        self.assertEqual(result.remaining_size, Decimal("0"))

    async def test_canceled_after_partial_fill(self):
        result = await client_with(
            order_response("canceled", "0.004")
        ).get_order_info("12345")
        self.assertEqual(result.status, "CANCELED")
        self.assertEqual(result.filled_size, Decimal("0.006"))

    async def test_unknown_not_treated_as_canceled(self):
        client = client_with({"status": "unknownOid"})
        with self.assertRaises(RuntimeError):
            await client.get_order_info("12345")

    async def test_wrong_order_id(self):
        response = order_response()
        response["order"]["order"]["oid"] = 999
        with self.assertRaises(RuntimeError):
            await client_with(response).get_order_info("12345")

    async def test_transport_failure_not_retried(self):
        client = client_with(order_response())
        client.gateway.query_order.side_effect = TimeoutError("simulated")
        with self.assertRaises(TimeoutError):
            await client.get_order_info("12345")
        self.assertEqual(client.gateway.query_order.call_count, 1)

    async def test_active_order_normalization(self):
        client = client_with(order_response())
        client.gateway.open_orders.return_value = [
            order_response()["order"]["order"]
        ]
        orders = await client.get_active_orders("io:NVDA")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].side, "BUY")
        self.assertEqual(orders[0].status, "PARTIALLY_FILLED")

    async def test_submit_status_normalization(self):
        for payload, expected in [
            ({"resting": {"oid": 1}}, "OPEN"),
            ({"filled": {"oid": 2, "totalSz": "0.010"}}, "FILLED"),
            ({"error": "simulated rejection"}, "REJECTED"),
        ]:
            with self.subTest(expected=expected):
                response = {
                    "status": "ok",
                    "response": {"data": {"statuses": [payload]}},
                }
                result = EntropyClient._order_result(
                    response, "BUY", Decimal("0.010"), Decimal("207.43")
                )
                self.assertEqual(result.status, expected)

if __name__ == "__main__":
    unittest.main()
