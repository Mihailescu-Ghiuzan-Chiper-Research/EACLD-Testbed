"""Fixed-width EACLD telemetry and challenge-response frame.

Architecture V1, Table 8: 62-byte big-endian header and 32-byte
HMAC-SHA256 authenticator. No implicit wire-protocol version is assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import struct

TELEMETRY_TYPE = 1
CHALLENGE_RESPONSE_TYPE = 2
HEADER_FORMAT = "!BBHBB16sQQiI16s"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
MAC_SIZE = hashlib.sha256().digest_size
FRAME_SIZE = HEADER_SIZE + MAC_SIZE

if (HEADER_SIZE, MAC_SIZE, FRAME_SIZE) != (62, 32, 94):
    raise RuntimeError("EACLD wire frame size does not match architecture V1")


def _check_unsigned(name: str, value: int, bits: int) -> None:
    if type(value) is not int or not (0 <= value < 1 << bits):
        raise ValueError(f"{name} must be an unsigned {bits}-bit integer")


def _check_signed(name: str, value: int, bits: int) -> None:
    if type(value) is not int or not (-(1 << (bits - 1)) <= value < 1 << (bits - 1)):
        raise ValueError(f"{name} must be a signed {bits}-bit integer")


def _check_fixed_bytes(name: str, value: bytes, length: int) -> None:
    if not isinstance(value, bytes) or len(value) != length:
        raise ValueError(f"{name} must contain exactly {length} bytes")


def _check_key(key: bytes) -> None:
    if not isinstance(key, bytes) or not key:
        raise ValueError("HMAC key must be nonempty bytes")


@dataclass(frozen=True, slots=True)
class FrameHeader:
    """All eleven authenticated header fields in their specified wire order."""

    version: int
    type: int
    node_id: int
    zone_id: int
    key_version: int
    boot_id: bytes
    sequence_no: int
    uptime_ms: int
    temperature_mC: int
    humidity_mPct: int
    nonce: bytes

    def __post_init__(self) -> None:
        _check_unsigned("version", self.version, 8)
        _check_unsigned("type", self.type, 8)
        if self.type not in (TELEMETRY_TYPE, CHALLENGE_RESPONSE_TYPE):
            raise ValueError("type must be 1 (telemetry) or 2 (challenge response)")
        _check_unsigned("node_id", self.node_id, 16)
        _check_unsigned("zone_id", self.zone_id, 8)
        _check_unsigned("key_version", self.key_version, 8)
        _check_fixed_bytes("boot_id", self.boot_id, 16)
        _check_unsigned("sequence_no", self.sequence_no, 64)
        _check_unsigned("uptime_ms", self.uptime_ms, 64)
        _check_signed("temperature_mC", self.temperature_mC, 32)
        _check_unsigned("humidity_mPct", self.humidity_mPct, 32)
        _check_fixed_bytes("nonce", self.nonce, 16)

    def to_bytes(self) -> bytes:
        return struct.pack(
            HEADER_FORMAT,
            self.version,
            self.type,
            self.node_id,
            self.zone_id,
            self.key_version,
            self.boot_id,
            self.sequence_no,
            self.uptime_ms,
            self.temperature_mC,
            self.humidity_mPct,
            self.nonce,
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> FrameHeader:
        if not isinstance(data, bytes) or len(data) != HEADER_SIZE:
            raise ValueError(f"header must contain exactly {HEADER_SIZE} bytes")
        return cls(*struct.unpack(HEADER_FORMAT, data))


@dataclass(frozen=True, slots=True)
class Frame:
    """A parsed frame. Parsing alone does not establish authenticity."""

    header: FrameHeader
    mac: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.header, FrameHeader):
            raise TypeError("header must be a FrameHeader")
        _check_fixed_bytes("mac", self.mac, MAC_SIZE)

    def to_bytes(self) -> bytes:
        return self.header.to_bytes() + self.mac

    def to_hex(self) -> str:
        return self.to_bytes().hex()

    @classmethod
    def from_bytes(cls, data: bytes) -> Frame:
        if not isinstance(data, bytes) or len(data) != FRAME_SIZE:
            raise ValueError(f"frame must contain exactly {FRAME_SIZE} bytes")
        return cls(FrameHeader.from_bytes(data[:HEADER_SIZE]), data[HEADER_SIZE:])

    @classmethod
    def from_hex(cls, value: str) -> Frame:
        if not isinstance(value, str):
            raise TypeError("frame_hex must be a hexadecimal string")
        try:
            data = bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError("frame_hex is not valid hexadecimal data") from exc
        return cls.from_bytes(data)


def sign_frame(header: FrameHeader, key: bytes) -> Frame:
    """Produce an HMAC-SHA256 authenticator over the 62-byte header."""
    if not isinstance(header, FrameHeader):
        raise TypeError("header must be a FrameHeader")
    _check_key(key)
    mac = hmac.new(key, header.to_bytes(), hashlib.sha256).digest()
    return Frame(header=header, mac=mac)


def verify_frame(frame: Frame, key: bytes) -> bool:
    """Check authenticity; freshness/replay validation is a separate operation."""
    if not isinstance(frame, Frame):
        raise TypeError("frame must be a Frame")
    _check_key(key)
    expected = hmac.new(key, frame.header.to_bytes(), hashlib.sha256).digest()
    return hmac.compare_digest(frame.mac, expected)
