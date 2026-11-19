"""WiLink transport fixes for ph4-walkingpad 1.0.2."""

from __future__ import annotations

import asyncio
import logging
import time

from bleak import BleakClient
from bleak_retry_connector import establish_connection
from ph4_walkingpad.pad import Controller
from walkingpad_controller.wilink import WiLinkController

_LOGGER = logging.getLogger(__name__)


class PacedPadController(Controller):
    """Space every write and use Home Assistant's proxy-aware connector."""

    def __init__(self) -> None:
        """Initialize instance-local write serialization and timing."""
        super().__init__()
        self._write_lock = asyncio.Lock()
        self._last_write_completed: float | None = None

    async def connect(self, address=None):
        """Connect using the BLEDevice supplied by Home Assistant."""
        device = address or self.address
        if device is None:
            raise ValueError("No Bluetooth device supplied")
        self.client = await establish_connection(
            BleakClient, device, device.name or device.address
        )
        return self.client.is_connected

    async def send_cmd(self, cmd):
        """Replace the upstream elapsed-time/remaining-time calculation."""
        self.fix_crc(cmd)
        return await self.send_cmd_raw(cmd)

    async def send_cmd_raw(self, cmd):
        """Serialize writes with the library's minimum gap after completion."""
        async with self._write_lock:
            if self._last_write_completed is not None:
                remaining = self.minimal_cmd_space - (
                    time.monotonic() - self._last_write_completed
                )
                if remaining > 0:
                    await asyncio.sleep(remaining)
            _LOGGER.debug("WiLink TX: %s", bytes(cmd).hex())
            try:
                return await super().send_cmd_raw(cmd)
            finally:
                self._last_write_completed = time.monotonic()


class KingsmithWiLinkController(WiLinkController):
    """Keep the existing WiLink protocol with a corrected BLE transport."""

    def __init__(self) -> None:
        """Replace the transport while retaining the library's status handler."""
        super().__init__()
        self._controller = PacedPadController()
        self._controller.log_messages_info = False
        self._controller.handler_cur_status = self._on_status_update
