# King Smith Walking Pad for Home Assistant

[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

A custom Home Assistant integration for local Bluetooth control of King Smith
WalkingPads. It provides start/stop control, a speed control in km/h, live session
data, and persistent totals for distance, duration, and steps.

This fork adds FTMS support and the King Smith unlock sequence required by the
Z1 advertised as `KS-HD-Z1D`. It targets Home Assistant 2026.11.2 or newer.
The original integration also supports the C2 and A1 Pro through the legacy
WiLink protocol.

The A1 Pro advertised as `KS-ST-A1P` has a dedicated model profile with a
0.5–6.0 km/h speed range, 0.1 km/h speed increments, and WiLink standby support.
The controls use the existing WiLink implementation, with the speed range from
the [A1 Pro specifications](https://www.walkingpad.nl/en/products/kingsmith-walkingpad-a1-pro-folding-treadmill).
Verification with this fork on a physical A1 Pro is still pending.
Its Bluetooth name is also accepted with a trailing device suffix. Older entries
without a saved profile are identified from their stored Bluetooth name. Entries
with an explicitly saved profile keep that selection.

## Requirements

- Home Assistant 2026.11.2 or newer.
- A local Bluetooth adapter or an ESPHome Bluetooth proxy that supports active
  connections.
- The WalkingPad must be powered on and in Bluetooth range.
- KS Fit and other apps must be fully closed. A BLE device can normally maintain
  only one control connection at a time.

## Install with HACS

1. Open HACS, select the three-dot menu, and choose **Custom repositories**.
2. Add `https://github.com/koalexbear/king_smith` as an **Integration**.
3. Open **King Smith Walking Pad** and select **Download**. If HACS asks for a
   version, choose `main`.
4. Restart Home Assistant.

When updating an existing installation, refresh the repository information in
HACS and choose **Redownload** if the new version is not shown immediately.

## Add the WalkingPad

You normally do **not** need to know or type a MAC address.

1. Power on the WalkingPad and fully close KS Fit.
2. Wait up to 30 seconds so the local adapter or a Bluetooth proxy can report an
   advertisement.
3. Open **Settings → Devices & services**. If Home Assistant shows a discovered
   **King Smith Walking Pad**, select **Configure**.
4. Otherwise select **Add integration → King Smith Walking Pad**.
5. Choose `KS-HD-Z1D (…)` for the Z1 or `KS-ST-A1P (…)` for the A1 Pro from the
   **WalkingPad** drop-down, assign a friendly name, and submit the form.

The value in parentheses is the Bluetooth address detected by Home Assistant.
It identifies the physical device across the local adapter and all proxies. The
integration stores it so it can reconnect to the same WalkingPad later; you do
not need to obtain it yourself when the device appears in the list.

Manual address entry is only a fallback for installations where Home Assistant
can reach the device but has not yet displayed it in the drop-down. Find the
address under **Settings → Devices & services → Bluetooth → Advertisement
monitor**, locate the entry whose local name is `KS-HD-Z1D` or `KS-ST-A1P`, and
copy its Bluetooth address. Do not enter the Raspberry Pi's address, the proxy's address,
or the address of a phone.

If the list is empty, check that:

- the WalkingPad is on and KS Fit is disconnected;
- at least one adapter or proxy reports it as connectable;
- the proxy is close enough to the WalkingPad;
- you waited for a fresh advertisement after opening the setup dialog.

## Controls and sensors

The switch starts or stops the belt. The speed slider covers 1.0–6.0 km/h on the
Z1 and 0.5–6.0 km/h on the A1 Pro, in 0.1 km/h increments. The Increase speed
and Decrease speed buttons adjust that value using the configurable Speed step
size entity. Its range is 0.1–1.0 km/h, its default is 0.5 km/h, and Home Assistant restores the
selected value after a restart. Changes are clamped to the model profile's safe
speed limits.

While a speed change is still pending, the Increase/Decrease buttons adjust the
last requested target instead of an older measured speed. Rapid presses are
calculated in command-queue order. Once a speed report reaches that target,
subsequent adjustments follow the reported speed again, including changes made
with the physical remote. Pending targets are cleared on stop, sleep, start,
disconnect, or a failed speed write; displayed speed remains device-reported.

The separate Sleep button stops the belt if necessary and then puts the Z1 into
its low-power mode. Wake the Z1 with its physical remote.

Live sensors include speed, session distance, session duration, steps, and step
cadence. Values can remain **Unknown** until the first corresponding Bluetooth
report arrives. Total distance, duration, and steps are restored after a Home
Assistant restart. Sessions completed entirely while Home Assistant is offline
cannot be recovered unless the WalkingPad reports that history later.

## Adding another WalkingPad model

Before adding a profile, collect the exact model name, Bluetooth local name,
firmware version, protocol or GATT service UUIDs, minimum and maximum speed,
supported speed increment, and the behavior of start, stop, and sleep. Values
from a manual or verified hardware test are preferred over values copied from a
similar-looking model.

Model-specific behavior lives in
`custom_components/king_smith/device_profile.py`. To add a model that uses an
already supported protocol:

1. Add a uniquely named `DeviceProfile`. Use a stable, lowercase `profile_id`;
   a user-facing `model`; the narrowest reliable `name_patterns`; and only the
   capabilities confirmed on hardware.
2. Set `min_speed`, `max_speed`, and `speed_step` to the device's actual safe
   values. Do not reuse another model's limits without verification.
3. Select an existing `sleep_strategy` only if that exact command sequence has
   been tested. Set `supplement_unlock` only when the model requires the same
   supplement-channel initialization.
4. Add the profile to `DEVICE_PROFILES`. Put specific name patterns before broad
   fallback profiles so the intended model wins matching.
5. Add the model's Bluetooth local-name pattern to the `bluetooth` section of
   `custom_components/king_smith/manifest.json`. Without this entry, Home
   Assistant may not start discovery for the integration.
6. Add profile matching, safe-limit, capability, and backward-compatibility
   coverage to `tests/test_device_profile.py`. Add discovery coverage to
   `tests/test_config_flow.py`.
7. Test every enabled control on the physical WalkingPad, starting with the belt
   stopped and at the minimum speed. Include the advertised name, firmware
   version, and test results in the pull request.

For example, the profile itself follows this shape:

```python
EXAMPLE_PROFILE = DeviceProfile(
    profile_id="example_model",
    model="WalkingPad Example",
    name_patterns=("KS-EXAMPLE-*",),
    capabilities=frozenset({Capability.SPEED_CONTROL}),
    min_speed=1.0,
    max_speed=6.0,
    speed_step=0.1,
)
```

Discovery stores the stable profile ID in new config entries. Entries created by
older releases remain compatible because their profile is inferred from the
saved Bluetooth name. The first profile whose name pattern matches is selected.

A data-only profile is sufficient only when the model uses an existing protocol
and command strategy. Add or extend a protocol adapter when its GATT services,
unlock sequence, packet format, or command frames differ. A new anonymous FTMS
model also needs explicit discovery logic because unnamed FTMS advertisements
currently fall back to the Z1 profile. Unknown proprietary controls must remain
disabled until their command semantics and safe limits have been verified on
real hardware.

Before submitting a change, run:

```text
PYTHONPATH=. ruff check custom_components tests
PYTHONPATH=. pytest -q
```

## Troubleshooting

Enable debug logging for this integration from its Home Assistant integration
page. For protocol details, also set `walkingpad_controller` to debug. When
reporting a problem, include the exact setup or connection error, relevant log
lines, the advertised Bluetooth name, and whether the connection uses the local
adapter or an ESPHome proxy.

The protocol implementation uses
[walkingpad-controller](https://github.com/mcdax/walkingpad-controller). The Z1
unlock sequence is documented by
[z1-walkingpad-mcp](https://github.com/slandau3/z1-walkingpad-mcp#how-it-works-the-protocol).

For legacy WiLink devices such as the A1 Pro, the integration spaces Bluetooth
writes by at least the underlying library's minimum interval (currently 690 ms),
including mode changes and status requests. This works around a timing error in
`ph4-walkingpad` 1.0.2 that can send consecutive commands too close together.
Connection establishment uses `bleak-retry-connector` with Home Assistant's
discovered Bluetooth device. Belt commands themselves are not automatically
retried. Enable `custom_components.king_smith.wilink` and `ph4_walkingpad.pad`
debug logging to inspect outgoing commands and incoming WiLink reports.

## Manual installation

1. Copy `custom_components/king_smith` from this repository into the
   `custom_components` directory inside your Home Assistant configuration
   directory.
2. Restart Home Assistant.
3. Follow **Add the WalkingPad** above.

Contributions and hardware test reports are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md).

[commits-shield]: https://img.shields.io/github/commit-activity/y/koalexbear/king_smith.svg?style=for-the-badge
[commits]: https://github.com/koalexbear/king_smith/commits/main
[license-shield]: https://img.shields.io/github/license/koalexbear/king_smith.svg?style=for-the-badge
