"""Support for Securitas Direct refresh button."""

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN, SecuritasDirectDevice, SecuritasHub
from .securitas_direct_new_api import (
    Installation,
    SecuritasDirectError,
)
from .securitas_direct_new_api.dataTypes import CameraDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Securitas Direct Refresh Button based on config_entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    client: SecuritasHub = entry_data["hub"]
    buttons = []
    securitas_devices: list[SecuritasDirectDevice] = entry_data["devices"]
    for device in securitas_devices:
        buttons.append(SecuritasRefreshButton(device.installation, client, hass))
        # Camera capture buttons
        try:
            cameras = await client.get_camera_devices(device.installation)
        except Exception:
            _LOGGER.warning(
                "Failed to get camera devices for %s", device.installation.number
            )
            cameras = []
        for cam_device in cameras:
            buttons.append(
                SecuritasCaptureButton(client, device.installation, cam_device)
            )
    async_add_entities(buttons, True)


class SecuritasRefreshButton(ButtonEntity):
    """Representation of a Securitas refresh button."""

    def __init__(
        self,
        installation: Installation,
        client: SecuritasHub,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the refresh button."""
        self._attr_name = f"Refresh {installation.alias}"
        self._attr_unique_id = f"refresh_button_{installation.number}"
        self.installation = installation
        self.client = client
        self.hass = hass
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"securitas_direct.{installation.number}")},
            manufacturer="Securitas Direct",
            model=installation.panel,
            name=installation.alias,
            hw_version=installation.type,
        )

    async def async_press(self) -> None:
        """Update alarm status when button pressed."""
        if self.hass is None:
            return
        try:
            reference_id = await self.client.session.check_alarm(self.installation)
            alarm_status = await self.client.session.check_alarm_status(
                self.installation, reference_id
            )

            self.client.session.protom_response = alarm_status.protomResponse

            _LOGGER.info(
                "Status of the Alarm via API: %s installation id: %s",
                alarm_status.protomResponse,
                self.installation.number,
            )

            _LOGGER.info("Updating alarm panel entity for %s", self.installation.number)
            for entity_id in self.hass.states.async_entity_ids("alarm_control_panel"):
                if "securitas" in entity_id:
                    await self.hass.services.async_call(
                        "homeassistant",
                        "update_entity",
                        {"entity_id": entity_id},
                        blocking=True,
                    )

        except SecuritasDirectError as err:
            _LOGGER.error(
                "Error refreshing alarm status for %s: %s",
                self.installation.number,
                err.log_detail(),
            )
            if getattr(err, "http_status", None) == 403:
                await self.hass.services.async_call(
                    domain="persistent_notification",
                    service="create",
                    service_data={
                        "title": "Securitas: Rate limited",
                        "message": (
                            "Too many requests — blocked by Securitas servers. "
                            "Please wait a few minutes before trying again."
                        ),
                        "notification_id": (
                            f"{DOMAIN}.securitas_rate_limited_{self.installation.number}"
                        ),
                    },
                )
                alarm_entities = self.hass.data.get(DOMAIN, {}).get(
                    "alarm_entities", {}
                )
                alarm_entity = alarm_entities.get(self.installation.number)
                if alarm_entity is not None:
                    alarm_entity._set_waf_blocked(True)
                    alarm_entity.async_write_ha_state()


class SecuritasCaptureButton(ButtonEntity):
    """Button to capture a new image from a Securitas camera."""

    _attr_icon = "mdi:camera"

    def __init__(
        self,
        client: SecuritasHub,
        installation: Installation,
        camera_device: CameraDevice,
    ) -> None:
        """Initialize the capture button."""
        self._client = client
        self._installation = installation
        self._camera_device = camera_device
        self._attr_unique_id = f"{installation.number}_capture_{camera_device.zone_id}"
        self._attr_name = f"{installation.alias} Capture {camera_device.name}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"securitas_direct.{installation.number}")},
            manufacturer="Securitas Direct",
            model=installation.panel,
            name=installation.alias,
            hw_version=installation.type,
        )

    async def async_press(self) -> None:
        """Request a new image capture."""
        try:
            await self._client.capture_image(self._installation, self._camera_device)
        except SecuritasDirectError as err:
            _LOGGER.warning(
                "Failed to capture image from %s: %s",
                self._camera_device.name,
                err,
            )
