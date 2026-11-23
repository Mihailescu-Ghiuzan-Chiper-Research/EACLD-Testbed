"""VM-NODES runtime: virtual sensors, authenticated MQTT and active probes.

No private ground truth is fetched or persisted. VM-CONTROL remains the
only owner of the shared process and private intervention state.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import signal
import threading
from typing import Iterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

from eacld_contracts.frame import CHALLENGE_RESPONSE_TYPE, TELEMETRY_TYPE
from eacld_contracts.interfaces import MQTT_BROKER_HOST, MQTT_BROKER_PORT
from eacld_contracts.topics import raw_topic

from .challenge import create_probe_app
from .frame import NodeFrameProducer
from .sensor import PlantClient, SensorBias, VirtualSensor

LOGGER = logging.getLogger(__name__)
SERVICE_VERSION = "0.1.0"
CONFIG_PATH = "/etc/eacld/nodes.json"
KEYS_PATH = "/run/eacld/node_keys.json"
MQTT_PASSWORD_PATH = "/run/eacld/mqtt_password"


@dataclass(frozen=True, slots=True)
class NodeDefinition:
    node_id: int
    zone_id: int
    key_version: int
    key: bytes
    bias: SensorBias


@dataclass(frozen=True, slots=True)
class NodesConfiguration:
    protocol_version: int
    telemetry_interval_ms: int
    nodes: tuple[NodeDefinition, ...]


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def load_config(config_path: str | Path, keys_path: str | Path) -> NodesConfiguration:
    """Load public node metadata and private per-node/version HMAC keys."""
    with Path(config_path).open(encoding="utf-8") as stream:
        config = json.load(stream)
    with Path(keys_path).open(encoding="utf-8") as stream:
        secrets_data = json.load(stream)
    if not isinstance(config, dict) or not isinstance(secrets_data, dict):
        raise ValueError("configuration and secret keyring must be JSON objects")
    version = _bounded_int(config.get("protocol_version"), "protocol_version", 0, 255)
    interval = _bounded_int(config.get("telemetry_interval_ms"),
                            "telemetry_interval_ms", 100, 3_600_000)
    descriptors = config.get("nodes")
    if not isinstance(descriptors, list) or not descriptors:
        raise ValueError("nodes must be a nonempty array")
    definitions = []
    seen: set[int] = set()
    for item in descriptors:
        if not isinstance(item, dict):
            raise ValueError("each node definition must be a JSON object")
        node_id = _bounded_int(item.get("node_id"), "node_id", 0, 65535)
        zone_id = _bounded_int(item.get("zone_id"), "zone_id", 0, 255)
        key_version = _bounded_int(item.get("key_version"), "key_version", 0, 255)
        if node_id in seen:
            raise ValueError("duplicate node_id in node configuration")
        seen.add(node_id)
        key_versions = secrets_data.get(str(node_id), {})
        encoded = key_versions.get(str(key_version)) if isinstance(key_versions, dict) else None
        if not isinstance(encoded, str):
            raise ValueError(f"missing private HMAC key for configured node {node_id}")
        try:
            key = bytes.fromhex(encoded)
        except ValueError as exc:
            raise ValueError(f"invalid private HMAC key encoding for node {node_id}") from exc
        if len(key) < 32:
            raise ValueError(f"HMAC key for node {node_id} must be at least 32 bytes")
        bias = SensorBias(
            temperature_mC=_bounded_int(item.get("bias_temperature_mC", 0),
                "bias_temperature_mC", -(1 << 31), (1 << 31) - 1),
            humidity_mPct=_bounded_int(item.get("bias_humidity_mPct", 0),
                "bias_humidity_mPct", -(1 << 31), (1 << 31) - 1),
        )
        definitions.append(NodeDefinition(node_id, zone_id, key_version, key, bias))
    return NodesConfiguration(version, interval, tuple(definitions))


class NodesService:
    """Holds per-node boot/sequence state shared by telemetry and challenges."""

    def __init__(self, config: NodesConfiguration, plant: PlantClient):
        self.config = config
        self.plant = plant
        self.producers = {
            item.node_id: NodeFrameProducer(
                node_id=item.node_id, zone_id=item.zone_id,
                key_version=item.key_version, key=item.key,
                protocol_version=config.protocol_version,
            ) for item in config.nodes
        }
        self.sensors = {item.node_id: VirtualSensor(item.bias) for item in config.nodes}

    def _emit(self, node_id: int, message_type: int, nonce: bytes | None = None):
        if node_id not in self.producers:
            raise KeyError("unknown node_id")
        observation = self.plant.read()
        measurement = self.sensors[node_id].measure(observation)
        return self.producers[node_id].emit(message_type, measurement, nonce=nonce)

    def challenge(self, node_id: int, nonce: bytes):
        return self._emit(node_id, CHALLENGE_RESPONSE_TYPE, nonce)

    def telemetry_batch(self) -> tuple[tuple[str, bytes], ...]:
        """Read the common plant once and sign every configured node's view."""
        observation = self.plant.read()
        output = []
        for item in self.config.nodes:
            measurement = self.sensors[item.node_id].measure(observation)
            frame = self.producers[item.node_id].emit(TELEMETRY_TYPE, measurement)
            output.append((raw_topic(item.node_id), frame.to_bytes()))
        return tuple(output)


