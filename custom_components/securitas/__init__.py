"""Support for Securitas Direct alarms."""

import asyncio
import base64
from collections import OrderedDict
import functools
import json
import logging
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from aiohttp import ClientSession
import voluptuous as vol

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.const import (
    CONF_CODE,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_UNIQUE_ID,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .api_queue import ApiQueue
from .log_filter import SensitiveDataFilter
from .securitas_direct_new_api import (
    ApiDomains,
    ApiManager,
    CameraDevice,
    OperationStatus,
    Installation,
    Login2FAError,
    LoginError,
    OtpPhone,
    SStatus,
    SecuritasDirectError,
    Service,
    generate_device_id,
    generate_uuid,
)

_LOGGER = logging.getLogger(__name__)

DOMAIN = "securitas"
SIGNAL_XSSTATUS_UPDATE = f"{DOMAIN}_xsstatus_update"
SIGNAL_CAMERA_UPDATE = f"{DOMAIN}_camera_update"
CARD_BASE_URL = "/securitas_panel/securitas-alarm-card.js"
_MANIFEST = json.loads((Path(__file__).parent / "manifest.json").read_text())
CARD_URL = f"{CARD_BASE_URL}?v={_MANIFEST['version']}"

CONF_ADVANCED = "advanced"
CONF_COUNTRY = "country"
CONF_CODE_ARM_REQUIRED = "code_arm_required"
CONF_HAS_PERI = "has_peri"
CONF_DEVICE_INDIGITALL = "idDeviceIndigitall"
CONF_ENTRY_ID = "entry_id"
CONF_INSTALLATION_KEY = "instalation"
CONF_DELAY_CHECK_OPERATION = "delay_check_operation"
CONF_MAP_HOME = "map_home"
CONF_MAP_AWAY = "map_away"
CONF_MAP_NIGHT = "map_night"
CONF_MAP_CUSTOM = "map_custom"
CONF_MAP_VACATION = "map_vacation"
CONF_NOTIFY_GROUP = "notify_group"
CONF_INSTALLATION = "installation"

DEFAULT_SCAN_INTERVAL = 120
DEFAULT_CODE_ARM_REQUIRED = False
DEFAULT_DELAY_CHECK_OPERATION = 2
DEFAULT_CODE = ""
DEFAULT_COUNTRY = "ES"
API_CACHE_TTL = 60  # seconds — sensor data changes hourly at most

COUNTRY_CODES: list[str] = ["AR", "BR", "CL", "ES", "FR", "GB", "IE", "IT", "PT"]


PLATFORMS = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.SENSOR,
    Platform.LOCK,
]
HUB = None


CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_COUNTRY, default=DEFAULT_COUNTRY): str,
                vol.Optional(CONF_CODE, default=DEFAULT_CODE): str,
                vol.Optional(
                    CONF_CODE_ARM_REQUIRED, default=DEFAULT_CODE_ARM_REQUIRED
                ): bool,
                vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): int,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def add_device_information(config: dict) -> dict:
    """Add device information to the configuration."""
    if CONF_DEVICE_ID not in config:
        config[CONF_DEVICE_ID] = generate_device_id(config[CONF_COUNTRY])

    if CONF_UNIQUE_ID not in config:
        config[CONF_UNIQUE_ID] = generate_uuid()

    if CONF_DEVICE_INDIGITALL not in config:
        config[CONF_DEVICE_INDIGITALL] = str(uuid4())

    return config


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    if any(
        entry.data.get(attrib) != entry.options.get(attrib)
        for attrib in (
            CONF_CODE,
            CONF_CODE_ARM_REQUIRED,
            CONF_SCAN_INTERVAL,
            CONF_MAP_HOME,
            CONF_MAP_AWAY,
            CONF_MAP_NIGHT,
            CONF_MAP_CUSTOM,
            CONF_MAP_VACATION,
            CONF_NOTIFY_GROUP,
        )
    ):
        # update entry replacing data with new options
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, **entry.options}
        )
        await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Reject old config entries — users must delete and re-add."""
    if config_entry.version < 3:
        _LOGGER.error(
            "Config entry %s uses format v%s which is no longer supported. "
            "Please remove this integration entry and re-add it.",
            config_entry.entry_id,
            config_entry.version,
        )
        _notify_error(
            hass,
            "migration_required",
            "Securitas Direct",
            "Your Securitas Direct configuration uses an old format. "
            "Please remove the integration entry and re-add it.",
        )
        return False
    return True


def _build_config_dict(entry: ConfigEntry) -> tuple[dict, bool]:
    """Build config dict from entry.data + entry.options.

    Returns the config dict and a flag indicating whether sign-in is needed
    (True if any device ID fields are missing from entry.data).
    """

    def _opt(key, default=None):
        """Read from options first, then data, then default."""
        return entry.options.get(key, entry.data.get(key, default))

    config = OrderedDict()
    config[CONF_USERNAME] = entry.data[CONF_USERNAME]
    config[CONF_PASSWORD] = entry.data[CONF_PASSWORD]
    config[CONF_COUNTRY] = entry.data.get(CONF_COUNTRY, None)
    config[CONF_CODE] = _opt(CONF_CODE, DEFAULT_CODE)
    config[CONF_HAS_PERI] = entry.data.get(CONF_HAS_PERI, False)
    config[CONF_CODE_ARM_REQUIRED] = _opt(
        CONF_CODE_ARM_REQUIRED, DEFAULT_CODE_ARM_REQUIRED
    )
    config[CONF_SCAN_INTERVAL] = _opt(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    config[CONF_DELAY_CHECK_OPERATION] = _opt(
        CONF_DELAY_CHECK_OPERATION, DEFAULT_DELAY_CHECK_OPERATION
    )
    config[CONF_ENTRY_ID] = entry.entry_id
    config[CONF_NOTIFY_GROUP] = _opt(CONF_NOTIFY_GROUP, "")
    config = add_device_information(config)

    # Read mapping config (options override data)
    config[CONF_MAP_HOME] = _opt(CONF_MAP_HOME)
    config[CONF_MAP_AWAY] = _opt(CONF_MAP_AWAY)
    config[CONF_MAP_NIGHT] = _opt(CONF_MAP_NIGHT)
    config[CONF_MAP_CUSTOM] = _opt(CONF_MAP_CUSTOM)
    config[CONF_MAP_VACATION] = _opt(CONF_MAP_VACATION)

    need_sign_in = False
    if CONF_DEVICE_ID in entry.data:
        config[CONF_DEVICE_ID] = entry.data[CONF_DEVICE_ID]
    else:
        need_sign_in = True
    if CONF_UNIQUE_ID in entry.data:
        config[CONF_UNIQUE_ID] = entry.data[CONF_UNIQUE_ID]
    else:
        need_sign_in = True
    if CONF_DEVICE_INDIGITALL in entry.data:
        config[CONF_DEVICE_INDIGITALL] = entry.data[CONF_DEVICE_INDIGITALL]
    else:
        need_sign_in = True

    return config, need_sign_in


async def _get_or_create_session(
    hass: HomeAssistant, config: dict, entry: ConfigEntry
) -> "SecuritasHub":
    """Get or create a shared SecuritasHub session with reference counting.

    Multiple config entries for the same username share a single
    SecuritasHub / ApiManager session to avoid duplicate logins
    and WAF rate-limit blocks.  A per-username lock prevents concurrent
    async_setup_entry calls from creating duplicate hubs.
    """
    username = config[CONF_USERNAME]
    sessions = hass.data[DOMAIN].setdefault("sessions", {})
    setup_locks = hass.data[DOMAIN].setdefault("setup_locks", {})
    if username not in setup_locks:
        setup_locks[username] = asyncio.Lock()

    async with setup_locks[username]:
        if username in sessions:
            # Reuse existing session
            client: SecuritasHub = sessions[username]["hub"]
            sessions[username]["ref_count"] += 1
        else:
            # Create new session and log in
            client = SecuritasHub(config, entry, async_get_clientsession(hass), hass)
            try:
                await client.login()
            except Login2FAError:
                msg = (
                    "Securitas Direct need a 2FA SMS code."
                    "Please login again with your phone"
                )
                _notify_error(hass, "2fa_error", "Securitas Direct", msg)
                raise
            except LoginError as err:
                _notify_error(hass, "login_error", "Securitas Direct", str(err))
                _LOGGER.error(
                    "Could not log in to Securitas: %s",
                    err.log_detail(),
                )
                raise
            except SecuritasDirectError as err:
                detail = err.log_detail()
                _LOGGER.error(
                    "Unable to connect to Securitas Direct: %s",
                    detail,
                )
                raise ConfigEntryNotReady(
                    f"Unable to connect to Securitas Direct: {detail}"
                ) from None
            sessions[username] = {"hub": client, "ref_count": 1}

    return client


def _get_or_create_api_queue(
    hass: HomeAssistant,
    session: "SecuritasHub",
    config: dict,
    entry: ConfigEntry,
) -> None:
    """Create or reuse an ApiQueue for the session's API domain.

    WAF rate-limits by IP per domain, so entries sharing a domain share a queue.
    Sets session.api_queue as a side effect.
    """
    domain_url = ApiDomains().get_url(config[CONF_COUNTRY])
    api_queues = hass.data[DOMAIN].setdefault("api_queues", {})
    if domain_url not in api_queues:
        api_queues[domain_url] = ApiQueue(
            interval=config[CONF_DELAY_CHECK_OPERATION],
        )
        _LOGGER.info(
            "Created ApiQueue %s for domain %s (country=%s, entry=%s)",
            id(api_queues[domain_url]),
            domain_url,
            config[CONF_COUNTRY],
            entry.entry_id,
        )
    else:
        _LOGGER.info(
            "Reusing ApiQueue %s for domain %s (country=%s, entry=%s)",
            id(api_queues[domain_url]),
            domain_url,
            config[CONF_COUNTRY],
            entry.entry_id,
        )
    session.api_queue = api_queues[domain_url]


async def _fetch_and_cache_installations(
    hass: HomeAssistant,
    hub: "SecuritasHub",
    entry: ConfigEntry,
) -> list["SecuritasDirectDevice"]:
    """Fetch installations and services, populating caches.

    Uses cached data from the config flow when available, otherwise
    fetches from the API (e.g. on HA restart).

    Returns a list of SecuritasDirectDevice wrappers for this entry's
    installations.
    """
    cached = hass.data[DOMAIN].pop("installations", None)
    all_installations: list[Installation] = (
        cached
        if cached is not None
        else await hub.api_queue.submit(
            hub.session.list_installations,
            priority=ApiQueue.FOREGROUND,
        )
    )
    target_number = entry.data.get(CONF_INSTALLATION)
    if target_number:
        entry_installations = [
            inst for inst in all_installations if inst.number == target_number
        ]
    else:
        # Legacy entries without CONF_INSTALLATION get all
        entry_installations = all_installations

    # Use cached services from config flow if available,
    # otherwise fetch from API (e.g. on HA restart).
    cached_services = hass.data[DOMAIN].pop("cached_services", None)

    devices: list[SecuritasDirectDevice] = []
    for installation in entry_installations:
        if cached_services and installation.number in cached_services:
            # Pre-populate from config flow cache
            hub.services_cache[installation.number] = cached_services[
                installation.number
            ]
        elif installation.number not in hub.services_cache:
            # HA restart: fetch directly (bypass queue — we just logged
            # in, no WAF risk yet) so platforms don't block on queue.
            hub.services_cache[
                installation.number
            ] = await hub.session.get_all_services(installation)
        devices.append(SecuritasDirectDevice(installation))
    return devices


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Establish connection with Securitas Direct."""
    config, need_sign_in = _build_config_dict(entry)

    # Register card static path + Lovelace resource early so the card
    # is available even when login fails (ConfigEntryNotReady).
    if hass.http and not hass.data.get(DOMAIN, {}).get("card_registered"):
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    "/securitas_panel",
                    str(Path(__file__).parent / "www"),
                    cache_headers=False,
                )
            ]
        )
        await _register_card_resource(hass)
        hass.data.setdefault(DOMAIN, {})["card_registered"] = True

    hass.data.setdefault(DOMAIN, {})

    # Set up log sanitization filter — must be on handlers, not the logger,
    # because logger-level filters don't apply to child logger records.
    if "log_filter" not in hass.data[DOMAIN]:
        log_filter = SensitiveDataFilter()
        for handler in logging.getLogger().handlers:
            handler.addFilter(log_filter)
        hass.data[DOMAIN]["log_filter"] = log_filter
    else:
        log_filter = hass.data[DOMAIN]["log_filter"]

    # Register credentials immediately
    log_filter.update_secret("username", config[CONF_USERNAME])
    log_filter.update_secret("password", config[CONF_PASSWORD])

    hass.data[DOMAIN][CONF_ENTRY_ID] = entry.entry_id
    if not need_sign_in:
        try:
            client = await _get_or_create_session(hass, config, entry)
        except Login2FAError:
            return False
        except LoginError:
            return False

        _get_or_create_api_queue(hass, client, config, entry)

        entry.async_on_unload(entry.add_update_listener(async_update_options))

        try:
            devices = await _fetch_and_cache_installations(hass, client, entry)
        except SecuritasDirectError as err:
            _LOGGER.error("Unable to connect to Securitas Direct: %s", err.log_detail())
            raise ConfigEntryNotReady("Unable to connect to Securitas Direct") from None

        # Store per-entry data
        hass.data[DOMAIN][entry.entry_id] = {
            "hub": client,
            "devices": devices,
        }

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        # Discover cameras and locks in the background after setup completes.
        # This avoids blocking startup with API calls.
        entry.async_create_background_task(
            hass,
            _async_discover_devices(hass, entry),
            f"securitas_discover_{entry.entry_id}",
        )
        return True
    raise ConfigEntryNotReady(
        "Config entry missing device IDs. Delete and re-add the integration."
    )


