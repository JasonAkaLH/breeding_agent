from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from unittest.mock import patch

from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import create_engine

from src.auth.generation_cache import AuthGenerationCache
from src.auth.invalidation_bus import AuthGenerationChanged, InMemoryAuthInvalidationBus
from src.auth.postgres_invalidation_bus import CHANNEL, PostgresAuthInvalidationBus


class AuthInvalidationBusTest(unittest.IsolatedAsyncioTestCase):
    async def test_fake_bus_publishes_redacted_payload_to_multiple_subscribers(self) -> None:
        bus = InMemoryAuthInvalidationBus()
        first = bus.subscribe()
        second = bus.subscribe()
        event = AuthGenerationChanged("alice", 2, datetime(2026, 5, 26, 12, 0, 0), "refresh")
        await bus.publish(event)
        self.assertEqual((await asyncio.wait_for(first.get(), timeout=1)).auth_generation, 2)
        self.assertEqual((await asyncio.wait_for(second.get(), timeout=1)).reason, "refresh")
        payload = event.public_payload()
        self.assertEqual(set(payload), {"username", "auth_generation", "changed_at", "reason"})
        self.assertNotIn("maf_tok", repr(payload))
        self.assertNotIn("hash", repr(payload).lower())

    async def test_unsubscribe_stops_delivery(self) -> None:
        bus = InMemoryAuthInvalidationBus()
        queue = bus.subscribe()
        bus.unsubscribe(queue)
        await bus.publish(AuthGenerationChanged("alice", 1, datetime(2026, 5, 26), "login"))
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)

    async def test_postgres_listener_registers_listen_before_reconcile(self) -> None:
        call_order: list[str] = []
        engine = create_engine("postgresql+psycopg://user:pass@localhost/db")
        bus = PostgresAuthInvalidationBus(engine, AuthGenerationCache())

        def reconcile_once() -> None:
            call_order.append("reconcile")

        bus.reconcile_once = reconcile_once  # type: ignore[method-assign]

        class FakeConnection:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def execute(self, sql):
                call_order.append(str(sql))

            def notifies(self, *, timeout: float, stop_after: int):
                async def _generator():
                    bus._closed = True
                    if False:
                        yield None

                return _generator()

        async def fake_connect(*_args, **_kwargs):
            return FakeConnection()

        with patch("src.auth.postgres_invalidation_bus.psycopg.AsyncConnection.connect", side_effect=fake_connect):
            await bus._listen_loop()

        self.assertLess(call_order.index(f"LISTEN {CHANNEL}"), call_order.index("reconcile"))
        self.assertTrue(bus.health.ready)

    def test_postgres_listener_normalizes_sqlalchemy_driver_url_for_psycopg(self) -> None:
        engine = create_engine("postgresql+psycopg://user:pass@localhost:15432/db")
        bus = PostgresAuthInvalidationBus(engine, AuthGenerationCache())
        conninfo = conninfo_to_dict(bus._dsn)
        self.assertEqual(bus._dsn, "postgresql://user:pass@localhost:15432/db")
        self.assertEqual(conninfo["user"], "user")
        self.assertEqual(conninfo["password"], "pass")


if __name__ == "__main__":
    unittest.main()
