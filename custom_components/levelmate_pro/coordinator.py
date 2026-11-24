"""BLE connection + data coordinator for the LevelMate Pro (read-only fork).

Connection model ("on demand"):

* Nothing connects until the user turns the connection switch on. The switch
  starts off at every Home Assistant start, so the official phone app can
  always reach the device first.
* While the switch is on, the coordinator connects whenever the device is
  advertising (it advertises while it is powered on and awake) and reconnects
  after a drop.
* If the switch is on but the device has not been connected for
  GRACE_SECONDS - it was switched off, or it went to sleep - the switch turns
  itself off again.

The coordinator never writes to the device: it only subscribes to
notifications and, once per connection, reads the current values.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from bleak import BleakClient
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CHAR_FRONT,
    CHAR_SIDE,
    CHAR_TEMPERATURE,
    DOMAIN,
    GRACE_SECONDS,
    RETRY_SECONDS,
)
from .levelmate_math import (
    CornerDeltas,
    celsius_to_fahrenheit,
    decode_float32_le,
    decode_int16_le,
    reconstruct_corners,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class LevelMateData:
    """Latest known state, pushed to entities through the coordinator.

    Readings are cleared whenever the connection starts or ends, so entities
    never show a value from an earlier connection as if it were live.
    """

    side_raw: int | None = None
    front_raw: int | None = None
    temperature_c: float | None = None
    temperature_f: float | None = None
    corners: CornerDeltas | None = None
    connected: bool = False
    enabled: bool = False

    def clear_readings(self) -> None:
        self.side_raw = None
        self.front_raw = None
        self.temperature_c = None
        self.temperature_f = None
        self.corners = None


class LevelMateCoordinator(DataUpdateCoordinator[LevelMateData]):
    """Owns the (optional) BLE connection to one LevelMate Pro unit."""

    def __init__(self, hass: HomeAssistant, address: str) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None)
        self.address = address
        self.data = LevelMateData()

        self._client: BleakClient | None = None
        self._connect_task: asyncio.Task | None = None
        self._last_attempt = 0.0
        self._cancel_grace: CALLBACK_TYPE | None = None
        self._unsub_adv: Callable[[], None] | None = None

    # --- lifecycle ------------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Start listening for the device's advertisements. Does not connect."""
        self._unsub_adv = bluetooth.async_register_callback(
            self.hass,
            self._on_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=True),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )

    async def async_shutdown(self) -> None:
        """Stop listening and release the connection (entry unload)."""
        if self._unsub_adv is not None:
            self._unsub_adv()
            self._unsub_adv = None
        await self.async_set_enabled(False)
        await super().async_shutdown()

    # --- the switch -----------------------------------------------------------

    async def async_set_enabled(self, enabled: bool) -> None:
        """Turn Home Assistant's connection on or off (the switch)."""
        if enabled == self.data.enabled:
            return
        if enabled:
            self.data.enabled = True
            self._start_grace()
            self.async_set_updated_data(self.data)
            self._maybe_connect()
            return

        self.data.enabled = False
        self._stop_grace()
        # Wait for any connect in flight to finish (or be cancelled) BEFORE
        # disconnecting, so a connect that completes anyway is released too.
        task = self._connect_task
        self._connect_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._disconnect()
        self.data.connected = False
        self.data.clear_readings()
        self.async_set_updated_data(self.data)
        _LOGGER.info("LevelMate Pro %s: connection switched off", self.address)

    # --- grace timer: switch turns itself off ---------------------------------

    @callback
    def _start_grace(self) -> None:
        if self._cancel_grace is None:
            self._cancel_grace = async_call_later(
                self.hass, GRACE_SECONDS, self._on_grace_expired
            )

    @callback
    def _stop_grace(self) -> None:
        if self._cancel_grace is not None:
            self._cancel_grace()
            self._cancel_grace = None

    async def _on_grace_expired(self, _now) -> None:
        self._cancel_grace = None
        if self._connect_task is not None and not self._connect_task.done():
            # The device just woke and a connect is in progress: let it finish.
            self._start_grace()
            return
        if self.data.enabled and not self.data.connected:
            _LOGGER.info(
                "LevelMate Pro %s not connected for %d s (switched off or "
                "asleep) - turning the connection switch off",
                self.address, GRACE_SECONDS,
            )
            await self.async_set_enabled(False)

    # --- connecting -----------------------------------------------------------

    @callback
    def _on_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        self._maybe_connect()

    @callback
    def _maybe_connect(self) -> None:
        if not self.data.enabled or self.data.connected:
            return
        if self._connect_task is not None and not self._connect_task.done():
            return
        now = time.monotonic()
        if now - self._last_attempt < RETRY_SECONDS:
            return
        if bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        ) is None:
            return
        self._last_attempt = now
        self._connect_task = self.hass.async_create_background_task(
            self._async_connect(), f"{DOMAIN}_connect_{self.address}"
        )

    async def _async_connect(self) -> None:
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            return
        _LOGGER.debug("Connecting to LevelMate Pro %s", self.address)
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                self.address,
                disconnected_callback=self._on_disconnected,
                max_attempts=2,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("LevelMate Pro %s: connect failed: %s", self.address, exc)
            return

        if not self.data.enabled:
            # Switched off while we were connecting.
            await self._safe_disconnect(client)
            return

        self._client = client
        self.data.clear_readings()
        try:
            await client.start_notify(CHAR_SIDE, self._on_side)
            await client.start_notify(CHAR_FRONT, self._on_front)
            await client.start_notify(CHAR_TEMPERATURE, self._on_temperature)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "LevelMate Pro %s: could not subscribe (%s); will retry",
                self.address, exc,
            )
            self._client = None
            await self._safe_disconnect(client)
            return

        self.data.connected = True
        self._stop_grace()
        _LOGGER.info("LevelMate Pro %s connected (read-only)", self.address)

        # Notifications may only arrive when a value changes, so read the
        # current values once. Reads only; failures are harmless.
        for char, handler in (
            (CHAR_SIDE, self._on_side),
            (CHAR_FRONT, self._on_front),
            (CHAR_TEMPERATURE, self._on_temperature),
        ):
            try:
                handler(None, await client.read_gatt_char(char))
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "LevelMate Pro %s: initial read of %s failed: %s",
                    self.address, char, exc,
                )
        self.async_set_updated_data(self.data)

    async def _disconnect(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await self._safe_disconnect(client)

    @staticmethod
    async def _safe_disconnect(client: BleakClient) -> None:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    @callback
    def _on_disconnected(self, client: BleakClient) -> None:
        if client is not self._client and self._client is not None:
            return  # a stale client from an abandoned connect
        self._client = None
        was_connected = self.data.connected
        self.data.connected = False
        self.data.clear_readings()
        if self.data.enabled:
            if was_connected:
                _LOGGER.info(
                    "LevelMate Pro %s disconnected; will reconnect while it "
                    "is awake", self.address,
                )
            self._start_grace()
        self.async_set_updated_data(self.data)

    # --- notifications ----------------------------------------------------------

    def _recompute_corners_if_ready(self) -> None:
        if self.data.side_raw is not None and self.data.front_raw is not None:
            self.data.corners = reconstruct_corners(
                self.data.side_raw, self.data.front_raw
            )

    @callback
    def _on_side(self, _sender, raw: bytearray) -> None:
        value = decode_int16_le(bytes(raw))
        if value is None:
            return
        self.data.side_raw = value
        self._recompute_corners_if_ready()
        self.async_set_updated_data(self.data)

    @callback
    def _on_front(self, _sender, raw: bytearray) -> None:
        value = decode_int16_le(bytes(raw))
        if value is None:
            return
        self.data.front_raw = value
        self._recompute_corners_if_ready()
        self.async_set_updated_data(self.data)

    @callback
    def _on_temperature(self, _sender, raw: bytearray) -> None:
        value = decode_float32_le(bytes(raw))
        if value is None:
            return
        self.data.temperature_c = value
        self.data.temperature_f = celsius_to_fahrenheit(value)
        self.async_set_updated_data(self.data)
