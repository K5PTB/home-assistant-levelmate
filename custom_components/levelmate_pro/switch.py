"""Connection switch for the LevelMate Pro integration (read-only fork)."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LevelMateCoordinator, LevelMateData


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: LevelMateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([LevelMateConnectionSwitch(coordinator, entry)])


class LevelMateConnectionSwitch(CoordinatorEntity[LevelMateCoordinator], SwitchEntity):
    """Lets Home Assistant connect to the LevelMate Pro, on demand.

    The device accepts one Bluetooth connection at a time. While this switch is
    off (the default after every Home Assistant start) Home Assistant leaves
    the device alone, so the official phone app can use it. Turn it on to show
    the readings in Home Assistant; it turns itself off again a few minutes
    after the LevelMate is switched off or goes to sleep.
    """

    _attr_has_entity_name = True
    _attr_name = "Connection"
    _attr_icon = "mdi:bluetooth-connect"

    def __init__(self, coordinator: LevelMateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.data['address']}_connection_switch"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data["address"])},
            name=entry.title,
            manufacturer="LogicBlue",
            model="LevelMate Pro",
        )

    @property
    def is_on(self) -> bool:
        data: LevelMateData = self.coordinator.data
        return bool(data and data.enabled)

    @property
    def available(self) -> bool:
        # Always controllable, even while disconnected.
        return True

    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        data: LevelMateData = self.coordinator.data
        return {"connected": bool(data and data.connected)}

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_enabled(False)
