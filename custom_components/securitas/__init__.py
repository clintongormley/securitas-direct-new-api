"""Support for Securitas Direct alarms."""

import asyncio
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
    CheckAlarmStatus,
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
CARD_BASE_URL = "/securitas_panel/securitas-alarm-card.js"
_MANIFEST = json.loads((Path(__file__).parent / "manifest.json").read_text())
CARD_URL = f"{CARD_BASE_URL}?v={_MANIFEST['version']}"

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
DEFAULT_SCAN_INTERVAL_ES = 300
DEFAULT_CODE_ARM_REQUIRED = False
DEFAULT_DELAY_CHECK_OPERATION = 2
DEFAULT_CODE = ""
DEFAULT_COUNTRY = "ES"
API_CACHE_TTL = 60  # seconds — sensor data changes hourly at most

COUNTRY_CODES: list[str] = ["AR", "BR", "CL", "ES", "FR", "GB", "IE", "IT", "PT"]


PLATFORMS = [
    Platform.ALARM_CONTROL_PANEL,
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Establish connection with Securitas Direct."""
    need_sign_in: bool = False

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
    default_scan = (
        DEFAULT_SCAN_INTERVAL_ES
        if config[CONF_COUNTRY] == "ES"
        else DEFAULT_SCAN_INTERVAL
    )
    config[CONF_SCAN_INTERVAL] = _opt(CONF_SCAN_INTERVAL, default_scan)
    config[CONF_DELAY_CHECK_OPERATION] = _opt(
        CONF_DELAY_CHECK_OPERATION, DEFAULT_DELAY_CHECK_OPERATION
    )
    config[CONF_ENTRY_ID] = entry.entry_id
    config[CONF_NOTIFY_GROUP] = _opt(CONF_NOTIFY_GROUP, "")
    config = add_device_information(config)

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

    # Read mapping config (options override data)
    config[CONF_MAP_HOME] = _opt(CONF_MAP_HOME)
    config[CONF_MAP_AWAY] = _opt(CONF_MAP_AWAY)
    config[CONF_MAP_NIGHT] = _opt(CONF_MAP_NIGHT)
    config[CONF_MAP_CUSTOM] = _opt(CONF_MAP_CUSTOM)
    config[CONF_MAP_VACATION] = _opt(CONF_MAP_VACATION)

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
        # --- Shared session with reference counting ---
        # Multiple config entries for the same username share a single
        # SecuritasHub / ApiManager session to avoid duplicate logins
        # and WAF rate-limit blocks.
        # A per-username lock prevents concurrent async_setup_entry calls
        # from creating duplicate hubs (login() yields, so without a lock
        # the second entry wouldn't find the first's session yet).
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
                client = SecuritasHub(
                    config, entry, async_get_clientsession(hass), hass
                )
                try:
                    await client.login()
                except Login2FAError:
                    msg = (
                        "Securitas Direct need a 2FA SMS code."
                        "Please login again with your phone"
                    )
                    _notify_error(hass, "2fa_error", "Securitas Direct", msg)
                    return False
                except LoginError as err:
                    _notify_error(hass, "login_error", "Securitas Direct", str(err))
                    _LOGGER.error(
                        "Could not log in to Securitas: %s",
                        err.log_detail(),
                    )
                    return False
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

        # Share ApiQueue per domain — WAF rate-limits by IP per domain
        domain_url = ApiDomains().get_url(config[CONF_COUNTRY])
        api_queues = hass.data[DOMAIN].setdefault("api_queues", {})
        if domain_url not in api_queues:
            api_queues[domain_url] = ApiQueue(
                foreground_interval=config[CONF_DELAY_CHECK_OPERATION],
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
        client._api_queue = api_queues[domain_url]

        entry.async_on_unload(entry.add_update_listener(async_update_options))

        # Use cached installations from config flow if available,
        # otherwise fetch (e.g. on HA restart).
        try:
            cached = hass.data[DOMAIN].pop("installations", None)
            all_installations: list[Installation] = (
                cached
                if cached is not None
                else await client._api_queue.submit(
                    client.session.list_installations,
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
                    client._services_cache[installation.number] = cached_services[
                        installation.number
                    ]
                elif installation.number not in client._services_cache:
                    # HA restart: fetch directly (bypass queue — we just logged
                    # in, no WAF risk yet) so platforms don't block on queue.
                    client._services_cache[
                        installation.number
                    ] = await client.session.get_all_services(installation)
                devices.append(SecuritasDirectDevice(installation))
        except SecuritasDirectError as err:
            _LOGGER.error("Unable to connect to Securitas Direct: %s", err.log_detail())
            raise ConfigEntryNotReady("Unable to connect to Securitas Direct") from None

        # Store per-entry data
        hass.data[DOMAIN][entry.entry_id] = {
            "hub": client,
            "devices": devices,
        }

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        return True
    else:
        raise ConfigEntryNotReady(
            "Config entry missing device IDs. Delete and re-add the integration."
        )


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
    except Exception:
        _LOGGER.debug(
            "Could not register as Lovelace resource, falling back to add_extra_js_url"
        )
    # Fallback: YAML mode or Lovelace not available
    frontend.add_extra_js_url(hass, CARD_URL)


async def _unregister_card_resource(hass: HomeAssistant) -> None:
    """Remove the alarm card Lovelace resource on unload."""
    resource_id = hass.data.get(DOMAIN, {}).get("card_resource_id")
    if not resource_id:
        # Was using add_extra_js_url fallback or user-managed resource
        try:
            frontend.remove_extra_js_url(hass, CARD_URL)
        except Exception:
            pass
        return
    try:
        lovelace_data = hass.data.get("lovelace")
        if lovelace_data and hasattr(lovelace_data, "resources"):
            resources = lovelace_data.resources
            if hasattr(resources, "async_delete_item"):
                await resources.async_delete_item(resource_id)
    except Exception:
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
        self.overview: CheckAlarmStatus | dict = {}
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
            foreground_interval=domain_config[CONF_DELAY_CHECK_OPERATION],
            background_interval=5.0,
        )
        self._lock_modes: dict[
            str, list
        ] = {}  # installation.number -> SmartLockMode list
        self._lock_modes_time: dict[str, float] = {}  # last fetch time per installation
        self._api_cache: dict[str, Any] = {}  # generic cache: key -> result
        self._api_cache_time: dict[str, float] = {}  # generic cache: key -> timestamp

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
                self.session.arm_request, installation, command, **force_params
            ),
            priority=ApiQueue.FOREGROUND,
        )

        max_attempts = max(10, round(30 / max(1, self.session.delay_check_operation)))
        for attempt in range(1, max_attempts + 1):
            raw = await self._api_queue.submit(
                self.session.check_arm_status_once,
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
            self.session.disarm_request,
            installation,
            command,
            priority=ApiQueue.FOREGROUND,
        )

        max_attempts = max(10, round(30 / max(1, self.session.delay_check_operation)))
        for attempt in range(1, max_attempts + 1):
            raw = await self._api_queue.submit(
                self.session.check_disarm_status_once,
                installation,
                reference_id,
                command,
                attempt,
                priority=ApiQueue.FOREGROUND,
            )
            if raw.get("res") != "WAIT":
                return self.session.process_disarm_result(raw)

        raise TimeoutError("Disarm status poll timed out")

    async def update_overview(self, installation: Installation) -> CheckAlarmStatus:
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
            return CheckAlarmStatus()
        self.xsstatus[installation.number] = status
        async_dispatcher_send(self.hass, SIGNAL_XSSTATUS_UPDATE, installation.number)
        return CheckAlarmStatus(
            status.status or "",
            "",
            status.status or "",
            installation.number,
            status.status or "",
            status.timestampUpdate or "",
        )

    async def change_lock_mode(
        self, installation: Installation, lock_state: bool, device_id: str
    ) -> Any:
        """Change lock mode via queue-submitted API calls."""
        reference_id = await self._api_queue.submit(
            self.session.change_lock_mode_request,
            installation,
            lock_state,
            device_id,
            priority=ApiQueue.FOREGROUND,
        )

        max_attempts = max(10, round(30 / max(1, self.session.delay_check_operation)))
        for attempt in range(1, max_attempts + 1):
            raw = await self._api_queue.submit(
                self.session.check_change_lock_mode_once,
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
    def get_config_entry(self) -> ConfigEntry | None:
        return self.config_entry
