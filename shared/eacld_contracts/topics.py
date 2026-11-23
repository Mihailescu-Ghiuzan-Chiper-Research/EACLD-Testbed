"""MQTT topic routing and principal permissions from architecture V1."""

from __future__ import annotations

from .frame import _check_unsigned

RAW_SUBSCRIPTION = "raw/+"
G1_TRANSIT_SUBSCRIPTION = "transit/g1/+"
ACCEPTED_SUBSCRIPTION = "accepted/+"

# Permission declarations correspond to the four principals in the V1 ACL.
MQTT_PERMISSIONS = {
    "node": {"write": (RAW_SUBSCRIPTION,), "read": ()},
    "g1": {"read": (RAW_SUBSCRIPTION,), "write": (G1_TRANSIT_SUBSCRIPTION,)},
    "g2": {"read": (G1_TRANSIT_SUBSCRIPTION,), "write": (ACCEPTED_SUBSCRIPTION,)},
    "core": {"read": (ACCEPTED_SUBSCRIPTION,), "write": ()},
}


def raw_topic(node_id: int) -> str:
    _check_unsigned("node_id", node_id, 16)
    return f"raw/{node_id}"


def g1_transit_topic(node_id: int) -> str:
    _check_unsigned("node_id", node_id, 16)
    return f"transit/g1/{node_id}"


def accepted_topic(node_id: int) -> str:
    _check_unsigned("node_id", node_id, 16)
    return f"accepted/{node_id}"
