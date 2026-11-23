"""MQTT routing checks against architecture V1."""

import unittest

from eacld_contracts.topics import (
    ACCEPTED_SUBSCRIPTION,
    G1_TRANSIT_SUBSCRIPTION,
    MQTT_PERMISSIONS,
    RAW_SUBSCRIPTION,
    accepted_topic,
    g1_transit_topic,
    raw_topic,
)


class TopicContractTests(unittest.TestCase):
    def test_topic_progression(self) -> None:
        self.assertEqual(raw_topic(12), "raw/12")
        self.assertEqual(g1_transit_topic(12), "transit/g1/12")
        self.assertEqual(accepted_topic(12), "accepted/12")
        self.assertEqual((RAW_SUBSCRIPTION, G1_TRANSIT_SUBSCRIPTION, ACCEPTED_SUBSCRIPTION),
                         ("raw/+", "transit/g1/+", "accepted/+"))

    def test_permissions(self) -> None:
        self.assertEqual(MQTT_PERMISSIONS["node"]["write"], ("raw/+",))
        self.assertEqual(MQTT_PERMISSIONS["g1"]["read"], ("raw/+",))
        self.assertEqual(MQTT_PERMISSIONS["g2"]["read"], ("transit/g1/+",))
        self.assertEqual(MQTT_PERMISSIONS["core"]["read"], ("accepted/+",))
        self.assertEqual(MQTT_PERMISSIONS["core"]["write"], ())

    def test_invalid_node_id(self) -> None:
        for invalid in (-1, 65536, "12", True):
            with self.subTest(node_id=invalid), self.assertRaises(ValueError):
                raw_topic(invalid)


if __name__ == "__main__":
    unittest.main()