async def _discover_cameras(
    hub: "SecuritasHub",
    installation: Installation,
    entry_data: dict,
) -> None:
    """Discover camera devices for an installation and add entities."""
    from .button import SecuritasCaptureButton
    from .camera import SecuritasCamera

    try:
        cameras = await hub.get_camera_devices(installation)
    except Exception:  # pylint: disable=broad-exception-caught  # background discovery must not crash
        _LOGGER.warning("Failed to get camera devices for %s", installation.number)
        cameras = []

    if cameras:
        camera_add = entry_data.get("camera_add_entities")
        button_add = entry_data.get("button_add_entities")
        if camera_add:
            camera_add(
                [SecuritasCamera(hub, installation, cam) for cam in cameras],
                False,
            )
        if button_add:
            button_add(
                [SecuritasCaptureButton(hub, installation, cam) for cam in cameras],
                True,
            )


async def _discover_locks(
    hass: HomeAssistant,
    hub: "SecuritasHub",
    installation: Installation,
    entry_data: dict,
) -> None:
    """Discover lock devices for an installation and add entities."""
    from .lock import (
        DOORLOCK_SERVICE,
        LOCK_STATUS_UNKNOWN,
        SecuritasLock,
    )
    from .securitas_direct_new_api import SmartLockMode
    from .securitas_direct_new_api.apimanager import SMARTLOCK_DEVICE_ID

    try:
        services = await hub.get_services(installation)
    except Exception:  # pylint: disable=broad-exception-caught  # background discovery must not crash
        _LOGGER.warning("Failed to get services for %s", installation.number)
        return

    has_doorlock = any(s.request == DOORLOCK_SERVICE for s in services)
    if not has_doorlock:
        return

    try:
        lock_modes: list[SmartLockMode] = await hub.get_lock_modes(installation)
    except Exception:  # pylint: disable=broad-exception-caught  # background discovery must not crash
        _LOGGER.warning("Failed to get lock modes for %s", installation.number)
        lock_modes = []

    if not lock_modes:
        lock_modes = [
            SmartLockMode(
                res=None,
                lockStatus=LOCK_STATUS_UNKNOWN,
                deviceId=SMARTLOCK_DEVICE_ID,
            )
        ]

    lock_add = entry_data.get("lock_add_entities")
    if lock_add:
        locks = [
            SecuritasLock(
                installation,
                client=hub,
                hass=hass,
                device_id=mode.deviceId or SMARTLOCK_DEVICE_ID,
                initial_status=mode.lockStatus,
            )
            for mode in lock_modes
        ]
        lock_add(locks, False)
        # Schedule initial lock update
        for lock_entity in locks:
            lock_entity.async_schedule_update_ha_state(force_refresh=True)


