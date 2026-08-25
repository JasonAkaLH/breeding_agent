from __future__ import annotations

import json
import unittest

from src.integrations.agent_skills import (
    contract,
    execution,
    input_resolution,
    input_schema,
    missing_input_interrupt,
    slot_state,
)
from src.integrations.agent_skills._parsing import load_json_object, string_tuple


class SharedSkillParsingHelpersTest(unittest.TestCase):
    def test_string_tuple_aliases_share_one_exact_behavior(self) -> None:
        self.assertIs(contract._string_tuple, string_tuple)
        self.assertIs(input_schema._string_tuple, string_tuple)
        self.assertIs(slot_state._string_tuple, string_tuple)
        fixtures = (
            (None, ()),
            ("", ()),
            ("  alpha  ", ("alpha",)),
            ([" alpha ", "", 3], ("alpha", "3")),
            (("a", " b "), ("a", "b")),
            ({"z"}, ("z",)),
            ({"unsupported": True}, ()),
        )
        for value, expected in fixtures:
            self.assertEqual(string_tuple(value), expected)

    def test_json_object_aliases_share_one_exact_behavior(self) -> None:
        self.assertIs(execution._load_v2_json_object, load_json_object)
        self.assertIs(input_resolution._load_json_object, load_json_object)
        self.assertIs(missing_input_interrupt._load_json_object, load_json_object)
        self.assertEqual(load_json_object('{"field": 1}'), {"field": 1})
        self.assertEqual(
            load_json_object('prefix {"field": 2} suffix'),
            {"field": 2},
        )
        for payload, message in (
            ("", "empty response"),
            ("[]", "response is not a JSON object"),
            ("not-json", "Expecting value"),
        ):
            with self.assertRaisesRegex(json.JSONDecodeError, message):
                load_json_object(payload)


if __name__ == "__main__":
    unittest.main()
