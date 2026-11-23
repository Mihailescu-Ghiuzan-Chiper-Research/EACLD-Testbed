"""G2 routing, MQTT, intervention, and deployment contract tests."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
import urllib.error
import urllib.request

from src.g2 import Gateway2, create_health_server, route_transit_topic

EDGE_ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    def __init__(self):
        self.subscriptions = []
        self.publications = []
        self.publish_rc = 0
        self.subscribe_rc = 0

    def subscribe(self, topic, qos):
        self.subscriptions.append((topic, qos))
        return self.subscribe_rc, 42

    def publish(self, topic, payload, qos, retain):
        self.publications.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=self.publish_rc)


def frame(seed=0):
    header = bytes((i + seed) % 256 for i in range(62))
    return header + hmac.new(b"test-key", header, hashlib.sha256).digest()


def message(node, payload):
    return SimpleNamespace(topic=f"transit/g1/{node}", payload=payload)


class RoutingTests(unittest.TestCase):
    def test_canonical_topics(self):
        for node in (0, 12, 65535):
            self.assertEqual(route_transit_topic(f"transit/g1/{node}"),
                             (node, f"accepted/{node}"))

    def test_invalid_topics(self):
        for topic in ("raw/12", "accepted/12", "transit/g1/",
                      "transit/g1/001", "transit/g1/-1", "transit/g1/65536",
                      "transit/g1/+", "transit/g1/1/extra", "transit/g2/1",
                      "transit/g1/\u0661", None):
            with self.subTest(topic=topic), self.assertRaises(ValueError):
                route_transit_topic(topic)


class ForwardingTests(unittest.TestCase):
    def test_normal_forward_is_byte_exact(self):
        gateway, client = Gateway2(), FakeClient()
        original = frame()
        gateway.on_message(client, None, message(12, original))
        self.assertEqual(client.publications, [("accepted/12", original, 1, False)])

    def test_invalid_mac_and_opaque_data_preserved(self):
        gateway, client = Gateway2(), FakeClient()
        corrupt = bytearray(frame())
        corrupt[-1] ^= 255
        for packet in (bytes(corrupt), b"opaque", bytes(corrupt)):
            gateway.on_message(client, None, message(12, packet))
        self.assertEqual([x[1] for x in client.publications],
                         [bytes(corrupt), b"opaque", bytes(corrupt)])

    def test_invalid_topic_and_nonbytes_ignored(self):
        gateway, client = Gateway2(), FakeClient()
        gateway.on_message(client, None,
                           SimpleNamespace(topic="raw/12", payload=frame()))
        gateway.on_message(client, None, message(12, "invalid"))
        self.assertEqual(client.publications, [])

    def test_mutation_changes_only_header_once(self):
        gateway, client = Gateway2(), FakeClient()
        original = frame()
        gateway.stage_mutation(12, offset=5, xor_mask=64)
        gateway.on_message(client, None, message(11, original))
        gateway.on_message(client, None, message(12, original))
        gateway.on_message(client, None, message(12, original))
        changed = client.publications[1][1]
        self.assertEqual(changed[:5], original[:5])
        self.assertEqual(changed[5], original[5] ^ 64)
        self.assertEqual(changed[6:], original[6:])
        self.assertEqual(changed[-32:], original[-32:])
        self.assertNotEqual(hmac.new(b"test-key", changed[:62], hashlib.sha256).digest(),
                            changed[-32:])
        self.assertEqual(client.publications[2][1], original)

    def test_invalid_frame_defers_mutation(self):
        gateway, client = Gateway2(), FakeClient()
        gateway.stage_mutation(12, offset=1, xor_mask=1)
        gateway.on_message(client, None, message(12, b"wrong length"))
        gateway.on_message(client, None, message(12, frame()))
        self.assertEqual(client.publications[0][1], b"wrong length")
        self.assertNotEqual(client.publications[1][1], frame())

    def test_replay_emits_prior_snapshot_once(self):
        gateway, client = Gateway2(), FakeClient()
        old, fresh = frame(0), frame(1)
        gateway.on_message(client, None, message(12, old))
        gateway.stage_replay(12)
        gateway.on_message(client, None, message(15, frame(4)))
        gateway.on_message(client, None, message(12, fresh))
        gateway.on_message(client, None, message(12, fresh))
        self.assertEqual([p[1] for p in client.publications],
                         [old, frame(4), fresh, old, fresh])

    def test_replay_requires_a_previous_frame(self):
        with self.assertRaises(ValueError):
            Gateway2().stage_replay(12)

    def test_clear_cancels_action(self):
        gateway, client = Gateway2(), FakeClient()
        gateway.stage_mutation(12, offset=1, xor_mask=1)
        gateway.clear_intervention(12)
        gateway.on_message(client, None, message(12, frame()))
        self.assertEqual(client.publications[0][1], frame())

    def test_rejects_invalid_parameters(self):
        gateway = Gateway2()
        for node in (True, -1, 65536, "12"):
            with self.assertRaises(ValueError):
                gateway.stage_mutation(node, offset=1, xor_mask=1)
        for offset in (-1, 62, 93, True):
            with self.assertRaises(ValueError):
                gateway.stage_mutation(12, offset=offset, xor_mask=1)
        for mask in (0, 256, -1, True):
            with self.assertRaises(ValueError):
                gateway.stage_mutation(12, offset=1, xor_mask=mask)

    def test_failed_publication_keeps_action(self):
        gateway, client = Gateway2(), FakeClient()
        gateway.stage_mutation(12, offset=1, xor_mask=1)
        client.publish_rc = 5
        with self.assertLogs("src.g2", level="ERROR"):
            gateway.on_message(client, None, message(12, frame()))
        client.publish_rc = 0
        gateway.on_message(client, None, message(12, frame()))
        self.assertEqual(client.publications[-1][1][1], frame()[1] ^ 1)


class LifecycleTests(unittest.TestCase):
    def test_subscription_and_disconnect(self):
        gateway, client = Gateway2(), FakeClient()
        gateway.on_connect(client, None, None, 0, None)
        self.assertEqual(client.subscriptions, [("transit/g1/+", 1)])
        gateway.on_subscribe(client, None, 41, [SimpleNamespace(is_failure=False)], None)
        self.assertFalse(gateway.ready.is_set())
        gateway.on_subscribe(client, None, 42, [SimpleNamespace(is_failure=False)], None)
        self.assertTrue(gateway.ready.is_set())
        gateway.on_disconnect(client, None, None, 0, None)
        self.assertFalse(gateway.ready.is_set())

    def test_failed_subscription_not_ready(self):
        gateway, client = Gateway2(), FakeClient()
        gateway.on_connect(client, None, None, 0, None)
        with self.assertLogs("src.g2", level="ERROR"):
            gateway.on_subscribe(client, None, 42, [SimpleNamespace(is_failure=True)], None)
        self.assertFalse(gateway.ready.is_set())

    def test_rejected_connection(self):
        gateway, client = Gateway2(), FakeClient()
        with self.assertLogs("src.g2", level="ERROR"):
            gateway.on_connect(client, None, None, 5, None)
        self.assertFalse(client.subscriptions)

    def test_local_health_only(self):
        gateway = Gateway2()
        server = create_health_server(gateway, port=0)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                opener.open(url + "/healthz", timeout=2)
            self.assertEqual(error.exception.code, 503)
            gateway.ready.set()
            with opener.open(url + "/healthz", timeout=2) as response:
                self.assertEqual(json.load(response),
                                 {"status": "ok", "service_version": "0.1.0"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                opener.open(url + "/control", timeout=2)
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class ConfigurationTests(unittest.TestCase):
    def test_no_keys_or_crypto_signing(self):
        source = (EDGE_ROOT / "services/g2/src/g2.py").read_text()
        for forbidden in ("sign_frame", "verify_frame", "hmac.new", "node_keys"):
            self.assertNotIn(forbidden, source)

    def test_broker_only_external_port(self):
        compose = (EDGE_ROOT / "compose.yaml").read_text()
        self.assertEqual(compose.count("    ports:"), 1)
        self.assertIn('"10.60.10.14:1883:1883"', compose)
        self.assertIn("./secrets/g2_mqtt_password:/run/eacld/mqtt_password:ro", compose)

    def test_g2_acl_is_read_transit_write_accepted(self):
        acl = (EDGE_ROOT / "config/acl").read_text()
        self.assertIn("user g2\ntopic read transit/g1/+\ntopic write accepted/+", acl)


if __name__ == "__main__":
    unittest.main()
