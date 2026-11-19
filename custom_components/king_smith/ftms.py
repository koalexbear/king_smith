"""Adapt walkingpad-controller for KingSmith Z1 supplement initialization."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

from bleak.exc import BleakError
from walkingpad_controller import WalkingPadController
from walkingpad_controller.ftms import FTMSController
from walkingpad_controller.models import TreadmillStatus

from .device_profile import Capability, DeviceProfile, resolve_profile
from .wilink import KingsmithWiLinkController

_LOGGER = logging.getLogger(__name__)
SUPPLEMENT_NOTIFY_UUID = "24e2521c-f63b-48ed-85be-c5330b00fdf7"
SUPPLEMENT_WRITE_UUID = "24e2521c-f63b-48ed-85be-c5330d00fdf7"
PROPERTY_COMMAND = 0x72
PROPERTY_MODE = 10
MODE_SLEEP = 2 << 5


@dataclass
class KingsmithStatus(TreadmillStatus):
    """Track which optional measurements the device has actually reported."""

    has_speed: bool = False
    has_distance: bool = False
    has_duration: bool = False
    has_steps: bool = False


class KingsmithFTMSController(FTMSController):
    """Add Z1 unlocking and validate packets before library parsing."""

    def __init__(self, ble_name: str, profile: DeviceProfile | None = None) -> None:
        """Keep the advertised name used to derive the unlock frame."""
        super().__init__()
        self._status = KingsmithStatus()
        self._ble_name = ble_name
        self._profile = profile or resolve_profile(None, ble_name)
        self._unlock_event = asyncio.Event()
        self._property_event = asyncio.Event()
        self._property_response = b""

    def _on_supplement(self, _sender, data: bytearray) -> None:
        """Accept a complete unlock acknowledgement with a valid checksum."""
        if len(data) < 4:
            return
        length = data[2] + 4
        if len(data) < length or sum(data[: length - 1]) & 0xFF != data[length - 1]:
            return
        if data[:2] == b"\x71\x80":
            self._unlock_event.set()
        elif data[:2] == b"\x72\x81":
            self._property_response = bytes(data[3 : 3 + data[2]])
            self._property_event.set()

    async def sleep(self) -> bool:
        """Put a stopped Z1 into its vendor-defined low-power mode."""
        if not self._profile.supports(Capability.SLEEP):
            return False
        if self._client is None or not self._client.is_connected:
            raise BleakError("FTMS client is unavailable")
        payload = bytes([PROPERTY_COMMAND, 0x01, 0x03, PROPERTY_MODE, MODE_SLEEP, 0])
        payload += bytes([sum(payload) & 0xFF])
        self._property_response = b""
        self._property_event.clear()
        await self._client.write_gatt_char(
            SUPPLEMENT_WRITE_UUID, payload, response=False
        )
        async with asyncio.timeout(3):
            await self._property_event.wait()
        return (
            len(self._property_response) >= 2
            and self._property_response[0] == PROPERTY_MODE
            and self._property_response[1] == 0
        )

    async def _subscribe_notifications(self) -> None:
        """Unlock the Z1 supplement channel before subscribing to FTMS."""
        if self._client is None:
            raise BleakError("FTMS client is unavailable")
        if self._profile.supplement_unlock:
            services = self._client.services
            if services.get_characteristic(SUPPLEMENT_WRITE_UUID) is not None:
                await self._client.start_notify(
                    SUPPLEMENT_NOTIFY_UUID, self._on_supplement
                )
                self._unlock_event.clear()
                suffix = self._ble_name[-4:].encode("ascii")
                code = (int.from_bytes(suffix, "little") + 1) & 0xFFFFFFFF
                payload = b"\x71\x00\x05\x01" + code.to_bytes(4, "little")
                payload += bytes([sum(payload) & 0xFF])
                await self._client.write_gatt_char(
                    SUPPLEMENT_WRITE_UUID, payload, response=False
                )
                async with asyncio.timeout(5):
                    await self._unlock_event.wait()
                await asyncio.sleep(0.4)
        await super()._subscribe_notifications()

    def _on_treadmill_data(self, sender, data: bytearray) -> None:
        """Handle optional speed and reject truncated FTMS notifications."""
        if len(data) < 2:
            return
        flags = int.from_bytes(data[:2], "little")
        # Field lengths in FTMS flag order. KingSmith steps use two bytes,
        # with an optional trailing padding byte on some firmware versions.
        field_sizes = (2, 3, 4, 4, 1, 1, 5, 1, 1, 2, 2, 4, 2)
        expected = 2 + (0 if flags & 1 else 2)
        expected += sum(
            size for bit, size in enumerate(field_sizes, 1) if flags & (1 << bit)
        )
        if len(data) < expected:
            _LOGGER.debug("Ignoring incomplete treadmill packet: %s", data.hex())
            return
        self._status.has_speed |= not bool(flags & 1)
        self._status.has_distance |= bool(flags & (1 << 2))
        self._status.has_duration |= bool(flags & (1 << 10))
        self._status.has_steps |= bool(flags & (1 << 13))
        normalized = bytearray(data)
        if flags & 1:
            normalized[:2] = (flags & ~1).to_bytes(2, "little")
            speed = round(self.status.speed * 100)
            normalized[2:2] = speed.to_bytes(2, "little")
        if flags & (1 << 13) and len(data) == expected:
            normalized.append(0)
        super()._on_treadmill_data(sender, normalized)


class KingsmithController(WalkingPadController):
    """Select the Z1-capable FTMS backend while retaining WiLink support."""

    def __init__(
        self, ble_device, name: str | None = None, profile: DeviceProfile | None = None
    ) -> None:
        """Initialize with the profile selected during Bluetooth discovery."""
        super().__init__(ble_device, name)
        self._profile = profile or resolve_profile(None, name or ble_device.name or "")

    async def _connect_ftms(self) -> None:
        """Use the library backend with supplement initialization."""
        self._ftms = KingsmithFTMSController(
            self._ble_device.name or self._name, self._profile
        )
        self._ftms.register_status_callback(self._on_status_update)
        self._ftms.register_disconnect_callback(self._on_disconnect)
        try:
            await self._ftms.connect(self._ble_device)
        except BaseException:
            await self._ftms.disconnect()
            raise

    async def _connect_wilink(self) -> None:
        """Use paced WiLink writes and proxy-aware connection retries."""
        self._wilink = KingsmithWiLinkController()
        self._wilink.register_status_callback(self._on_status_update)
        self._wilink.register_disconnect_callback(self._on_disconnect)
        try:
            await self._wilink.connect(self._ble_device)
        except BaseException:
            await self._wilink.disconnect()
            raise

    async def sleep(self) -> bool:
        """Enter Z1 sleep mode through the supplement property channel."""
        if self._ftms is None:
            return False
        return await self._ftms.sleep()