class MqttPublisher:
    """Paho MQTT v2 publisher using the fixed 'node' principal and QoS 1."""

    def __init__(self, password_path: str | Path, *,
                 host: str = MQTT_BROKER_HOST, port: int = MQTT_BROKER_PORT):
        self.password_path = Path(password_path)
        self.host = host
        self.port = port
        self.connected = threading.Event()
        self.client = None

    def start(self) -> None:
        # Import lazily: unit tests of the frame and APIs do not need a broker.
        import paho.mqtt.client as mqtt

        password = self.password_path.read_text(encoding="utf-8").rstrip("\r\n")
        if not password:
            raise ValueError("MQTT password file is empty")
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id="eacld-nodes", protocol=mqtt.MQTTv311)
        client.username_pw_set("node", password)

        def on_connect(_client, _userdata, _flags, reason_code, _properties):
            if reason_code == 0:
                self.connected.set()
            else:
                self.connected.clear()
                LOGGER.warning("MQTT connect rejected: %s", reason_code)

        def on_disconnect(_client, _userdata, _flags, _reason_code, _properties):
            self.connected.clear()

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.reconnect_delay_set(min_delay=1, max_delay=20)
        client.connect_async(self.host, self.port, keepalive=30)
        client.loop_start()
        self.client = client

    def publish(self, topic: str, payload: bytes) -> None:
        if not self.connected.wait(timeout=10):
            raise RuntimeError("MQTT broker connection unavailable")
        if self.client is None:
            raise RuntimeError("MQTT publisher has not started")
        info = self.client.publish(topic, payload, qos=1, retain=False)
        if info.rc != 0:
            raise RuntimeError("MQTT publish failed")
        info.wait_for_publish(timeout=10)
        if not info.is_published():
            raise RuntimeError("MQTT publish was not acknowledged")

    def stop(self) -> None:
        self.connected.clear()
        if self.client is not None:
            self.client.disconnect()
            self.client.loop_stop()


async def publish_loop(service: NodesService, publisher: MqttPublisher) -> None:
    """Publish sampled views without ever transporting private state."""
    interval_s = service.config.telemetry_interval_ms / 1000
    while True:
        try:
            batch = await asyncio.to_thread(service.telemetry_batch)
            for topic, payload in batch:
                await asyncio.to_thread(publisher.publish, topic, payload)
        except (OSError, RuntimeError, ValueError) as exc:
            LOGGER.error("telemetry acquisition or delivery failed: %s", type(exc).__name__)
        await asyncio.sleep(interval_s)


def create_control_app(*, allowed_sources: frozenset[str] =
                       frozenset({"10.60.30.10", "127.0.0.1"})) -> FastAPI:
    """Health-only agent on NET-CONTROL; deny other sources and endpoints."""
    app = FastAPI(title="eacld-nodes control", docs_url=None,
                  redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def restrict_source(request: Request, call_next):
        source = request.client.host if request.client is not None else ""
        if source not in allowed_sources:
            return JSONResponse(status_code=403, content={"detail": "forbidden"})
        return await call_next(request)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok", "service_version": SERVICE_VERSION}

    return app


class SignalManagedServer(uvicorn.Server):
    """Leave signal registration to the joint multi-listener supervisor."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def serve(service: NodesService, publisher: MqttPublisher) -> None:
    """Run two isolated HTTP listeners and one telemetry task in one process."""
    probe = SignalManagedServer(uvicorn.Config(
        create_probe_app(service), host="0.0.0.0", port=8202,
        log_level="info", access_log=False))
    control = SignalManagedServer(uvicorn.Config(
        create_control_app(), host="0.0.0.0", port=8300,
        log_level="info", access_log=False))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    publisher.start()
    tasks = [asyncio.create_task(probe.serve()),
             asyncio.create_task(control.serve()),
             asyncio.create_task(publish_loop(service, publisher))]
    stopper = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(tasks + [stopper], return_when=asyncio.FIRST_COMPLETED)
        if stopper not in done:
            for task in done:
                if task.exception() is not None:
                    raise task.exception()
            raise RuntimeError("an EACLD nodes service task exited unexpectedly")
    finally:
        probe.should_exit = True
        control.should_exit = True
        tasks[-1].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stopper.cancel()
        publisher.stop()


def main() -> None:
    config = load_config(os.environ.get("EACLD_NODES_CONFIG", CONFIG_PATH),
                         os.environ.get("EACLD_NODE_KEYS", KEYS_PATH))
    service = NodesService(config, PlantClient())
    publisher = MqttPublisher(os.environ.get("EACLD_MQTT_PASSWORD_FILE", MQTT_PASSWORD_PATH))
    asyncio.run(serve(service, publisher))


if __name__ == "__main__":
    main()
