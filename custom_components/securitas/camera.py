"""Securitas Direct camera platform."""

import logging

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN, SIGNAL_CAMERA_UPDATE, SecuritasDirectDevice, SecuritasHub
from .securitas_direct_new_api import Installation
from .securitas_direct_new_api.dataTypes import CameraDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Securitas Direct camera entities."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    client: SecuritasHub = entry_data["hub"]
    securitas_devices: list[SecuritasDirectDevice] = entry_data["devices"]
    entities: list[Camera] = []

    for device in securitas_devices:
        try:
            cameras = await client.get_camera_devices(device.installation)
        except Exception:
            _LOGGER.warning(
                "Failed to get camera devices for installation %s",
                device.installation.number,
            )
            continue
        for cam_device in cameras:
            entities.append(SecuritasCamera(client, device.installation, cam_device))

    async_add_entities(entities, False)


def _device_info(installation: Installation) -> DeviceInfo:
    """Build DeviceInfo that groups under the installation device."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"securitas_direct.{installation.number}")},
        manufacturer="Securitas Direct",
        model=installation.panel,
        name=installation.alias,
        hw_version=installation.type,
    )


class SecuritasCamera(Camera):
    """A Securitas Direct camera entity showing the last captured image."""

    _attr_should_poll = False

    def __init__(
        self,
        client: SecuritasHub,
        installation: Installation,
        camera_device: CameraDevice,
    ) -> None:
        """Initialize the camera entity."""
        super().__init__()
        self._client = client
        self._installation = installation
        self._camera_device = camera_device
        self._attr_unique_id = f"{installation.number}_camera_{camera_device.zone_id}"
        self._attr_name = f"{installation.alias} {camera_device.name}"
        self._attr_device_info = _device_info(installation)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the last captured image."""
        return self._client.get_camera_image(
            self._installation.number, self._camera_device.zone_id
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to camera update signal."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_CAMERA_UPDATE, self._handle_update
            )
        )

    @callback
    def _handle_update(self, installation_number: str, zone_id: str) -> None:
        """Handle new image availability."""
        if (
            installation_number != self._installation.number
            or zone_id != self._camera_device.zone_id
        ):
            return
        self.async_write_ha_state()
