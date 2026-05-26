from __future__ import annotations

import unittest

from src.state.postgres.handlers import DEFAULT_HANDLER_REGISTRY, PRODUCTION_COMMAND_TYPES


class StateCommandHandlersTest(unittest.TestCase):
    def test_all_production_command_types_are_registered_with_partition_and_lock_policy(self) -> None:
        self.assertEqual(set(DEFAULT_HANDLER_REGISTRY.command_types()), PRODUCTION_COMMAND_TYPES)
        for command_type in PRODUCTION_COMMAND_TYPES:
            spec = DEFAULT_HANDLER_REGISTRY.get(command_type)
            self.assertEqual(spec.command_type, command_type)
            self.assertTrue(spec.partition_scope in {"conversation", "task", "auth", "system"})
            self.assertTrue(spec.lock_order)
            self.assertFalse(spec.allows_external_io)
            self.assertTrue(spec.idempotency_fields)

    def test_external_io_is_forbidden_inside_handler_transactions(self) -> None:
        forbidden = DEFAULT_HANDLER_REGISTRY.handlers_allowing_external_io()
        self.assertEqual(forbidden, [])