async def _async_discover_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Discover cameras and locks in the background after setup."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if entry_data is None:
        return

    client: SecuritasHub = entry_data["hub"]
    devices: list[SecuritasDirectDevice] = entry_data["devices"]

    for device in devices:
        installation = device.installation
        await _discover_cameras(client, installation, entry_data)
        await _discover_locks(hass, client, installation, entry_data)


async def _register_card_resource(hass: HomeAssistant) -> None:
    """Register the alarm card as a Lovelace resource for proper load ordering.

    Using add_extra_js_url injects the script asynchronously, causing a race
    condition on cold start where Lovelace renders cards before the custom
    element is registered. Registering as a Lovelace resource gives Lovelace
    explicit load ordering and avoids the "Configuration error" on first load.
    Falls back to add_extra_js_url if Lovelace resources are unavailable.
    """
    try:
        lovelace_data = hass.data.get("lovelace")
        if lovelace_data and hasattr(lovelace_data, "resources"):
            resources = lovelace_data.resources
            if hasattr(resources, "async_create_item"):
                # Storage mode — can add programmatically
                if not resources.loaded:
                    await resources.async_load()
                    resources.loaded = True
                # Update or skip if already registered
                for item in resources.async_items():
                    url = item.get("url", "")
                    if url == CARD_URL:
                        return  # Already current version
                    if url.startswith(CARD_BASE_URL):
                        # Old version — update the URL
                        await resources.async_update_item(item["id"], {"url": CARD_URL})
                        hass.data.setdefault(DOMAIN, {})["card_resource_id"] = item[
                            "id"
                        ]
                        return
                item = await resources.async_create_item(
                    {"res_type": "module", "url": CARD_URL}
                )
                hass.data.setdefault(DOMAIN, {})["card_resource_id"] = item["id"]
                return
    except Exception:  # pylint: disable=broad-exception-caught  # HA internals may raise anything
        _LOGGER.debug(
            "Could not register as Lovelace resource, falling back to add_extra_js_url"
        )
    # Fallback: YAML mode or Lovelace not available
    try:
        frontend.add_extra_js_url(hass, CARD_URL)
    except (KeyError, Exception):  # pylint: disable=broad-exception-caught
        _LOGGER.debug("Could not register card via add_extra_js_url")


