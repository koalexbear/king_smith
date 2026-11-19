"""Verify command serialization, errors, and connection recovery."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from homeassistant.exceptions import HomeAssistantError
from walkingpad_controller.const import OperatingMode, ProtocolType
from walkingpad_controller.models import TreadmillStatus

from custom_components.king_smith.walking_pad import WalkingPadApi
from custom_components.king_smith.device_profile import LEGACY_WILINK_PROFILE


@pytest.fixture
def api_and_controller():
    """Connect the API to a controller with realistic units and results."""
    controller = MagicMock()
    controller.connected = True
    controller.protocol = ProtocolType.FTMS
    controller.min_speed = 1.0
    controller.max_speed = 6.0
    controller.speed_increment = 0.1
    for method in ["start", "stop", "sleep", "set_speed", "switch_mode"]:
        setattr(controller, method, AsyncMock(return_value=True))
    controller.update_state = AsyncMock()
    controller.disconnect = AsyncMock()
    api = WalkingPadApi("Z1", "AA:BB:CC:DD:EE:FF", "KS-HD-Z1D", MagicMock())
    api._ctrl = controller
    return api, controller


async def test_commands_are_serialized(api_and_controller):
    """Keep a stop behind the pending speed command in the same queue."""
    api, controller = api_and_controller
    calls = []

    async def set_speed(value):
        calls.append("speed begin")
        await asyncio.sleep(0.01)
        calls.append("speed end")
        return True

    async def stop():
        calls.append("stop")
        return True

    controller.set_speed.side_effect = set_speed
    controller.stop.side_effect = stop
    await asyncio.gather(api.change_speed(3.3), api.stop_belt())
    assert calls == ["speed begin", "speed end", "stop"]
    controller.set_speed.assert_awaited_once_with(3.3)


async def test_error_releases_queue(api_and_controller):
    """Keep commands and device reports working after an operation fails."""
    api, controller = api_and_controller
    controller.start.side_effect = BleakError("connection problem")
    with pytest.raises(HomeAssistantError):
        await api.start_belt()
    assert not api._lock.locked()
    await api.stop_belt()
    api._on_status_update(TreadmillStatus(speed=2.5))
    assert api.speed == 2.5


async def test_rejected_command_is_reported(api_and_controller):
    """Surface a negative command result instead of claiming success."""
    api, controller = api_and_controller
    controller.start.return_value = False
    with pytest.raises(HomeAssistantError, match="did not confirm"):
        await api.start_belt()
    assert api.moving is None


@pytest.mark.parametrize("speed", [-1, float("nan"), float("inf"), 0.5, 6.1, 2.35])
async def test_invalid_speeds(api_and_controller, speed):
    """Reject invalid speed values before sending motor commands."""
    api, controller = api_and_controller
    with pytest.raises(HomeAssistantError):
        await api.change_speed(speed)
    controller.set_speed.assert_not_awaited()
    controller.start.assert_not_awaited()


async def test_zero_speed_stops(api_and_controller):
    """Send an explicit stop instead of allowing library minimum clamping."""
    api, controller = api_and_controller
    await api.change_speed(0)
    controller.stop.assert_awaited_once()
    controller.set_speed.assert_not_awaited()


async def test_speed_buttons_use_configured_adjustment(api_and_controller):
    """Apply the local step size to the device-reported speed."""
    api, controller = api_and_controller
    api.speed_adjustment_step = 0.2
    api._on_status_update(TreadmillStatus(speed=3.3))
    await api.adjust_speed(1)
    controller.set_speed.assert_awaited_once_with(3.5)


async def test_speed_buttons_stay_within_profile_limits(api_and_controller):
    """Clamp button changes to the model's safe speed range."""
    api, controller = api_and_controller
    api.speed_adjustment_step = 1.0
    api._on_status_update(TreadmillStatus(speed=6.0))
    await api.adjust_speed(1)
    controller.set_speed.assert_awaited_once_with(6.0)


