"""Shared EACLD protocol contracts (architecture V1)."""

from .frame import (
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

__all__ = [
    "CHALLENGE_RESPONSE_TYPE",
    "FRAME_SIZE",
    "HEADER_SIZE",
    "MAC_SIZE",
    "TELEMETRY_TYPE",
    "Frame",
    "FrameHeader",
    "sign_frame",
    "verify_frame",
]