async def _unregister_card_resource(hass: HomeAssistant) -> None:
    """Remove the alarm card Lovelace resource on unload."""
    resource_id = hass.data.get(DOMAIN, {}).get("card_resource_id")
    if not resource_id:
        # Was using add_extra_js_url fallback or user-managed resource
        try:
            frontend.remove_extra_js_url(hass, CARD_URL)
        except Exception:  # pylint: disable=broad-exception-caught  # HA internals may raise anything
            pass
        return
    try:
        lovelace_data = hass.data.get("lovelace")
        if lovelace_data and hasattr(lovelace_data, "resources"):
            resources = lovelace_data.resources
            if hasattr(resources, "async_delete_item"):
                await resources.async_delete_item(resource_id)
    except Exception:  # pylint: disable=broad-exception-caught  # HA internals may raise anything
        _LOGGER.debug("Could not remove Lovelace resource %s", resource_id)


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    )

    # Decrement shared session ref count
    username = config_entry.data.get(CONF_USERNAME)
    sessions = hass.data.get(DOMAIN, {}).get("sessions", {})
    if username and username in sessions:
        sessions[username]["ref_count"] -= 1
        if sessions[username]["ref_count"] <= 0:
            sessions.pop(username)

    # Clean up per-entry data
    hass.data[DOMAIN].pop(config_entry.entry_id, None)

    # Check if any sessions remain — if not, do full cleanup
    remaining_sessions = hass.data.get(DOMAIN, {}).get("sessions", {})
    if not remaining_sessions:
        # Last entry unloaded — full cleanup
        log_filter = hass.data[DOMAIN].get("log_filter")
        if log_filter:
            for handler in logging.getLogger().handlers:
                handler.removeFilter(log_filter)

        await _unregister_card_resource(hass)
        hass.data.pop(DOMAIN, None)

    return unload_ok


def _notify_error(
    hass: HomeAssistant, notification_id, title: str, message: str
) -> None:
    """Notify user with persistent notification."""
    hass.async_create_task(
        hass.services.async_call(
            domain="persistent_notification",
            service="create",
            service_data={
                "title": title,
                "message": message,
                "notification_id": f"{DOMAIN}.{notification_id}",
            },
        )
    )


