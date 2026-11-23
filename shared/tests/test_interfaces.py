"""Specified endpoint identities and request-field validation."""

import unittest

from eacld_contracts.interfaces import (
    CONTROL_AGENT_PORT,
    CONTROL_HEALTH_PATH,
    MQTT_BROKER_HOST,
    MQTT_BROKER_PORT,
    NODE_CHALLENGE,
    PLANT_STATE,
    REFERENCE_READ,
    parse_challenge_request,
)


class InterfaceContractTests(unittest.TestCase):
    def test_endpoints(self) -> None:
        self.assertEqual((PLANT_STATE.method, PLANT_STATE.url),
                         ("GET", "http://10.60.40.10:8100/v1/state"))
        self.assertEqual((REFERENCE_READ.method, REFERENCE_READ.url),
                         ("POST", "http://10.60.20.13:8201/v1/reference/read"))
        self.assertEqual((NODE_CHALLENGE.method, NODE_CHALLENGE.url),
                         ("POST", "http://10.60.20.12:8202/v1/challenge"))
        self.assertEqual((CONTROL_AGENT_PORT, CONTROL_HEALTH_PATH), (8300, "/healthz"))
        self.assertEqual((MQTT_BROKER_HOST, MQTT_BROKER_PORT), ("10.60.10.14", 1883))

    def test_challenge_request(self) -> None:
        value = {"node_id": 65535, "nonce_hex": "00" * 16}
        self.assertEqual(parse_challenge_request(value), (65535, bytes(16)))
        for bad in ({"node_id": -1, "nonce_hex": "00" * 16},
                    {"node_id": 1, "nonce_hex": "ff" * 15},
                    {"node_id": 1, "nonce_hex": "not hex"},
                    {"node_id": True, "nonce_hex": "00" * 16}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_challenge_request(bad)


if __name__ == "__main__":
    unittest.main()