@pytest.mark.parametrize("direction,expected", [(1, 3.5), (-1, 2.5)])
async def test_speed_buttons_follow_pending_slider_target(
    api_and_controller, direction, expected
):
    """Do not calculate either button from telemetry predating a slider command."""
    api, controller = api_and_controller
    api._on_status_update(TreadmillStatus(speed=1.0))
    await api.change_speed(3.0)
    api._on_status_update(TreadmillStatus(speed=1.2))
    await api.adjust_speed(direction)
    assert [call.args[0] for call in controller.set_speed.await_args_list] == [
        3.0, expected
    ]
    assert api.speed == 1.2


async def test_queued_speed_buttons_accumulate(api_and_controller):
    """Calculate each rapid press after the preceding command has completed."""
    api, controller = api_and_controller
    api._on_status_update(TreadmillStatus(speed=2.0))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def set_speed(value):
        if value == 3.0:
            entered.set()
            await release.wait()
        return True

    controller.set_speed.side_effect = set_speed
    slider = asyncio.create_task(api.change_speed(3.0))
    await entered.wait()
    faster1 = asyncio.create_task(api.adjust_speed(1))
    faster2 = asyncio.create_task(api.adjust_speed(1))
    slower = asyncio.create_task(api.adjust_speed(-1))
    release.set()
    await asyncio.gather(slider, faster1, faster2, slower)
    assert [call.args[0] for call in controller.set_speed.await_args_list] == [
        3.0, 3.5, 4.0, 3.5
    ]


async def test_confirmed_target_allows_later_remote_speed_changes(api_and_controller):
    """Return to measured speed once the device has reached our target."""
    api, controller = api_and_controller
    api._on_status_update(TreadmillStatus(speed=1.0))
    await api.change_speed(3.0)
    api._on_status_update(TreadmillStatus(speed=3.0))
    api._on_status_update(TreadmillStatus(speed=2.0))
    await api.adjust_speed(1)
    assert controller.set_speed.await_args_list[-1].args == (2.5,)


async def test_target_confirmation_during_write_is_retained(api_and_controller):
    """Do not reinstate a pending target after its report arrived during a write."""
    api, controller = api_and_controller
    api._on_status_update(TreadmillStatus(speed=1.0))

    async def set_speed(value):
        api._on_status_update(TreadmillStatus(speed=value))
        return True

    controller.set_speed.side_effect = set_speed
    await api.change_speed(3.0)
    api._on_status_update(TreadmillStatus(speed=2.0))
    await api.adjust_speed(1)
    assert controller.set_speed.await_args_list[-1].args == (2.5,)


@pytest.mark.parametrize("failure", [False, BleakError("write failed"), asyncio.CancelledError()])
async def test_failed_speed_command_does_not_seed_next_adjustment(
    api_and_controller, failure
):
    """Drop an unconfirmed command target after rejection, failure or cancellation."""
    api, controller = api_and_controller
    api._on_status_update(TreadmillStatus(speed=2.0))
    if failure is False:
        controller.set_speed.return_value = False
    else:
        controller.set_speed.side_effect = failure
    error = asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else HomeAssistantError
    with pytest.raises(error):
        await api.change_speed(5.0)
    controller.set_speed.side_effect = None
    controller.set_speed.return_value = True
    await api.adjust_speed(1)
    assert controller.set_speed.await_args_list[-1].args == (2.5,)


@pytest.mark.parametrize("reset", ["start", "stop", "sleep", "disconnect", "remote_stop"])
async def test_pending_speed_is_reset_between_sessions(api_and_controller, reset):
    """Do not carry a speed target across stopping, sleeping or disconnecting."""
    api, controller = api_and_controller
    api._on_status_update(TreadmillStatus(speed=2.0))
    await api.change_speed(5.0)
    if reset == "disconnect":
        api._on_disconnect()
    elif reset == "remote_stop":
        api._on_status_update(TreadmillStatus(speed=0.0))
    else:
        await api._command(reset)
    api._on_status_update(TreadmillStatus(speed=1.0))
    await api.adjust_speed(1)
    assert controller.set_speed.await_args_list[-1].args == (1.5,)