class SecuritasDirectDevice:
    """Securitas direct device instance."""

    def __init__(self, installation: Installation) -> None:
        """Construct a device wrapper."""
        self.installation = installation
        self.name = installation.alias
        self._available = True

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return True

    @property
    def device_id(self) -> str:
        """Return device ID."""
        return self.installation.number

    @property
    def address(self) -> str:
        """Return the address of the instalation."""
        return self.installation.address

    @property
    def city(self) -> str:
        """Return the city of the instalation."""
        return self.installation.city

    @property
    def postal_code(self) -> str:
        """Return the postalCode of the instalation."""
        return self.installation.postalCode

    @property
    def device_info(self) -> DeviceInfo:
        """Return a device description for device registry."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.installation.alias}")},
            manufacturer="Securitas Direct",
            model=self.installation.type,
            hw_version=self.installation.panel,
            name=self.name,
        )


class SecuritasHub:
    """A Securitas hub wrapper class."""

    def __init__(
        self,
        domain_config: dict,
        config_entry: ConfigEntry | None,
        http_client: ClientSession,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the Securitas hub."""
        self.overview: OperationStatus | dict = {}
        self.xsstatus: dict[str, SStatus] = {}
        self.config = domain_config
        self.config_entry: ConfigEntry | None = config_entry
        self.sentinel_services: list[Service] = []
        self.country: str = domain_config[CONF_COUNTRY].upper()
        self.lang: str = ApiDomains().get_language(self.country)
        self.hass: HomeAssistant = hass
        self.services: dict[int, list[Service]] = {1: []}
        self._services_cache: dict[str, list[Service]] = {}
        self.log_filter: SensitiveDataFilter | None = hass.data.get(DOMAIN, {}).get(
            "log_filter"
        )
        self.session: ApiManager = ApiManager(
            domain_config[CONF_USERNAME],
            domain_config[CONF_PASSWORD],
            self.country,
            http_client,
            domain_config[CONF_DEVICE_ID],
            domain_config[CONF_UNIQUE_ID],
            domain_config[CONF_DEVICE_INDIGITALL],
            domain_config[CONF_DELAY_CHECK_OPERATION],
            log_filter=self.log_filter,
        )
        self.installations: list[Installation] = []
        self._api_queue = ApiQueue(
            interval=domain_config[CONF_DELAY_CHECK_OPERATION],
        )
        self._lock_modes: dict[
            str, list
        ] = {}  # installation.number -> SmartLockMode list
        self._lock_modes_time: dict[str, float] = {}  # last fetch time per installation
        self._api_cache: dict[str, Any] = {}  # generic cache: key -> result
        self._api_cache_time: dict[str, float] = {}  # generic cache: key -> timestamp
        self.camera_images: dict[str, bytes] = {}
        self.camera_timestamps: dict[str, str] = {}
        self._camera_devices_cache: dict[str, list[CameraDevice]] = {}

    async def login(self):
        """Login to Securitas."""
        await self.session.login()

    async def validate_device(self) -> tuple[str | None, list[OtpPhone] | None]:
        """Validate the current device."""
        return await self.session.validate_device(False, "", "")

    async def send_sms_code(
        self, auth_otp_hash: str, sms_code: str
    ) -> tuple[str | None, list[OtpPhone] | None]:
        """Send the SMS."""
        return await self.session.validate_device(True, auth_otp_hash, sms_code)

    async def refresh_token(self) -> bool:
        """Refresh the token."""
        return await self.session.refresh_token()

    async def send_opt(self, challange: str, phone_index: int):
        """Call for the SMS challange."""
        return await self.session.send_otp(phone_index, challange)

    async def get_services(
        self, instalation: Installation, priority=None
    ) -> list[Service]:
        """Get the list of services from the installation (cached)."""
        if priority is None:
            priority = ApiQueue.BACKGROUND
        key = instalation.number
        if key in self._services_cache:
            return self._services_cache[key]
        services = await self._api_queue.submit(
            self.session.get_all_services,
            instalation,
            priority=priority,
        )
        self._services_cache[key] = services
        return services

    async def get_camera_devices(
        self, installation: Installation
    ) -> list[CameraDevice]:
        """Get camera devices for an installation (cached)."""
        key = installation.number
        if key in self._camera_devices_cache:
            return self._camera_devices_cache[key]
        devices = await self._api_queue.submit(
            self.session.get_device_list,
            installation,
            priority=ApiQueue.BACKGROUND,
        )
        self._camera_devices_cache[key] = devices
        return devices

    async def capture_image(
        self, installation: Installation, camera_device: CameraDevice
    ) -> bytes | None:
        """Request a new image capture and fetch the result."""
        device = camera_device

        # Get the baseline thumbnail idSignal so we can detect when it changes
        baseline = await self._api_queue.submit(
            self.session.get_thumbnail,
            installation,
            device.name,
            device.zone_id,
            priority=ApiQueue.FOREGROUND,
        )
        baseline_id = baseline.id_signal

        reference_id = await self._api_queue.submit(
            self.session.request_images,
            installation,
            device.code,
            priority=ApiQueue.FOREGROUND,
        )

        # Poll for completion — continue while "processing" (image not yet ready)
        max_attempts = self._max_poll_attempts(timeout_seconds=60)
        try:
            for attempt in range(1, max_attempts + 1):
                raw = await self._api_queue.submit(
                    self.session.check_request_images_status,
                    installation,
                    device.code,
                    reference_id,
                    attempt,
                    priority=ApiQueue.FOREGROUND,
                )
                msg = raw.get("msg", "")
                if "processing" not in msg and raw.get("res") != "WAIT":
                    break
            else:
                raise TimeoutError("Image request poll timed out")
        except (TimeoutError, SecuritasDirectError):
            _LOGGER.warning(
                "Image request polling timed out for %s, fetching thumbnail anyway",
                device.name,
            )

        # Poll the thumbnail until idSignal changes (CDN propagation delay)
        thumbnail = None
        for attempt in range(max_attempts):
            if attempt > 0:
                await asyncio.sleep(max(5, self.session.delay_check_operation))
            thumbnail = await self._api_queue.submit(
                self.session.get_thumbnail,
                installation,
                device.name,
                device.zone_id,
                priority=ApiQueue.FOREGROUND,
            )
            if thumbnail.id_signal != baseline_id:
                break
        else:
            _LOGGER.warning(
                "Thumbnail idSignal did not change for %s after capture",
                device.name,
            )

        image_bytes = self._validate_and_store_image(
            thumbnail, installation, device, log_warnings=True
        )

        if image_bytes is not None:
            async_dispatcher_send(
                self.hass, SIGNAL_CAMERA_UPDATE, installation.number, device.zone_id
            )
        return image_bytes

    async def fetch_latest_thumbnail(
        self, installation: Installation, camera_device: CameraDevice
    ) -> None:
        """Fetch the current thumbnail from the API and store it."""
        try:
            thumbnail = await self._api_queue.submit(
                self.session.get_thumbnail,
                installation,
                camera_device.name,
                camera_device.zone_id,
                priority=ApiQueue.BACKGROUND,
            )
        except Exception:  # pylint: disable=broad-exception-caught  # API call may raise anything
            _LOGGER.debug(
                "Could not fetch thumbnail for %s on startup",
                camera_device.name,
            )
            return

        image_bytes = self._validate_and_store_image(
            thumbnail, installation, camera_device, log_warnings=False
        )
        if image_bytes is not None:
            async_dispatcher_send(
                self.hass,
                SIGNAL_CAMERA_UPDATE,
                installation.number,
                camera_device.zone_id,
            )

    def _validate_and_store_image(
        self,
        thumbnail,
        installation: Installation,
        camera_device,
        *,
        log_warnings: bool = True,
    ) -> bytes | None:
        """Decode, validate JPEG, and cache a thumbnail image."""
        if thumbnail is None or thumbnail.image is None:
            return None
        image_bytes = base64.b64decode(thumbnail.image)
        if not image_bytes.startswith(b"\xff\xd8"):
            if log_warnings:
                _LOGGER.warning(
                    "Thumbnail for %s is not JPEG data (got %d bytes starting with %r)",
                    camera_device.name,
                    len(image_bytes),
                    image_bytes[:40],
                )
            return None
        key = f"{installation.number}_{camera_device.zone_id}"
        self.camera_images[key] = image_bytes
        if thumbnail.timestamp:
            self.camera_timestamps[key] = thumbnail.timestamp
        return image_bytes

    def get_camera_image(self, installation_number: str, zone_id: str) -> bytes | None:
        """Return the last captured image for a camera."""
        return self.camera_images.get(f"{installation_number}_{zone_id}")

    def get_camera_timestamp(
        self, installation_number: str, zone_id: str
    ) -> str | None:
        """Return the timestamp of the last captured image."""
        return self.camera_timestamps.get(f"{installation_number}_{zone_id}")

    def _max_poll_attempts(self, timeout_seconds: int = 30) -> int:
        """Calculate max polling attempts for a given timeout."""
        return max(
            10, round(timeout_seconds / max(1, self.session.delay_check_operation))
        )

    def get_authentication_token(self) -> str | None:
        """Get the authentication token."""
        return self.session.authentication_token

    def set_authentication_token(self, value: str):
        """Set the authentication token."""
        self.session.authentication_token = value

    async def logout(self):
        """Logout from Securitas."""
        ret = await self.session.logout()
        if not ret:
            _LOGGER.error("Could not log out from Securitas: %s", ret)
            return False
        return True

    async def get_lock_modes(self, installation: Installation) -> list:
        """Get lock modes with caching, submitted via queue."""
        from .securitas_direct_new_api import SmartLockMode

        _CACHE_TTL = API_CACHE_TTL
        now = time.monotonic()
        cached_time = self._lock_modes_time.get(installation.number, 0)
        if now - cached_time < _CACHE_TTL and installation.number in self._lock_modes:
            return self._lock_modes[installation.number]

        try:
            modes: list[SmartLockMode] = await self._api_queue.submit(
                self.session.get_lock_current_mode,
                installation,
                priority=ApiQueue.BACKGROUND,
            )
        except SecuritasDirectError as err:
            _LOGGER.warning(
                "Error fetching lock modes for %s: %s",
                installation.number,
                err.log_detail(),
            )
            modes = []

        self._lock_modes[installation.number] = modes
        self._lock_modes_time[installation.number] = time.monotonic()
        return modes

    async def _cached_api_call(self, cache_key: str, coro_fn, *args, priority=None):
        """Execute an API call with caching, submitted via queue.

        The cache is checked twice: once before queuing (fast path) and once
        inside the queue-submitted wrapper (after serialization).  This
        prevents duplicate API calls when multiple entities concurrently
        request the same cached data — they all miss the cache, queue up,
        but only the first actually calls the API; the rest see the freshly
        populated cache.
        """
        if priority is None:
            priority = ApiQueue.BACKGROUND
        _CACHE_TTL = API_CACHE_TTL
        now = time.monotonic()
        if (
            now - self._api_cache_time.get(cache_key, 0) < _CACHE_TTL
            and cache_key in self._api_cache
        ):
            return self._api_cache[cache_key]

        _sentinel = object()

        async def _call_with_cache_recheck(*call_args):
            # Re-check cache after queue serialization — another caller
            # may have populated it while we were waiting.
            now_inner = time.monotonic()
            if (
                now_inner - self._api_cache_time.get(cache_key, 0) < _CACHE_TTL
                and cache_key in self._api_cache
            ):
                return _sentinel  # signal: used cache, no API call made
            return await coro_fn(*call_args)

        result = await self._api_queue.submit(
            _call_with_cache_recheck, *args, priority=priority
        )

        if result is _sentinel:
            return self._api_cache[cache_key]

        if result is not None:
            self._api_cache[cache_key] = result
            self._api_cache_time[cache_key] = time.monotonic()
        return result

    async def get_sentinel(self, installation: Installation, service: Service) -> Any:
        """Get sentinel data with rate-limit serialization and caching."""
        cache_key = f"sentinel_{installation.number}_{service.id}"
        return await self._cached_api_call(
            cache_key,
            self.session.get_sentinel_data,
            installation,
            service,
        )

    async def get_air_quality(self, installation: Installation, zone: str) -> Any:
        """Get air quality data with rate-limit serialization and caching."""
        cache_key = f"air_quality_{installation.number}_{zone}"
        return await self._cached_api_call(
            cache_key,
            self.session.get_air_quality_data,
            installation,
            zone,
        )

    async def arm_alarm(
        self, installation: Installation, command: str, **force_params: str
    ) -> Any:
        """Arm the alarm via queue-submitted API calls."""
        reference_id = await self._api_queue.submit(
            functools.partial(
                self.session.submit_arm_request, installation, command, **force_params
            ),
            priority=ApiQueue.FOREGROUND,
        )

        max_attempts = self._max_poll_attempts(timeout_seconds=30)
        for attempt in range(1, max_attempts + 1):
            raw = await self._api_queue.submit(
                self.session.check_arm_status,
                installation,
                reference_id,
                command,
                attempt,
                priority=ApiQueue.FOREGROUND,
            )
            if raw.get("res") != "WAIT":
                return await self.session.process_arm_result(raw, installation)

        raise TimeoutError("Arm status poll timed out")

    async def disarm_alarm(self, installation: Installation, command: str) -> Any:
        """Disarm the alarm via queue-submitted API calls."""
        reference_id = await self._api_queue.submit(
            self.session.submit_disarm_request,
            installation,
            command,
            priority=ApiQueue.FOREGROUND,
        )

        max_attempts = self._max_poll_attempts(timeout_seconds=30)
        for attempt in range(1, max_attempts + 1):
            raw = await self._api_queue.submit(
                self.session.check_disarm_status,
                installation,
                reference_id,
                command,
                attempt,
                priority=ApiQueue.FOREGROUND,
            )
            if raw.get("res") != "WAIT":
                return self.session.process_disarm_result(raw)

        raise TimeoutError("Disarm status poll timed out")

    async def update_overview(self, installation: Installation) -> OperationStatus:
        """Poll alarm status via check_general_status (single API call).

        Periodic polling always uses xSStatus for efficiency.  The more
        expensive CheckAlarm path (protom round-trip) is used only for
        arm/disarm operations and the manual refresh button.
        """
        try:
            status = await self._api_queue.submit(
                self.session.check_general_status,
                installation,
                priority=ApiQueue.BACKGROUND,
            )
        except SecuritasDirectError as err:
            _LOGGER.warning(
                "Error checking general status for %s: %s",
                installation.number,
                err.log_detail(),
            )
            if getattr(err, "http_status", None) == 403:
                raise
            return OperationStatus()
        self.xsstatus[installation.number] = status
        async_dispatcher_send(self.hass, SIGNAL_XSSTATUS_UPDATE, installation.number)
        return OperationStatus(
            operation_status=status.status or "",
            message="",
            status=status.status or "",
            installation_number=installation.number,
            protomResponse=status.status or "",
            protomResponseData=status.timestampUpdate or "",
        )

    async def change_lock_mode(
        self, installation: Installation, lock_state: bool, device_id: str
    ) -> Any:
        """Change lock mode via queue-submitted API calls."""
        reference_id = await self._api_queue.submit(
            self.session.submit_change_lock_mode_request,
            installation,
            lock_state,
            device_id,
            priority=ApiQueue.FOREGROUND,
        )

        max_attempts = self._max_poll_attempts(timeout_seconds=30)
        for attempt in range(1, max_attempts + 1):
            raw = await self._api_queue.submit(
                self.session.check_change_lock_mode,
                installation,
                reference_id,
                attempt,
                device_id,
                priority=ApiQueue.FOREGROUND,
            )
            if raw.get("res") != "WAIT":
                return self.session.process_lock_mode_result(raw)

        raise TimeoutError("Lock mode change timed out")

    async def get_danalock_config(
        self, installation: Installation, device_id: str
    ) -> Any:
        """Fetch danalock config via queue-submitted API calls."""
        return await self._api_queue.submit(
            self.session.get_danalock_config,
            installation,
            device_id,
            priority=ApiQueue.FOREGROUND,
        )

    @property
    def api_queue(self) -> ApiQueue:
        """Return the API queue."""
        return self._api_queue

    @api_queue.setter
    def api_queue(self, value: ApiQueue) -> None:
        """Set the API queue."""
        self._api_queue = value

    @property
    def services_cache(self) -> dict[str, list[Service]]:
        """Return the services cache."""
        return self._services_cache

    @property
    def get_config_entry(self) -> ConfigEntry | None:
        """Return the config entry."""
        return self.config_entry
