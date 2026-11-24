"""Tests for the read-only, connect-on-demand LevelMate Pro fork.

The fake BLE client fails the test on ANY write, so every test that connects
also proves the integration never writes to the device.
"""
from __future__ import annotations

import asyncio
import struct
from datetime import timedelta
from unittest.mock import patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.levelmate_pro import coordinator as coord_mod
from custom_components.levelmate_pro.const import (
    CHAR_FRONT,
    CHAR_SIDE,
    CHAR_TEMPERATURE,
    DOMAIN,
    GRACE_SECONDS,
    RETRY_SECONDS,
)
from custom_components.levelmate_pro.coordinator import LevelMateCoordinator

ADDRESS = "AA:BB:CC:DD:EE:FF"


def i16(v: int) -> bytearray:
    return bytearray(struct.pack("<h", v))


def f32(v: float) -> bytearray:
    return bytearray(struct.pack("<f", v))


class FakeClient:
    """Stands in for a connected BleakClient. Writing is a test failure."""

    def __init__(self, disconnected_callback, values=None) -> None:
        self.disconnected_callback = disconnected_callback
        self.handlers = {}
        self.values = values or {
            CHAR_SIDE: i16(4),  # +1.00 in
            CHAR_FRONT: i16(-8),  # -2.00 in
            CHAR_TEMPERATURE: f32(20.0),
        }
        self.disconnect_calls = 0

    async def start_notify(self, char, handler):
        self.handlers[char] = handler

    async def read_gatt_char(self, char):
        return self.values[char]

    async def write_gatt_char(self, *args, **kwargs):
        raise AssertionError(f"integration wrote to the device: {args!r}")

    async def disconnect(self):
        self.disconnect_calls += 1
        self.disconnected_callback(self)

    def drop(self):
        """Simulate the device going away (switched off / asleep)."""
        self.disconnected_callback(self)


class FakeBluetooth:
    """Patches the bluetooth helpers and establish_connection."""

    def __init__(self) -> None:
        self.present = False
        self.adv_callback = None
        self.clients: list[FakeClient] = []
        self.gate: asyncio.Event | None = None
        self.stubborn = False
        self.fail = False

    def register(self, hass, cb, matcher, mode):
        self.adv_callback = cb
        return lambda: None

    def ble_device(self, hass, address, connectable=True):
        return object() if self.present else None

    async def establish(self, cls, device, name, disconnected_callback, **kw):
        if self.gate is not None:
            if self.stubborn:
                # A connect that completes even though it was cancelled, as a
                # BLE stack can: the device is connected by the time we return.
                while not self.gate.is_set():
                    try:
                        await self.gate.wait()
                    except asyncio.CancelledError:
                        continue
            else:
                await self.gate.wait()
        if self.fail:
            raise TimeoutError("no answer")
        client = FakeClient(disconnected_callback)
        self.clients.append(client)
        return client

    def advertise(self):
        self.adv_callback(None, None)


@pytest.fixture
def fake_bt():
    fb = FakeBluetooth()
    with patch.object(coord_mod.bluetooth, "async_register_callback", fb.register), patch.object(
        coord_mod.bluetooth, "async_ble_device_from_address", fb.ble_device
    ), patch.object(coord_mod, "establish_connection", fb.establish):
        yield fb


@pytest.fixture
def clock():
    """Controls time.monotonic() used for the retry throttle."""
    now = [1000.0]
    with patch.object(coord_mod.time, "monotonic", lambda: now[0]):
        yield now


async def _coordinator(hass: HomeAssistant) -> LevelMateCoordinator:
    c = LevelMateCoordinator(hass, ADDRESS)
    c.async_start()
    return c


def _fire(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))


async def test_off_by_default_never_connects(hass, fake_bt, clock):
    fake_bt.present = True
    c = await _coordinator(hass)
    fake_bt.advertise()
    await hass.async_block_till_done()
    assert not c.data.enabled
    assert fake_bt.clients == []


async def test_connects_reads_and_never_writes(hass, fake_bt, clock):
    fake_bt.present = True
    c = await _coordinator(hass)
    await c.async_set_enabled(True)
    await hass.async_block_till_done()

    assert c.data.connected
    assert len(fake_bt.clients) == 1
    # Initial read: side +1.00 in, front -2.00 in -> front is high, side > 0
    assert c.data.corners.reference_corner == "driver_front"
    assert c.data.corners.passenger_front_in == 1.0
    assert c.data.corners.driver_back_in == 2.0
    assert c.data.corners.passenger_back_in == 3.0
    assert c.data.temperature_c == pytest.approx(20.0)

    # Live notification updates the corners
    fake_bt.clients[0].handlers[CHAR_SIDE](CHAR_SIDE, i16(-2))
    assert c.data.corners.reference_corner == "passenger_front"
    assert c.data.corners.driver_front_in == 0.5


