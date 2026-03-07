"""Securitas direct sentinel sensor."""

from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.sensor.const import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

import logging

from . import DOMAIN, SecuritasDirectDevice, SecuritasHub
from .constants import SentinelName
from .securitas_direct_new_api import Installation, SecuritasDirectError
from .securitas_direct_new_api.dataTypes import Service

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=30)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Securitas Direct sentinel sensors based on config_entry.

    No API calls are made here beyond service discovery (already cached from
    __init__ setup).  Entities start with unknown state; the first periodic
    ``async_update`` populates values via rate-limited hub methods.
    """
    entry_data = hass.data[DOMAIN][entry.entry_id]
    client: SecuritasHub = entry_data["hub"]
    sensors: list[SensorEntity] = []
    securitas_devices: list[SecuritasDirectDevice] = entry_data["devices"]

    sentinel_name: SentinelName = SentinelName()
    sentinel_confort_name = sentinel_name.get_sentinel_name(client.lang)
    for device in securitas_devices:
        try:
            services: list[Service] = await client.get_services(device.installation)
        except SecuritasDirectError as err:
            _LOGGER.warning(
                "Skipping installation %s for sensor setup: %s",
                device.installation.number,
                err.log_detail(),
            )
            continue
        for service in services:
            if service.request == sentinel_confort_name:
                sensors.append(
                    SentinelTemperature(service, client, device.installation)
                )
                sensors.append(SentinelHumidity(service, client, device.installation))
                sensors.append(SentinelAirQuality(service, client, device.installation))
    async_add_entities(sensors, False)


def _device_info(installation: Installation) -> DeviceInfo:
    """Build DeviceInfo that groups under the installation device."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"securitas_direct.{installation.number}")},
        manufacturer="Securitas Direct",
        model=installation.panel,
        name=installation.alias,
        hw_version=installation.type,
    )


class SentinelTemperature(SensorEntity):
    """Sentinel temperature sensor."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        service: Service,
        client: SecuritasHub,
        installation: Installation,
    ) -> None:
        """Init the component."""
        self._attr_unique_id = f"{installation.number}_temperature_{service.id}"
        self._attr_name = f"{installation.alias} Temperature"
        self._service: Service = service
        self._client: SecuritasHub = client
        self._installation = installation
        self._attr_device_info = _device_info(installation)

    async def async_update(self):
        """Update the sensor via the hub's rate-limited method."""
        if self.hass is None:
            return
        try:
            sentinel = await self._client.get_sentinel(
                self._installation, self._service
            )
        except SecuritasDirectError as err:
            _LOGGER.warning(
                "Error updating temperature for %s: %s",
                self._installation.number,
                err.log_detail(),
            )
            return
        self._attr_native_value = sentinel.temperature


class SentinelHumidity(SensorEntity):
    """Sentinel Humidity sensor."""

    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(
        self,
        service: Service,
        client: SecuritasHub,
        installation: Installation,
    ) -> None:
        """Init the component."""
        self._attr_unique_id = f"{installation.number}_humidity_{service.id}"
        self._attr_name = f"{installation.alias} Humidity"
        self._service: Service = service
        self._client: SecuritasHub = client
        self._installation = installation
        self._attr_device_info = _device_info(installation)

    async def async_update(self):
        """Update the sensor via the hub's rate-limited method."""
        if self.hass is None:
            return
        try:
            sentinel = await self._client.get_sentinel(
                self._installation, self._service
            )
        except SecuritasDirectError as err:
            _LOGGER.warning(
                "Error updating humidity for %s: %s",
                self._installation.number,
                err.log_detail(),
            )
            return
        self._attr_native_value = sentinel.humidity


class SentinelAirQuality(SensorEntity):
    """Sentinel Air Quality sensor."""

    def __init__(
        self,
        service: Service,
        client: SecuritasHub,
        installation: Installation,
    ) -> None:
        """Init the component."""
        self._attr_unique_id = f"{installation.number}_airquality_{service.id}"
        self._attr_name = f"{installation.alias} Air Quality"
        self._service: Service = service
        self._client: SecuritasHub = client
        self._installation = installation
        self._air_quality_value: int | None = None
        self._air_quality_message: str | None = None
        self._attr_device_info = _device_info(installation)

    async def async_update(self):
        """Update the air quality sensor via the hub's rate-limited method."""
        if self.hass is None:
            return
        try:
            air_quality = await self._client.get_air_quality(
                self._installation, self._service
            )
        except SecuritasDirectError:
            _LOGGER.debug(
                "Air quality data not available for installation %s",
                self._installation.number,
            )
            return
        self._air_quality_value = air_quality.value
        self._air_quality_message = air_quality.message
        self._attr_native_value = air_quality.message

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:  # type: ignore[override]
        """Return the state attributes."""
        if self._air_quality_message is None:
            return None
        return {
            "value": self._air_quality_value,
            "message": self._air_quality_message,
        }
