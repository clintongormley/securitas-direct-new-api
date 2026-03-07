import datetime
from datetime import timedelta
import logging
from typing import Any

import homeassistant.components.lock as lock

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from . import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SecuritasDirectDevice,
    SecuritasHub,
)
from .securitas_direct_new_api import (
    DanalockConfig,
    Installation,
    SecuritasDirectError,
    SmartLockMode,
)
from .securitas_direct_new_api.apimanager import SMARTLOCK_DEVICE_ID

from .securitas_direct_new_api.dataTypes import Service

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=20)

# Service request name that identifies a smart-lock capability
DOORLOCK_SERVICE = "DOORLOCK"

# lockStatus codes returned by the Securitas smart-lock API
LOCK_STATUS_UNKNOWN = "0"
LOCK_STATUS_OPEN = "1"
LOCK_STATUS_LOCKED = "2"
LOCK_STATUS_OPENING = "3"
LOCK_STATUS_LOCKING = "4"

# Delay between API calls during setup to avoid rate limiting

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Securitas Direct lock entities.

    Only fast, non-polling API calls are made here (get_services is cached,
    get_lock_modes is a single query).  Danalock config is fetched lazily
    on the first ``async_update_status`` to avoid blocking startup.
    """
    entry_data = hass.data[DOMAIN][entry.entry_id]
    client: SecuritasHub = entry_data["hub"]
    locks: list[SecuritasLock] = []
    securitas_devices: list[SecuritasDirectDevice] = entry_data["devices"]
    for device in securitas_devices:
        services: list[Service] = await client.get_services(device.installation)
        has_doorlock = any(s.request == DOORLOCK_SERVICE for s in services)
        if not has_doorlock:
            continue

        # Discover all lock devices for this installation (single fast query)
        lock_modes: list[SmartLockMode] = await client.get_lock_modes(
            device.installation
        )

        if not lock_modes:
            # Fallback: create one lock with default device ID
            lock_modes = [
                SmartLockMode(
                    res=None,
                    lockStatus=LOCK_STATUS_UNKNOWN,
                    deviceId=SMARTLOCK_DEVICE_ID,
                )
            ]

        for mode in lock_modes:
            device_id = mode.deviceId or SMARTLOCK_DEVICE_ID
            locks.append(
                SecuritasLock(
                    device.installation,
                    client=client,
                    hass=hass,
                    device_id=device_id,
                    initial_status=mode.lockStatus,
                )
            )

    if not locks:
        _LOGGER.debug("No Securitas Direct %s services found", DOORLOCK_SERVICE)
        return

    async_add_entities(locks, True)


class SecuritasLock(lock.LockEntity):
    def __init__(
        self,
        installation: Installation,
        client: SecuritasHub,
        hass: HomeAssistant,
        device_id: str = SMARTLOCK_DEVICE_ID,
        initial_status: str = LOCK_STATUS_LOCKED,
        danalock_config: DanalockConfig | None = None,
    ) -> None:
        self._state = (
            initial_status
            if initial_status != LOCK_STATUS_UNKNOWN
            else LOCK_STATUS_LOCKED
        )
        self._last_state = self._state
        self._new_state: str = self._state
        self._changed_by: str = ""
        self._device: str = installation.address
        self._device_id: str = device_id
        self._danalock_config: DanalockConfig | None = danalock_config
        self._danalock_config_fetched: bool = danalock_config is not None

        self._attr_unique_id = (
            f"securitas_direct.{installation.number}_lock_{device_id}"
        )

        self.entity_id = f"securitas_direct.{installation.number}_lock_{device_id}"
        self._time: datetime.datetime = datetime.datetime.now()
        self._message: str = ""
        self.installation: Installation = installation
        self.client: SecuritasHub = client
        self.hass: HomeAssistant = hass
        scan_seconds = client.config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        self._update_interval: timedelta = timedelta(seconds=scan_seconds)
        if scan_seconds > 0:
            self._update_unsub = async_track_time_interval(
                hass, self.async_update_status, self._update_interval
            )
        else:
            self._update_unsub = None

        # Group under the installation device (shared with alarm panel)
        self._attr_device_info: DeviceInfo | None = DeviceInfo(
            identifiers={(DOMAIN, f"securitas_direct.{installation.number}")},
            manufacturer="Securitas Direct",
            model=installation.panel,
            name=installation.alias,
            hw_version=installation.type,
        )

    def __force_state(self, state: str) -> None:
        self._last_state = self._state
        self._state = state
        if self.hass is not None:
            self.async_schedule_update_ha_state()

    def _notify_error(self, notification_id, title: str, message: str) -> None:
        """Notify user with persistent notification."""
        self.hass.async_create_task(
            self.hass.services.async_call(
                domain="persistent_notification",
                service="create",
                service_data={
                    "title": title,
                    "message": message,
                    "notification_id": f"{DOMAIN}.{notification_id}",
                },
            )
        )

    @property
    def name(self) -> str:  # type: ignore[override]
        """Return the name of the device."""
        return f"{self.installation.alias} Lock {self._device_id}"

    @property
    def changed_by(self) -> str:  # type: ignore[override]
        """Return the last change triggered by."""
        return self._changed_by

    async def async_will_remove_from_hass(self) -> None:
        """When entity will be removed from Home Assistant."""
        if self._update_unsub:
            self._update_unsub()  # Unsubscribe from updates
            self._update_unsub = None

    async def async_update(self) -> None:
        await self.async_update_status()

    async def async_update_status(self, now=None) -> None:
        if self.hass is None:
            return

        # Lazily fetch Danalock config on first update (avoids blocking setup)
        if not self._danalock_config_fetched:
            self._danalock_config_fetched = True
            try:
                self._danalock_config = await self.client.session.get_danalock_config(
                    self.installation, self._device_id
                )
                cfg = self._danalock_config
                if (
                    cfg
                    and cfg.features
                    and cfg.features.holdBackLatchTime > 0
                ):
                    _LOGGER.info(
                        "Lock %s on %s supports latch hold-back (%ds) — "
                        "open-door feature pending API mutation capture",
                        self._device_id,
                        self.installation.number,
                        cfg.features.holdBackLatchTime,
                    )
            except Exception:
                _LOGGER.debug(
                    "Could not fetch Danalock config for %s device %s",
                    self.installation.number,
                    self._device_id,
                )

        try:
            self._new_state = await self.get_lock_state()
            if self._new_state != LOCK_STATUS_UNKNOWN:
                self._state = self._new_state
        except SecuritasDirectError as err:
            _LOGGER.error(
                "Error updating lock state for %s device %s: %s",
                self.installation.number,
                self._device_id,
                err,
            )

    async def get_lock_state(self) -> str:
        lock_modes: list[SmartLockMode] = await self.client.get_lock_modes(
            self.installation
        )
        for mode in lock_modes:
            if mode.deviceId == self._device_id:
                return mode.lockStatus
        return LOCK_STATUS_UNKNOWN

    @property
    def is_locked(self) -> bool:  # type: ignore[override]
        return self._state == LOCK_STATUS_LOCKED

    @property
    def is_open(self) -> bool:  # type: ignore[override]
        return self._state == LOCK_STATUS_OPEN

    @property
    def is_locking(self) -> bool:  # type: ignore[override]
        return self._state == LOCK_STATUS_LOCKING

    @property
    def is_unlocking(self) -> bool:  # type: ignore[override]
        return False

    @property
    def is_opening(self) -> bool:  # type: ignore[override]
        return self._state == LOCK_STATUS_OPENING

    @property
    def is_jammed(self) -> bool:  # type: ignore[override]
        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:  # type: ignore[override]
        """Return lock configuration as state attributes."""
        attrs: dict[str, Any] = {}
        if self._danalock_config:
            cfg = self._danalock_config
            attrs["battery_low_threshold"] = cfg.batteryLowPercentage
            attrs["lock_before_full_arm"] = cfg.lockBeforeFullArm == "1"
            attrs["lock_before_partial_arm"] = cfg.lockBeforePartialArm == "1"
            attrs["lock_before_perimeter_arm"] = cfg.lockBeforePerimeterArm == "1"
            attrs["unlock_after_disarm"] = cfg.unlockAfterDisarm == "1"
            attrs["auto_lock_time"] = cfg.autoLockTime
            if cfg.features:
                attrs["hold_back_latch_time"] = cfg.features.holdBackLatchTime
                if cfg.features.autolock:
                    attrs["autolock_active"] = cfg.features.autolock.active
                    attrs["autolock_timeout"] = cfg.features.autolock.timeout
        return attrs

    async def async_lock(self, **kwargs):
        self.__force_state(LOCK_STATUS_LOCKING)
        try:
            await self.client.session.change_lock_mode(
                self.installation, True, self._device_id
            )
        except SecuritasDirectError as err:
            _LOGGER.error(
                "Lock operation failed for %s device %s: %s",
                self.installation.number,
                self._device_id,
                err.log_detail(),
            )
            return

        self._state = LOCK_STATUS_LOCKED

    async def async_unlock(self, **kwargs):
        self.__force_state(LOCK_STATUS_OPENING)
        try:
            await self.client.session.change_lock_mode(
                self.installation, False, self._device_id
            )
        except SecuritasDirectError as err:
            _LOGGER.error(
                "Unlock operation failed for %s device %s: %s",
                self.installation.number,
                self._device_id,
                err.log_detail(),
            )
            return

        self._state = LOCK_STATUS_OPEN

    @property
    def supported_features(self) -> lock.LockEntityFeature:  # type: ignore[override]
        """Return the list of supported features."""
        # TODO: Add LockEntityFeature.OPEN when open-door mutation is captured
        return lock.LockEntityFeature(0)
