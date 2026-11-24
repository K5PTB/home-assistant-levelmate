"""Config flow for the LevelMate Pro integration (read-only fork).

Setup only asks which device to use. Nothing is written to the device - its
own settings (made with the official app) are left exactly as they are.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_ADDRESS, DOMAIN

MANUAL_ENTRY_VALUE = "__manual__"


class LevelMateProConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for LevelMate Pro."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_address: str | None = None
        self._discovered_name: str | None = None
        self._discovered_devices: dict[str, str] = {}

    def _create(self, address: str, name: str | None) -> FlowResult:
        return self.async_create_entry(
            title=name or f"LevelMate Pro {address}",
            data={CONF_ADDRESS: address},
        )

    async def async_step_bluetooth(
        self, discovery_info: bluetooth.BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle a discovered LevelMate Pro advertising the AA10 service."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovered_address = discovery_info.address
        self._discovered_name = discovery_info.name or discovery_info.address
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm a Bluetooth-discovered device."""
        if user_input is not None:
            assert self._discovered_address is not None
            return self._create(self._discovered_address, self._discovered_name)
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"name": self._discovered_name or ""},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick the device from nearby Bluetooth devices, or enter its address.

        The LevelMate Pro does not appear to advertise its AA10 service, so the
        list cannot be filtered: every nearby connectable device is shown.
        """
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            if address == MANUAL_ENTRY_VALUE:
                return await self.async_step_manual_address()
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            label = self._discovered_devices.get(address, address)
            return self._create(address, label.rsplit(" (", 1)[0])

        current_addresses = self._async_current_ids()
        for discovery_info in bluetooth.async_discovered_service_info(
            self.hass, connectable=True
        ):
            if discovery_info.address in current_addresses:
                continue
            self._discovered_devices[discovery_info.address] = (
                f"{discovery_info.name or 'Unknown'} ({discovery_info.address})"
            )

        choices = dict(self._discovered_devices)
        choices[MANUAL_ENTRY_VALUE] = "Enter address manually..."

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(choices)}),
        )

    async def async_step_manual_address(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manual address entry, for when the device is not in the list."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper()
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            if bluetooth.async_ble_device_from_address(
                self.hass, address, connectable=True
            ) is None:
                errors["base"] = "device_not_found"
            else:
                return self._create(address, None)

        return self.async_show_form(
            step_id="manual_address",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): str}),
            errors=errors,
        )
