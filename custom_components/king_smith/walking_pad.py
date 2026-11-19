"""Serialize WalkingPad operations and expose device-reported state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
import math
import time

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from homeassistant.exceptions import HomeAssistantError
from walkingpad_controller.const import OperatingMode, ProtocolType
from walkingpad_controller.models import TreadmillStatus

from .device_profile import Capability, DeviceProfile, SleepStrategy, resolve_profile
from .ftms import KingsmithController


class WalkingPadApi:
    """Own one connection and one command queue for a treadmill."""

    def __init__(
        self,
        name: str,
        address: str,
        ble_name: str,
        resolve_device: Callable[[], BLEDevice | None],
        profile: DeviceProfile | None = None,
    ) -> None:
        """Initialize the device without opening a connection."""
        self.name = name
        self.mac = address
        self._ble_name = ble_name
        self._resolve_device = resolve_device
        self.profile = profile or resolve_profile(None, ble_name)
        self._ctrl: KingsmithController | None = None
        self._lock = asyncio.Lock()
        self._callbacks: list[Callable[[TreadmillStatus], None]] = []
        self._disconnect_callbacks: list[Callable[[], None]] = []
        self.status: TreadmillStatus | None = None
        self._last_status_time = 0.0
        self._step_cadence = 0.0
        self._pending_speed: float | None = None
        self.speed_adjustment_step = 0.5

    def _measurement(self, field: str) -> float | int | None:
        """Return a value only after its field appeared in a device report."""
        if self.status is None or not getattr(self.status, f"has_{field}", True):
            return None
        return getattr(self.status, field)

    @property
    def connected(self) -> bool:
        """Return the live connection state."""
        return self._ctrl is not None and self._ctrl.connected

    @property
    def moving(self) -> bool | None:
        """Return whether the last device report indicates movement."""
        return self.speed > 0 if self.speed is not None else None

    @property
    def speed(self) -> float | None:
        """Return the actual speed in km/h."""
        return self._measurement("speed")

    @property
    def distance(self) -> float | None:
        """Return the session distance in kilometers."""
        distance = self._measurement("distance")
        return distance / 1000 if distance is not None else None

    @property
    def time(self) -> int | None:
        """Return the session duration in seconds."""
        return self._measurement("duration")

    @property
    def steps(self) -> int | None:
        """Return the device step counter."""
        return self._measurement("steps")

    @property
    def step_cadence(self) -> float | None:
        """Return cadence after both steps and elapsed time are available."""
        if self.steps is None or self.time is None:
            return None
        return self._step_cadence

    @property
    def min_speed(self) -> float:
        """Return the minimum running speed in km/h."""
        if self.profile.min_speed is not None:
            return self.profile.min_speed
        return self._ctrl.min_speed if self._ctrl is not None else 0.5

    @property
    def max_speed(self) -> float:
        """Return the maximum running speed in km/h."""
        if self.profile.max_speed is not None:
            return self.profile.max_speed
        return self._ctrl.max_speed if self._ctrl is not None else 6.0

    @property
    def speed_increment(self) -> float:
        """Return the supported speed increment."""
        if self.profile.speed_step is not None:
            return self.profile.speed_step
        return self._ctrl.speed_increment if self._ctrl is not None else 0.1

    @property
    def firmware_version(self) -> str | None:
        """Return the firmware version when available."""
        if self._ctrl is None:
            return None
        return self._ctrl.firmware_version or None

    def register_status_callback(self, callback: Callable) -> Callable[[], None]:
        """Register a listener and return its removal callback."""
        self._callbacks.append(callback)
        return lambda: self._callbacks.remove(callback)

    def register_disconnect_callback(self, callback: Callable) -> Callable[[], None]:
        """Register a disconnect listener and return its removal callback."""
        self._disconnect_callbacks.append(callback)
        return lambda: self._disconnect_callbacks.remove(callback)

    def _on_status_update(self, status: TreadmillStatus) -> None:
        """Snapshot mutable library status before notifying listeners."""
        previous = self.status
        if getattr(status, "has_speed", True):
            reached_target = self._pending_speed is not None and math.isclose(
                status.speed, self._pending_speed, abs_tol=1e-6
            )
            stopped = (
                previous is not None and previous.speed > 0 and status.speed == 0
            )
            if reached_target or stopped:
                self._pending_speed = None
        self._step_cadence = 0.0
        if previous is not None and previous.speed > 0 and status.speed > 0:
            elapsed = status.duration - previous.duration
            steps = status.steps - previous.steps
            fields_known = all(
                getattr(report, field, True)
                for report in (previous, status)
                for field in ("has_steps", "has_duration")
            )
            if elapsed > 0 and steps >= 0 and fields_known:
                self._step_cadence = round(steps * 60 / elapsed, 2)
        self.status = replace(status)
        self._last_status_time = time.monotonic()
        for callback in tuple(self._callbacks):
            callback(self.status)

    def _on_disconnect(self) -> None:
        """Notify listeners immediately when the BLE link closes."""
        self._pending_speed = None
        for callback in tuple(self._disconnect_callbacks):
            callback()

    async def _connect(self) -> None:
        """Connect using a fresh device reference from Home Assistant."""
        if self.connected:
            return
        self._pending_speed = None
        if self._ctrl is not None:
            await self._ctrl.disconnect()
        device = self._resolve_device()
        if device is None:
            raise BleakError(f"No connectable Bluetooth source can reach {self.mac}")
        self.status = None
        self._step_cadence = 0.0
        self._ctrl = KingsmithController(
            device, self._ble_name or device.name, self.profile
        )
        self._ctrl.register_status_callback(self._on_status_update)
        self._ctrl.register_disconnect_callback(self._on_disconnect)
        try:
            await self._ctrl.connect()
        except BaseException:
            await self._ctrl.disconnect()
            raise

    async def connect(self) -> None:
        """Open a connection without moving the belt."""
        async with self._lock:
            await self._connect()

    async def disconnect(self) -> None:
        """Release the BLE connection."""
        async with self._lock:
            self._pending_speed = None
            if self._ctrl is not None:
                await self._ctrl.disconnect()

    async def update_state(self) -> TreadmillStatus | None:
        """Maintain the connection and poll devices that require it."""
        async with self._lock:
            await self._connect()
            if self._ctrl.protocol == ProtocolType.WILINK:
                await self._ctrl.update_state()
            elif self.moving and time.monotonic() - self._last_status_time > 30:
                await self._ctrl.disconnect()
                raise BleakError("No treadmill data received for 30 seconds")
            return self.status

    async def _command(self, command: str, *args: float) -> None:
        """Execute an entire command sequence under the shared lock."""
        async with self._lock:
            try:
                await self._connect()
                if command == "adjust_speed":
                    current = self._pending_speed
                    if current is None:
                        current = self.speed
                    if current is None:
                        raise HomeAssistantError(
                            "Cannot adjust speed before a speed report is received"
                        )
                    target = (
                        max(self.min_speed, current)
                        + args[0] * self.speed_adjustment_step
                    )
                    target = min(self.max_speed, max(self.min_speed, target))
                    increments = round(target / self.speed_increment)
                    args = (round(increments * self.speed_increment, 10),)
                    command = "set_speed"
                if command == "set_speed":
                    speed = args[0]
                    if not self.min_speed <= speed <= self.max_speed:
                        raise HomeAssistantError(
                            f"Running speed must be {self.min_speed}–{self.max_speed} km/h"
                        )
                    increments = speed / self.speed_increment
                    if not math.isclose(increments, round(increments), abs_tol=1e-6):
                        raise HomeAssistantError(
                            f"Speed must use increments of {self.speed_increment} km/h"
                        )
                if command == "start" and self._ctrl.protocol == ProtocolType.WILINK:
                    if not await self._ctrl.switch_mode(OperatingMode.MANUAL):
                        raise HomeAssistantError("The treadmill rejected manual mode")
                if command == "sleep":
                    self._pending_speed = None
                    if not self.profile.supports(Capability.SLEEP):
                        raise HomeAssistantError("Sleep is not supported by this model")
                    if self.profile.sleep_strategy == SleepStrategy.STANDBY:
                        if not await self._ctrl.switch_mode(OperatingMode.STANDBY):
                            raise HomeAssistantError(
                                "The treadmill rejected standby mode"
                            )
                        await self._ctrl.update_state()
                        return
                    if self.moving and not await self._ctrl.stop():
                        raise HomeAssistantError("The treadmill did not confirm stop")
                    await asyncio.sleep(0.4)
                if command in ("start", "stop"):
                    self._pending_speed = None
                if command == "set_speed":
                    # Keep the target separate from measured speed. A report
                    # arriving during the write can already confirm the target.
                    self._pending_speed = args[0]
                try:
                    if not await getattr(self._ctrl, command)(*args):
                        raise HomeAssistantError(
                            f"The treadmill did not confirm {command}"
                        )
                except BaseException:
                    self._pending_speed = None
                    raise
                if command == "stop" and self._ctrl.protocol == ProtocolType.WILINK:
                    if not await self._ctrl.switch_mode(OperatingMode.STANDBY):
                        raise HomeAssistantError("The treadmill rejected standby mode")
                if self._ctrl.protocol == ProtocolType.WILINK:
                    await self._ctrl.update_state()
            except (BleakError, TimeoutError, RuntimeError) as error:
                self._pending_speed = None
                if not self.connected:
                    self._on_disconnect()
                raise HomeAssistantError(
                    f"WalkingPad command failed: {error}"
                ) from error

    async def start_belt(self) -> None:
        """Start the belt at its device-selected starting speed."""
        await self._command("start")

    async def stop_belt(self) -> None:
        """Stop the belt and end the current session."""
        await self._command("stop")

    async def sleep(self) -> None:
        """Stop the belt and put the controller into low-power mode."""
        await self._command("sleep")

    async def change_speed(self, speed: float) -> None:
        """Set speed in km/h, mapping zero to an explicit stop."""
        if not math.isfinite(speed) or speed < 0:
            raise HomeAssistantError("Speed must be a finite, non-negative number")
        if speed == 0:
            await self.stop_belt()
            return
        await self._command("set_speed", speed)

    async def adjust_speed(self, direction: int) -> None:
        """Queue an adjustment relative to the pending target or measured speed."""
        if direction not in (-1, 1):
            raise ValueError("Direction must be -1 or 1")
        await self._command("adjust_speed", direction)