async def test_switch_turns_itself_off_when_device_never_appears(hass, fake_bt, clock):
    c = await _coordinator(hass)
    await c.async_set_enabled(True)
    _fire(hass, GRACE_SECONDS - 5)
    await hass.async_block_till_done()
    assert c.data.enabled
    _fire(hass, GRACE_SECONDS + 5)
    await hass.async_block_till_done()
    assert not c.data.enabled
    assert fake_bt.clients == []


async def test_connected_device_keeps_switch_on_past_grace(hass, fake_bt, clock):
    fake_bt.present = True
    c = await _coordinator(hass)
    await c.async_set_enabled(True)
    await hass.async_block_till_done()
    _fire(hass, GRACE_SECONDS * 3)
    await hass.async_block_till_done()
    assert c.data.enabled and c.data.connected


async def test_drop_clears_readings_then_reconnects_on_advertisement(hass, fake_bt, clock):
    fake_bt.present = True
    c = await _coordinator(hass)
    await c.async_set_enabled(True)
    await hass.async_block_till_done()
    fake_bt.clients[0].drop()
    assert not c.data.connected
    assert c.data.corners is None and c.data.temperature_c is None

    # Throttled: an immediate advertisement does not hammer the device
    fake_bt.advertise()
    await hass.async_block_till_done()
    assert len(fake_bt.clients) == 1

    clock[0] += RETRY_SECONDS + 1
    fake_bt.advertise()
    await hass.async_block_till_done()
    assert c.data.connected
    assert len(fake_bt.clients) == 2


async def test_drop_then_device_gone_switches_off(hass, fake_bt, clock):
    fake_bt.present = True
    c = await _coordinator(hass)
    await c.async_set_enabled(True)
    await hass.async_block_till_done()
    fake_bt.present = False
    fake_bt.clients[0].drop()
    _fire(hass, GRACE_SECONDS + 5)
    await hass.async_block_till_done()
    assert not c.data.enabled


async def test_turning_off_disconnects_and_clears(hass, fake_bt, clock):
    fake_bt.present = True
    c = await _coordinator(hass)
    await c.async_set_enabled(True)
    await hass.async_block_till_done()
    client = fake_bt.clients[0]
    await c.async_set_enabled(False)
    assert client.disconnect_calls == 1
    assert not c.data.connected and c.data.corners is None

    # And it stays off: advertisements do not reconnect
    clock[0] += RETRY_SECONDS + 1
    fake_bt.advertise()
    await hass.async_block_till_done()
    assert len(fake_bt.clients) == 1


@pytest.mark.parametrize("stubborn", [False, True])
async def test_turning_off_mid_connect_releases_device(hass, fake_bt, clock, stubborn):
    fake_bt.present = True
    fake_bt.stubborn = stubborn
    fake_bt.gate = asyncio.Event()
    c = await _coordinator(hass)
    await c.async_set_enabled(True)
    await asyncio.sleep(0)
    off = hass.async_create_task(c.async_set_enabled(False))
    await asyncio.sleep(0)
    fake_bt.gate.set()
    await off
    await hass.async_block_till_done()
    assert not c.data.connected and not c.data.enabled
    assert c._client is None
    if stubborn:
        assert len(fake_bt.clients) == 1
    for client in fake_bt.clients:
        assert client.disconnect_calls == 1


async def test_failed_connect_retries_on_later_advertisement(hass, fake_bt, clock):
    fake_bt.present = True
    fake_bt.fail = True
    c = await _coordinator(hass)
    await c.async_set_enabled(True)
    await hass.async_block_till_done()
    assert not c.data.connected
    fake_bt.fail = False
    clock[0] += RETRY_SECONDS + 1
    fake_bt.advertise()
    await hass.async_block_till_done()
    assert c.data.connected


async def test_setup_succeeds_with_device_asleep_and_switch_off(hass, fake_bt, clock):
    hass.config.components.add("bluetooth")
    entry = MockConfigEntry(domain=DOMAIN, data={"address": ADDRESS}, unique_id=ADDRESS)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    switches = [s for s in hass.states.async_all("switch")]
    assert len(switches) == 1 and switches[0].state == "off"
    corner = [s for s in hass.states.async_all("sensor") if "corner" in s.entity_id]
    assert corner and all(s.state == "unavailable" for s in corner)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_grace_expiry_does_not_cancel_a_connect_in_progress(hass, fake_bt, clock):
    fake_bt.present = True
    fake_bt.gate = asyncio.Event()
    c = await _coordinator(hass)
    await c.async_set_enabled(True)
    await asyncio.sleep(0)
    _fire(hass, GRACE_SECONDS + 5)
    await asyncio.sleep(0)
    assert c.data.enabled
    fake_bt.gate.set()
    await hass.async_block_till_done()
    assert c.data.connected and c.data.enabled
