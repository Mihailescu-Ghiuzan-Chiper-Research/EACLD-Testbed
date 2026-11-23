"""Local G1 and Mosquitto-configuration contract tests (architecture V1)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
import urllib.error
import urllib.request

from eacld_contracts.frame import Frame, FrameHeader, sign_frame, verify_frame
from src.g1 import Gateway1, create_health_server, route_raw_topic

EDGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = EDGE_ROOT.parent


class FakeClient:
    def __init__(self):
        self.subscriptions = []
        self.publications = []
        self.subscribe_rc = 0
        self.publish_rc = 0

    def subscribe(self, topic, qos):
        self.subscriptions.append((topic, qos))
        return self.subscribe_rc, 42

    def publish(self, topic, payload, qos, retain):
        self.publications.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=self.publish_rc)


def frame_bytes():
    header = FrameHeader(
        version=1, type=1, node_id=12, zone_id=3, key_version=1,
        boot_id=bytes(range(16)), sequence_no=5, uptime_ms=10,
        temperature_mC=-15789, humidity_mPct=35000,
        nonce=bytes(range(16, 32)),
    )
    return sign_frame(header, b"S" * 32).to_bytes()


class RouteTests(unittest.TestCase):
    def test_canonical_topic_mapping(self):
        self.assertEqual(route_raw_topic("raw/0"), "transit/g1/0")
        self.assertEqual(route_raw_topic("raw/12"), "transit/g1/12")
        self.assertEqual(route_raw_topic("raw/65535"), "transit/g1/65535")

    def test_invalid_topic_rejected(self):
        invalid = ("raw/", "raw/-1", "raw/+", "raw/#", "raw/12/other",
                   "raw/65536", "raw/0001", "raw/ 12", "raw/1.0",
                   "accepted/12", "transit/g1/12", "raw/\u0661", None)
        for topic in invalid:
            with self.subTest(topic=topic), self.assertRaises(ValueError):
                route_raw_topic(topic)

    def test_preserves_frame_bytes_and_authenticator(self):
        data = frame_bytes()
        gateway, client = Gateway1(), FakeClient()
        gateway.on_message(client, None, SimpleNamespace(topic="raw/12", payload=data))
        self.assertEqual(client.publications, [("transit/g1/12", data, 1, False)])
        self.assertTrue(verify_frame(Frame.from_bytes(client.publications[0][1]), b"S" * 32))
        self.assertEqual(len(data), 94)

    def test_does_not_repair_tampered_hmac(self):
        frame = bytearray(frame_bytes())
        frame[-1] ^= 0xFF
        tampered = bytes(frame)
        gateway, client = Gateway1(), FakeClient()
        gateway.on_message(client, None, SimpleNamespace(topic="raw/12", payload=tampered))
        self.assertEqual(client.publications[0][1], tampered)
        self.assertFalse(verify_frame(Frame.from_bytes(client.publications[0][1]), b"S" * 32))
        self.assertNotEqual(client.publications[0][1], frame_bytes())

    def test_no_payload_mutation_or_deduplication(self):
        opaque = bytes([0, 255, 0, 1]) + b"not necessarily authenticated"
        gateway, client = Gateway1(), FakeClient()
        msg = SimpleNamespace(topic="raw/99", payload=opaque)
        gateway.on_message(client, None, msg)
        gateway.on_message(client, None, msg)
        self.assertEqual(client.publications, [
            ("transit/g1/99", opaque, 1, False),
            ("transit/g1/99", opaque, 1, False),
        ])

    def test_rejects_invalid_topic_without_publish(self):
        gateway, client = Gateway1(), FakeClient()
        gateway.on_message(client, None, SimpleNamespace(topic="raw/abc", payload=frame_bytes()))
        self.assertEqual(client.publications, [])

    def test_nonbytes_payload_ignored(self):
        gateway, client = Gateway1(), FakeClient()
        gateway.on_message(client, None, SimpleNamespace(topic="raw/1", payload="not bytes"))
        self.assertEqual(client.publications, [])

    def test_publish_failure_does_not_rewrite_payload(self):
        gateway, client = Gateway1(), FakeClient()
        client.publish_rc = 5
        data = frame_bytes()
        with self.assertLogs("src.g1", level="ERROR"):
            gateway.on_message(client, None, SimpleNamespace(topic="raw/12", payload=data))
        self.assertEqual(client.publications[0][1], data)


class LifecycleTests(unittest.TestCase):
    def test_subscribes_only_to_raw_with_qos_one(self):
        gateway, client = Gateway1(), FakeClient()
        gateway.on_connect(client, None, None, 0, None)
        self.assertEqual(client.subscriptions, [("raw/+", 1)])
        self.assertFalse(gateway.ready.is_set())
        gateway.on_subscribe(client, None, 42, [SimpleNamespace(is_failure=False)], None)
        self.assertTrue(gateway.ready.is_set())

    def test_subscription_ack_must_match(self):
        gateway, client = Gateway1(), FakeClient()
        gateway.on_connect(client, None, None, 0, None)
        gateway.on_subscribe(client, None, 11, [SimpleNamespace(is_failure=False)], None)
        self.assertFalse(gateway.ready.is_set())

    def test_subscription_failure_does_not_set_ready(self):
        gateway, client = Gateway1(), FakeClient()
        gateway.on_connect(client, None, None, 0, None)
        with self.assertLogs("src.g1", level="ERROR"):
            gateway.on_subscribe(client, None, 42, [SimpleNamespace(is_failure=True)], None)
        self.assertFalse(gateway.ready.is_set())

    def test_failed_connect_does_not_subscribe(self):
        gateway, client = Gateway1(), FakeClient()
        with self.assertLogs("src.g1", level="ERROR"):
            gateway.on_connect(client, None, None, 5, None)
        self.assertEqual(client.subscriptions, [])
        self.assertFalse(gateway.ready.is_set())

    def test_disconnect_clears_health_and_reconnect_subscribes(self):
        gateway, client = Gateway1(), FakeClient()
        gateway.on_connect(client, None, None, 0, None)
        gateway.on_subscribe(client, None, 42, [SimpleNamespace(is_failure=False)], None)
        gateway.on_disconnect(client, None, None, 0, None)
        self.assertFalse(gateway.ready.is_set())
        gateway.on_connect(client, None, None, 0, None)
        self.assertEqual(len(client.subscriptions), 2)

    def test_subscribe_request_failure_not_ready(self):
        gateway, client = Gateway1(), FakeClient()
        client.subscribe_rc = 4
        with self.assertLogs("src.g1", level="ERROR"):
            gateway.on_connect(client, None, None, 0, None)
        self.assertFalse(gateway.ready.is_set())


class HealthTests(unittest.TestCase):
    def test_container_local_health_status(self):
        gateway = Gateway1()
        httpd = create_health_server(gateway, port=0)
        self.assertEqual(httpd.server_address[0], "127.0.0.1")
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                opener.open(url + "/healthz", timeout=2)
            self.assertEqual(ctx.exception.code, 503)
            gateway.ready.set()
            with opener.open(url + "/healthz", timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), {"status": "ok", "service_version": "0.1.0"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                opener.open(url + "/private", timeout=2)
            self.assertEqual(ctx.exception.code, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


class ConfigTests(unittest.TestCase):
    def test_mosquitto_configuration_matches_v1(self):
        lines = [line.strip() for line in (EDGE_ROOT / "config/mosquitto.conf").read_text().splitlines() if line.strip()]
        self.assertEqual(lines, [
            "listener 1883 0.0.0.0", "allow_anonymous false",
            "password_file /mosquitto/config/passwd", "acl_file /mosquitto/config/acl",
            "persistence true", "persistence_location /mosquitto/data/", "log_dest stdout",
        ])

    def test_mosquitto_acl_has_four_exact_principals(self):
        acl = (EDGE_ROOT / "config/acl").read_text()
        blocks = [block.strip().splitlines() for block in acl.split("\n\n") if block.strip()]
        self.assertEqual(blocks, [
            ["user node", "topic write raw/+"],
            ["user g1", "topic read raw/+", "topic write transit/g1/+"],
            ["user g2", "topic read transit/g1/+", "topic write accepted/+"],
            ["user core", "topic read accepted/+"],
        ])

    def test_compose_only_broker_publishes_external_port(self):
        compose = (EDGE_ROOT / "compose.yaml").read_text()
        self.assertIn('"10.60.10.14:1883:1883"', compose)
        self.assertEqual(compose.count("    ports:"), 1)
        self.assertIn("./config/passwd:/mosquitto/config/passwd:ro", compose)
        self.assertIn("./secrets/g1_mqtt_password:/run/eacld/mqtt_password:ro", compose)
        self.assertNotIn("node_keys", compose)
        self.assertNotIn("5432:", compose)

    def test_git_excludes_broker_password_and_g1_secret(self):
        ignore = (REPO_ROOT / ".gitignore").read_text()
        self.assertIn("edge/config/passwd", ignore)
        self.assertIn("edge/secrets/**", ignore)
        docker = (REPO_ROOT / ".dockerignore").read_text()
        self.assertIn("edge/config/passwd", docker)
        self.assertIn("**/secrets/", docker)


if __name__ == "__main__":
    unittest.main()
