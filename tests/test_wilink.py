"""Exercise WiLink command timing and connection setup without real hardware."""

import asyncio
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from ph4_walkingpad.pad import Controller
from walkingpad_controller.const import ProtocolType
from walkingpad_controller.models import TreadmillStatus

from custom_components.king_smith import wilink
from custom_components.king_smith.ftms import KingsmithController


async def test_minimum_gap_and_command_bytes():
    """Preserve WiLink frames and enforce spacing across command sequences."""
    clock = [100.0]
    writes = []
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    async def write(characteristic, frame):
        writes.append((clock[0], bytes(frame)))
        clock[0] += 0.02

    controller = wilink.PacedPadController()
    controller.client = MagicMock()
    controller.client.write_gatt_char = AsyncMock(side_effect=write)
    controller.char_fe02 = "write-characteristic"
    with (
        patch.object(wilink, "time", SimpleNamespace(monotonic=lambda: clock[0])),
        patch.object(wilink, "asyncio", SimpleNamespace(sleep=sleep)),
    ):
        await controller.switch_mode(1)
        await controller.start_belt()
        await controller.ask_stats()
        clock[0] += 1
        await controller.change_speed(10)
        await controller.stop_belt()
    assert [frame for _, frame in writes] == [
        bytes.fromhex("f7a20201a5fd"),
        bytes.fromhex("f7a20401a7fd"),
        bytes.fromhex("f7a20000a2fd"),
        bytes.fromhex("f7a2010aadfd"),
        bytes.fromhex("f7a20100a3fd"),
    ]
    assert sleeps == pytest.approx([0.69, 0.69, 0.69])
    assert all(b[0] - a[0] >= 0.709 for a, b in pairwise(writes))


async def test_upstream_reproduces_short_gap():
    """Demonstrate the pinned dependency sends mode/start only 40 ms apart."""
    clock = [100.0]
    writes = []

    async def sleep(delay):
        clock[0] += delay

    async def write(characteristic, frame):
        writes.append(clock[0])
        clock[0] += 0.02

    controller = Controller()
    controller.client = MagicMock()
    controller.client.write_gatt_char = AsyncMock(side_effect=write)
    with (
        patch("ph4_walkingpad.pad.time", SimpleNamespace(time=lambda: clock[0])),
        patch("ph4_walkingpad.pad.asyncio", SimpleNamespace(sleep=sleep)),
    ):
        await controller.switch_mode(1)
        await controller.start_belt()
    assert writes[1] - writes[0] == pytest.approx(0.04)


async def test_concurrent_commands_are_serialized():
    """Do not overlap BLE writes or bypass spacing with concurrent callers."""
    writes = []
    loop = asyncio.get_running_loop()
    controller = wilink.PacedPadController()
    controller.minimal_cmd_space = 0.03

    async def write(characteristic, frame):
        writes.append(loop.time())
        await asyncio.sleep(0.01)

    controller.client = MagicMock()
    controller.client.write_gatt_char = AsyncMock(side_effect=write)
    await asyncio.gather(
        controller.ask_stats(), controller.ask_stats(), controller.ask_stats()
    )
    assert len(writes) == 3
    assert all(b - a >= 0.039 for a, b in pairwise(writes))


async def test_failed_write_is_not_retried():
    """Propagate write errors without automatically replaying belt commands."""
    controller = wilink.PacedPadController()
    controller.client = MagicMock()
    controller.client.write_gatt_char = AsyncMock(
        side_effect=BleakError("disconnected")
    )
    with pytest.raises(BleakError, match="disconnected"):
        await controller.start_belt()
    assert controller.client.write_gatt_char.await_count == 1


async def test_connector_uses_original_ble_device_without_writing():
    """Retain Home Assistant proxy metadata and avoid commands on connection."""
    device = BLEDevice("AA:BB:CC:DD:EE:FF", "KS-ST-A1P", {})
    client = MagicMock()
    client.is_connected = True
    client.write_gatt_char = AsyncMock()
    connector = AsyncMock(return_value=client)
    controller = wilink.PacedPadController()
    with patch.object(wilink, "establish_connection", connector):
        assert await controller.connect(device)
    connector.assert_awaited_once_with(
        wilink.BleakClient, device, "KS-ST-A1P"
    )
    client.write_gatt_char.assert_not_called()


async def test_wilink_backend_keeps_status_and_disconnect_callbacks():
    """Route WiLink setup through the adapter and retain both callback paths."""
    device = BLEDevice("AA:BB:CC:DD:EE:FF", "KS-ST-A1P", {})
    controller = KingsmithController(device)
    controller._protocol = ProtocolType.WILINK
    status_callback = MagicMock()
    disconnect_callback = MagicMock()
    controller.register_status_callback(status_callback)
    controller.register_disconnect_callback(disconnect_callback)
    with patch.object(
        wilink.KingsmithWiLinkController, "connect", AsyncMock()
    ) as connect:
        await controller.connect()
    connect.assert_awaited_once_with(device)
    backend = controller._wilink
    assert isinstance(backend._controller, wilink.PacedPadController)
    backend._controller.handler_cur_status(
        None,
        SimpleNamespace(
            belt_state=1, speed=10, manual_mode=1, dist=2, time=3, steps=4
        ),
    )
    status_callback.assert_called_once()
    status = status_callback.call_args.args[0]
    assert isinstance(status, TreadmillStatus)
    assert status.speed == 1.0
    assert status.distance == 20
    backend._on_ble_disconnect(None)
    disconnect_callback.assert_called_once()


@pytest.mark.parametrize("error", [BleakError("setup failed"), asyncio.CancelledError()])
async def test_wilink_setup_failure_releases_connection(error):
    """Close partially initialized WiLink clients on errors and cancellation."""
    controller = KingsmithController(
        BLEDevice("AA:BB:CC:DD:EE:FF", "KS-ST-A1P", {})
    )
    controller._protocol = ProtocolType.WILINK
    with (
        patch.object(
            wilink.KingsmithWiLinkController, "connect", AsyncMock(side_effect=error)
        ),
        patch.object(
            wilink.KingsmithWiLinkController, "disconnect", AsyncMock()
        ) as disconnect,
        pytest.raises(type(error)),
    ):
        await controller.connect()
    disconnect.assert_awaited_once()
