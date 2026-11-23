"""Wire-format and authentication contract tests."""

import dataclasses
import unittest

from eacld_contracts.frame import (
    CHALLENGE_RESPONSE_TYPE,
    FRAME_SIZE,
    HEADER_SIZE,
    MAC_SIZE,
    TELEMETRY_TYPE,
    Frame,
    FrameHeader,
    sign_frame,
    verify_frame,
)


class FrameContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = bytes(range(1, 33))
        self.header = FrameHeader(
            version=1,
            type=TELEMETRY_TYPE,
            node_id=513,
            zone_id=7,
            key_version=2,
            boot_id=bytes(range(16)),
            sequence_no=2**32 + 17,
            uptime_ms=2**40 + 29,
            temperature_mC=-12345,
            humidity_mPct=56789,
            nonce=bytes(range(16, 32)),
        )

    def test_wire_sizes_and_round_trip(self) -> None:
        self.assertEqual((HEADER_SIZE, MAC_SIZE, FRAME_SIZE), (62, 32, 94))
        frame = sign_frame(self.header, self.key)
        self.assertEqual(len(frame.to_bytes()), 94)
        self.assertEqual(len(frame.to_hex()), 188)
        self.assertEqual(Frame.from_bytes(frame.to_bytes()), frame)
        self.assertEqual(Frame.from_hex(frame.to_hex()), frame)
        self.assertTrue(verify_frame(frame, self.key))

    def test_header_wire_order_and_endianness(self) -> None:
        wire = self.header.to_bytes()
        self.assertEqual(wire[:6], bytes([1, 1, 2, 1, 7, 2]))
        self.assertEqual(FrameHeader.from_bytes(wire), self.header)

    def test_challenge_response_type(self) -> None:
        response = sign_frame(dataclasses.replace(self.header, type=CHALLENGE_RESPONSE_TYPE), self.key)
        self.assertEqual(response.header.type, 2)
        self.assertTrue(verify_frame(response, self.key))

    def test_tampering_and_wrong_key(self) -> None:
        frame = sign_frame(self.header, self.key)
        wire = bytearray(frame.to_bytes())
        wire[42] ^= 1
        self.assertFalse(verify_frame(Frame.from_bytes(bytes(wire)), self.key))
        self.assertFalse(verify_frame(frame, b"different-key"))

    def test_wrong_sizes_and_invalid_hex(self) -> None:
        for count in (0, 61, 93, 95):
            with self.subTest(count=count), self.assertRaises(ValueError):
                Frame.from_bytes(bytes(count))
        with self.assertRaises(ValueError):
            Frame.from_hex("not hexadecimal")
        with self.assertRaises(ValueError):
            FrameHeader.from_bytes(bytes(63))

    def test_field_boundaries(self) -> None:
        for field, value in (
            ("node_id", 65536),
            ("zone_id", -1),
            ("sequence_no", 2**64),
            ("temperature_mC", 2**31),
            ("humidity_mPct", -1),
            ("nonce", b"short"),
            ("boot_id", b"short"),
            ("type", 3),
            ("version", True),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                dataclasses.replace(self.header, **{field: value})

    def test_missing_key(self) -> None:
        with self.assertRaises(ValueError):
            sign_frame(self.header, b"")
        with self.assertRaises(ValueError):
            verify_frame(sign_frame(self.header, self.key), b"")


if __name__ == "__main__":
    unittest.main()
