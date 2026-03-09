"""Shared base entity for Securitas Direct integration."""

from __future__ import annotations

import re

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import DOMAIN
from .securitas_direct_new_api.dataTypes import Installation

TYPE_CHECKING = False
if TYPE_CHECKING:
    from . import SecuritasHub


def securitas_device_info(installation: Installation) -> DeviceInfo:
    """Build DeviceInfo that groups entities under the installation device."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"securitas_direct.{installation.number}")},
        manufacturer="Securitas Direct",
        model=installation.panel,
        name=installation.alias,
        hw_version=installation.type,
    )


class SecuritasEntity(Entity):
    """Base class for Securitas Direct entities."""

    _attr_has_entity_name = False

    def __init__(
        self,
        installation: Installation,
        client: SecuritasHub,
    ) -> None:
        """Initialize common entity attributes."""
        self._installation = installation
        self._client = client
        self._attr_device_info = securitas_device_info(installation)

    @property
    def installation(self) -> Installation:
        """Return the installation."""
        return self._installation

    @property
    def client(self) -> SecuritasHub:
        """Return the client hub."""
        return self._client

    def _notify_error(self, title: str, message: str) -> None:
        """Send persistent notification with auto-generated ID."""
        notification_id = re.sub(r"\W+", "_", title.lower()).strip("_")
        self.hass.async_create_task(
            self.hass.services.async_call(
                domain="persistent_notification",
                service="create",
                service_data={
                    "title": title,
                    "message": message,
                    "notification_id": (
                        f"{DOMAIN}.{notification_id}_{self._installation.number}"
                    ),
                },
            )
        )
