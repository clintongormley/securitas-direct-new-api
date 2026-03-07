"""Config flow for the Securitas Direct platform."""

from __future__ import annotations

from collections import OrderedDict
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import (
    CONF_CODE,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    CONF_UNIQUE_ID,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import selector

from . import (
    CONF_CHECK_ALARM_PANEL,
    CONF_CODE_ARM_REQUIRED,
    CONF_COUNTRY,
    CONF_DELAY_CHECK_OPERATION,
    CONF_DEVICE_INDIGITALL,
    CONF_ENTRY_ID,
    CONF_HAS_PERI,
    CONF_INSTALLATION,
    CONF_MAP_AWAY,
    CONF_MAP_CUSTOM,
    CONF_MAP_HOME,
    CONF_MAP_NIGHT,
    CONF_MAP_VACATION,
    CONF_NOTIFY_GROUP,
    CONF_USE_2FA,
    COUNTRY_NAMES,
    DEFAULT_CHECK_ALARM_PANEL,
    DEFAULT_CODE,
    DEFAULT_CODE_ARM_REQUIRED,
    DEFAULT_DELAY_CHECK_OPERATION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SecuritasHub,
    generate_uuid,
)
from .securitas_direct_new_api import (
    Installation,
    OtpPhone,
    PERI_DEFAULTS,
    PERI_OPTIONS,
    STD_DEFAULTS,
    STD_OPTIONS,
    STATE_LABELS,
)

VERSION = 3

_LOGGER = logging.getLogger(__name__)


class FlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 3
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self) -> None:
        """Initialize the flow handler."""
        self.config: OrderedDict = OrderedDict()
        self.securitas: SecuritasHub | None = None
        self.otp_challenge: tuple[str | None, list[OtpPhone] | None] | None = None
        self._available_installations: list[Installation] = []
        self._selected_installation: Installation | None = None
        self._options_data: dict[str, Any] = {}
        self._has_peri: bool = False

    async def _create_entry_for_installation(
        self, installation: Installation
    ) -> config_entries.ConfigFlowResult:
        """Register new entry for a specific installation."""
        username = self.config[CONF_USERNAME]
        unique_id = f"{username}_{installation.number}"
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        self.config[CONF_INSTALLATION] = installation.number
        return self.async_create_entry(title=installation.alias, data=dict(self.config))

    def _create_client(
        self,
    ) -> SecuritasHub:
        """Create client (SecuritasHub)."""

        if self.config[CONF_PASSWORD] is None:
            raise ValueError(
                "Invalid internal state. Called without either password or token"
            )

        self.securitas = SecuritasHub(
            self.config, None, async_get_clientsession(self.hass), self.hass
        )

        return self.securitas

    async def async_step_phone_list(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show the list of phones for the OTP challenge."""
        phone_index: int = -1
        assert user_input is not None
        selected_phone_key = user_input["phones"]

        assert self.otp_challenge is not None
        assert self.securitas is not None
        otp_phones = self.otp_challenge[1] or []
        try:
            index_str = selected_phone_key.split("_")[0]
            list_index = int(index_str)
            if 0 <= list_index < len(otp_phones):
                phone_index = otp_phones[list_index].id
        except (ValueError, IndexError):
            for phone_item in otp_phones:
                if phone_item.phone in selected_phone_key:
                    phone_index = phone_item.id
                    break

        await self.securitas.send_opt(self.otp_challenge[0] or "", phone_index)
        return self.async_show_form(
            step_id="otp_challenge",
            data_schema=vol.Schema({vol.Required(CONF_CODE): str}),
        )

    async def async_step_otp_challenge(self, user_input: dict[str, Any] | None = None):
        """Last step of the OTP challenge."""
        assert self.securitas is not None
        assert self.otp_challenge is not None
        assert user_input is not None
        await self.securitas.send_sms_code(
            self.otp_challenge[0] or "", user_input[CONF_CODE]
        )
        return await self.finish_setup()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 1: Country, username, password, 2FA toggle."""
        if user_input is None:
            country_options = [
                {"value": code, "label": name} for code, name in COUNTRY_NAMES.items()
            ]
            schema = vol.Schema(
                {
                    vol.Required(CONF_COUNTRY, default="ES"): selector(
                        {"select": {"options": country_options, "mode": "dropdown"}}
                    ),
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_USE_2FA, default=True): bool,
                }
            )
            return self.async_show_form(step_id="user", data_schema=schema)

        self.config = OrderedDict(user_input)

        uuid = generate_uuid()
        self.config[CONF_DELAY_CHECK_OPERATION] = DEFAULT_DELAY_CHECK_OPERATION
        self.config[CONF_DEVICE_ID] = uuid
        self.config[CONF_UNIQUE_ID] = uuid
        self.config[CONF_DEVICE_INDIGITALL] = ""
        self.config[CONF_ENTRY_ID] = ""

        self.securitas = self._create_client()

        if not self.config.get(CONF_USE_2FA, True):
            return await self.finish_setup()

        otp_result = await self.securitas.validate_device()
        self.otp_challenge = otp_result
        otp_phones = otp_result[1] or []
        phone_options = [
            {"value": f"{i}_{phone.phone}", "label": phone.phone}
            for i, phone in enumerate(otp_phones)
        ]
        return self.async_show_form(
            step_id="phone_list",
            data_schema=vol.Schema(
                {"phones": selector({"select": {"options": phone_options}})}
            ),
        )

    async def finish_setup(self):
        """Login, discover installations, detect peri, advance to options."""
        assert self.securitas is not None
        if self.securitas.get_authentication_token() is None:
            await self.securitas.login()
        self.config[CONF_TOKEN] = self.securitas.get_authentication_token()

        self.hass.data.setdefault(DOMAIN, {})
        self.hass.data[DOMAIN][SecuritasHub.__name__] = self.securitas

        username = self.config[CONF_USERNAME]
        sessions = self.hass.data[DOMAIN].setdefault("sessions", {})
        if username not in sessions:
            sessions[username] = {"hub": self.securitas, "ref_count": 0}

        installations = await self.securitas.session.list_installations()
        self.hass.data[DOMAIN]["installations"] = installations

        configured_ids = {
            entry.data.get(CONF_INSTALLATION) for entry in self._async_current_entries()
        }
        available = [
            inst for inst in installations if inst.number not in configured_ids
        ]

        if not available:
            return self.async_abort(reason="already_configured")

        if len(available) == 1:
            return await self._select_installation(available[0])

        self._available_installations = available
        return await self.async_step_select_installation()

    async def _select_installation(self, installation: Installation):
        """Set installation, call get_services, detect peri, advance to options."""
        self.config[CONF_INSTALLATION] = installation.number
        self._selected_installation = installation

        assert self.securitas is not None
        services = await self.securitas.get_services(installation)
        self.hass.data.setdefault(DOMAIN, {})
        self.hass.data[DOMAIN]["cached_services"] = {
            installation.number: services,
        }

        self._has_peri = self._detect_peri(installation)
        self.config[CONF_HAS_PERI] = self._has_peri

        return await self.async_step_options()

    def _detect_peri(self, installation: Installation) -> bool:
        """Detect perimeter support from alarmPartitions states."""
        peri_codes = {"E", "A", "B", "C"}
        for partition in installation.alarm_partitions:
            for state in partition.get("enterStates", []):
                if state in peri_codes:
                    return True
            for state in partition.get("leaveStates", []):
                if state in peri_codes:
                    return True
        return False

    async def async_step_select_installation(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 2: Let user pick which installation to configure."""
        if user_input is not None:
            selected_number = user_input[CONF_INSTALLATION]
            for inst in self._available_installations:
                if inst.number == selected_number:
                    return await self._select_installation(inst)
            return self.async_abort(reason="unknown_installation")

        install_options = [
            {"value": inst.number, "label": inst.alias}
            for inst in self._available_installations
        ]
        return self.async_show_form(
            step_id="select_installation",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_INSTALLATION): selector(
                        {"select": {"options": install_options}}
                    ),
                }
            ),
        )

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 3: PIN, scan interval, notification settings."""
        if user_input is not None:
            user_input.setdefault(CONF_CODE, DEFAULT_CODE)
            self._options_data = user_input
            return await self.async_step_mappings()

        notify_services = sorted(
            svc
            for svc in self.hass.services.async_services().get("notify", {}).keys()
            if svc not in {"notify", "send_message", "persistent_notification"}
        )
        notify_options = [{"value": "", "label": "(disabled)"}] + [
            {"value": svc, "label": svc} for svc in notify_services
        ]

        schema = vol.Schema(
            {
                vol.Optional(CONF_CODE, default=DEFAULT_CODE): str,
                vol.Optional(
                    CONF_CODE_ARM_REQUIRED, default=DEFAULT_CODE_ARM_REQUIRED
                ): bool,
                vol.Optional(
                    CONF_CHECK_ALARM_PANEL, default=DEFAULT_CHECK_ALARM_PANEL
                ): bool,
                vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): int,
                vol.Optional(
                    CONF_DELAY_CHECK_OPERATION, default=DEFAULT_DELAY_CHECK_OPERATION
                ): vol.All(vol.Coerce(float), vol.Range(min=1.0, max=15.0)),
                vol.Optional(CONF_NOTIFY_GROUP, default=""): selector(
                    {
                        "select": {
                            "options": notify_options,
                            "custom_value": True,
                            "mode": "dropdown",
                        }
                    }
                ),
            }
        )
        return self.async_show_form(step_id="options", data_schema=schema)

    async def async_step_mappings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 4: Alarm state mappings, then create entry."""
        if user_input is not None:
            self.config.update(self._options_data)
            self.config.update(user_input)
            assert self._selected_installation is not None
            return await self._create_entry_for_installation(
                self._selected_installation
            )

        defaults = PERI_DEFAULTS if self._has_peri else STD_DEFAULTS
        options = PERI_OPTIONS if self._has_peri else STD_OPTIONS
        select_options = [
            {"value": state.value, "label": STATE_LABELS[state]} for state in options
        ]

        schema = vol.Schema(
            {
                vol.Optional(CONF_MAP_HOME, default=defaults[CONF_MAP_HOME]): selector(
                    {"select": {"options": select_options}}
                ),
                vol.Optional(CONF_MAP_AWAY, default=defaults[CONF_MAP_AWAY]): selector(
                    {"select": {"options": select_options}}
                ),
                vol.Optional(
                    CONF_MAP_NIGHT, default=defaults[CONF_MAP_NIGHT]
                ): selector({"select": {"options": select_options}}),
                vol.Optional(
                    CONF_MAP_VACATION, default=defaults[CONF_MAP_VACATION]
                ): selector({"select": {"options": select_options}}),
                vol.Optional(
                    CONF_MAP_CUSTOM, default=defaults[CONF_MAP_CUSTOM]
                ): selector({"select": {"options": select_options}}),
            }
        )
        return self.async_show_form(step_id="mappings", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> SecuritasOptionsFlowHandler:
        """Get the options flow for this handler."""
        return SecuritasOptionsFlowHandler()


class SecuritasOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Securitas options."""

    def __init__(self) -> None:
        """Initialize options flow."""
        self._general_data: dict[str, Any] = {}

    def _get(self, key, default=None):
        """Read current value from options, falling back to entry data."""
        return self.config_entry.options.get(
            key, self.config_entry.data.get(key, default)
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 1: General settings."""
        if user_input is not None:
            user_input.setdefault(CONF_CODE, DEFAULT_CODE)
            self._general_data = user_input
            return await self.async_step_mappings()
        scan_interval = self._get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

        code_arm_required = self._get(CONF_CODE_ARM_REQUIRED, DEFAULT_CODE_ARM_REQUIRED)
        delay_check_operation = self._get(
            CONF_DELAY_CHECK_OPERATION, DEFAULT_DELAY_CHECK_OPERATION
        )
        check_alarm_panel = self._get(CONF_CHECK_ALARM_PANEL, DEFAULT_CHECK_ALARM_PANEL)

        notify_group = self._get(CONF_NOTIFY_GROUP, "")

        _NOTIFY_EXCLUDE = {"notify", "send_message", "persistent_notification"}
        notify_services = sorted(
            svc
            for svc in self.hass.services.async_services().get("notify", {}).keys()
            if svc not in _NOTIFY_EXCLUDE
        )
        notify_options = [{"value": "", "label": "(disabled)"}] + [
            {"value": svc, "label": svc} for svc in notify_services
        ]

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CODE,
                    description={"suggested_value": self._get(CONF_CODE, DEFAULT_CODE)},
                ): str,
                vol.Optional(CONF_CODE_ARM_REQUIRED, default=code_arm_required): bool,
                vol.Optional(CONF_CHECK_ALARM_PANEL, default=check_alarm_panel): bool,
                vol.Optional(CONF_SCAN_INTERVAL, default=scan_interval): int,
                vol.Optional(
                    CONF_DELAY_CHECK_OPERATION, default=delay_check_operation
                ): vol.All(vol.Coerce(float), vol.Range(min=1.0, max=15.0)),
                vol.Optional(CONF_NOTIFY_GROUP, default=notify_group): selector(
                    {
                        "select": {
                            "options": notify_options,
                            "custom_value": True,
                            "mode": "dropdown",
                        }
                    }
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_mappings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 2: Alarm state mappings."""
        if user_input is not None:
            data = {**self._general_data, **user_input}
            return self.async_create_entry(title="", data=data)

        has_peri = self.config_entry.data.get(CONF_HAS_PERI, False)

        # Determine defaults for mapping dropdowns
        defaults = PERI_DEFAULTS if has_peri else STD_DEFAULTS
        options = PERI_OPTIONS if has_peri else STD_OPTIONS
        valid_values = {state.value for state in options}

        def _valid_map(key: str) -> str:
            """Return saved mapping if valid for current options, else default."""
            val = self._get(key, defaults[key])
            return val if val in valid_values else defaults[key]

        map_home = _valid_map(CONF_MAP_HOME)
        map_away = _valid_map(CONF_MAP_AWAY)
        map_night = _valid_map(CONF_MAP_NIGHT)
        map_vacation = _valid_map(CONF_MAP_VACATION)
        map_custom = _valid_map(CONF_MAP_CUSTOM)

        # Build dropdown options
        select_options = [
            {"value": state.value, "label": STATE_LABELS[state]} for state in options
        ]

        schema = vol.Schema(
            {
                vol.Optional(CONF_MAP_HOME, default=map_home): selector(
                    {"select": {"options": select_options}}
                ),
                vol.Optional(CONF_MAP_AWAY, default=map_away): selector(
                    {"select": {"options": select_options}}
                ),
                vol.Optional(CONF_MAP_NIGHT, default=map_night): selector(
                    {"select": {"options": select_options}}
                ),
                vol.Optional(CONF_MAP_VACATION, default=map_vacation): selector(
                    {"select": {"options": select_options}}
                ),
                vol.Optional(CONF_MAP_CUSTOM, default=map_custom): selector(
                    {"select": {"options": select_options}}
                ),
            }
        )
        return self.async_show_form(step_id="mappings", data_schema=schema)