async def test_unknown_speed_does_not_default_to_minimum(api_and_controller):
    """Require a measured starting point rather than guessing a low speed."""
    api, controller = api_and_controller
    with pytest.raises(HomeAssistantError, match="speed report"):
        await api.adjust_speed(1)
    controller.set_speed.assert_not_awaited()


async def test_sleep_stops_a_moving_ftms_pad_first(api_and_controller):
    """Match the KS Fit exit sequence before entering vendor sleep mode."""
    api, controller = api_and_controller
    api._on_status_update(TreadmillStatus(speed=3.3))
    calls = []
    controller.stop.side_effect = lambda: calls.append("stop") or True
    controller.sleep.side_effect = lambda: calls.append("sleep") or True
    with patch("custom_components.king_smith.walking_pad.asyncio.sleep", AsyncMock()):
        await api.sleep()
    assert calls == ["stop", "sleep"]


async def test_sleep_uses_standby_on_legacy_pad(api_and_controller):
    """Keep the equivalent standby action for supported WiLink devices."""
    api, controller = api_and_controller
    api.profile = LEGACY_WILINK_PROFILE
    controller.protocol = ProtocolType.WILINK
    await api.sleep()
    controller.switch_mode.assert_awaited_once_with(OperatingMode.STANDBY)
    controller.update_state.assert_awaited_once()
    controller.sleep.assert_not_awaited()


def test_mutable_library_status_is_snapshotted(api_and_controller):
    """Calculate cadence against a distinct snapshot of the previous report."""
    api, _ = api_and_controller
    report = TreadmillStatus(speed=3.3, distance=1000, duration=60, steps=100)
    api._on_status_update(report)
    report.duration = 70
    report.steps = 120
    assert api.time == 60
    api._on_status_update(report)
    assert api.step_cadence == 120
    assert api.distance == 1.0
    api._on_status_update(replace(report, duration=1, steps=1))
    assert api.step_cadence == 0


async def test_reconnect_resolves_current_adapter():
    """Build a fresh controller from the current HA Bluetooth source."""
    first = BLEDevice("AA:BB:CC:DD:EE:FF", "KS-HD-Z1D", {"source": "pi"})
    second = BLEDevice("AA:BB:CC:DD:EE:FF", "KS-HD-Z1D", {"source": "proxy"})
    resolve = MagicMock(side_effect=[first, second])
    controllers = []

    def create(device, name, profile):
        controller = MagicMock()
        controller.connected = False

        async def connect():
            controller.connected = True

        async def disconnect():
            controller.connected = False

        controller.connect = AsyncMock(side_effect=connect)
        controller.disconnect = AsyncMock(side_effect=disconnect)
        controllers.append(controller)
        assert profile.profile_id == "z1"
        return controller

    api = WalkingPadApi("Z1", first.address, first.name, resolve)
    with patch(
        "custom_components.king_smith.walking_pad.KingsmithController",
        side_effect=create,
    ) as factory:
        await api.connect()
        await api.disconnect()
        await api.connect()
    assert factory.call_args_list[0].args[0] is first
    assert factory.call_args_list[1].args[0] is second
    assert len(controllers) == 2


async def test_legacy_start_sequence(api_and_controller):
    """Keep manual mode selection for previously supported WiLink pads."""
    api, controller = api_and_controller
    controller.protocol = ProtocolType.WILINK
    await api.start_belt()
    controller.switch_mode.assert_awaited_once_with(OperatingMode.MANUAL)
    controller.start.assert_awaited_once()
    controller.update_state.assert_awaited_once()
