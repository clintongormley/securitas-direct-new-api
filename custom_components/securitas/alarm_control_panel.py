"""Support for Securitas Direct (AKA Verisure EU) alarm control panels."""

import datetime
import re
from datetime import timedelta
import logging
from typing import Any

import homeassistant.components.alarm_control_panel as alarm
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntityFeature,  # type: ignore[attr-defined]
    CodeFormat,  # type: ignore[attr-defined]
)
from homeassistant.components.alarm_control_panel.const import AlarmControlPanelState
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_CODE, CONF_SCAN_INTERVAL
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import (
    AddEntitiesCallback,
    async_get_current_platform,
)
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.exceptions import ServiceValidationError

from . import (
    CONF_CODE_ARM_REQUIRED,
    CONF_HAS_PERI,
    CONF_NOTIFY_GROUP,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SecuritasDirectDevice,
    SecuritasHub,
)
from .securitas_direct_new_api import (
    ArmingExceptionError,
    ArmStatus,
    CheckAlarmStatus,
    DisarmStatus,
    Installation,
    PROTO_DISARMED,
    PROTO_TO_STATE,
    SecuritasDirectError,
    SecuritasState,
    STATE_TO_COMMAND,
)
from .securitas_direct_new_api.command_resolver import (
    AlarmState,
    CommandResolver,
    CommandStep,
    InteriorMode,
    PerimeterMode,
    PROTO_TO_ALARM_STATE,
    SECURITAS_STATE_TO_ALARM_STATE,
)

# Map HA alarm state names to config keys
HA_STATE_TO_CONF_KEY: dict[str, str] = {
    AlarmControlPanelState.ARMED_HOME: "map_home",
    AlarmControlPanelState.ARMED_AWAY: "map_away",
    AlarmControlPanelState.ARMED_NIGHT: "map_night",
    AlarmControlPanelState.ARMED_CUSTOM_BYPASS: "map_custom",
    AlarmControlPanelState.ARMED_VACATION: "map_vacation",
}

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=20)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Securitas Direct based on config_entry.

    No API calls are made here.  Entities start with unknown state;
    the first periodic ``async_update_status`` populates values via
    the rate-limited hub ``update_overview`` method.
    """
    entry_data = hass.data[DOMAIN][entry.entry_id]
    client: SecuritasHub = entry_data["hub"]
    alarms = []
    securitas_devices: list[SecuritasDirectDevice] = entry_data["devices"]
    for devices in securitas_devices:
        alarms.append(
            SecuritasAlarm(
                devices.installation,
                state=CheckAlarmStatus(),
                client=client,
                hass=hass,
            )
        )
    async_add_entities(alarms, False)
    hass.data[DOMAIN]["alarm_entities"] = {a.installation.number: a for a in alarms}

    # Schedule initial update shortly after setup to populate values
    # without blocking entity registration.
    if alarms:

        @callback
        def _initial_update(_now) -> None:
            for entity in alarms:
                entity.async_schedule_update_ha_state(force_refresh=True)

        async_call_later(hass, 5, _initial_update)

    platform = async_get_current_platform()
    platform.async_register_entity_service(
        "force_arm",
        {},
        "async_force_arm",
    )
    platform.async_register_entity_service(
        "force_arm_cancel",
        {},
        "async_force_arm_cancel",
    )


class SecuritasAlarm(alarm.AlarmControlPanelEntity):
    """Representation of a Securitas alarm status."""

    def __init__(
        self,
        installation: Installation,
        state: CheckAlarmStatus,
        client: SecuritasHub,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the Securitas alarm panel."""
        self._state: str | None = None
        self._last_status: str | None = None
        self._device: str = installation.address
        self.entity_id: str = f"securitas_direct.{installation.number}"
        self._attr_unique_id: str | None = f"securitas_direct.{installation.number}"
        self._time: datetime.datetime = datetime.datetime.now()
        self._message: str = ""
        self.installation: Installation = installation
        self._attr_extra_state_attributes: dict[str, Any] = {}
        self.client: SecuritasHub = client
        self.hass: HomeAssistant = hass
        self._has_peri = self.client.config.get(CONF_HAS_PERI, False)
        self._last_proto_code: str | None = None
        self._resolver = CommandResolver(has_peri=self._has_peri)

        # Build outgoing map: HA state -> API command string
        # Build incoming map: protomResponse code -> HA state
        # Build securitas state map: HA state -> SecuritasState (for resolver)
        self._command_map: dict[str, str] = {}
        self._status_map: dict[str, str] = {}
        self._securitas_state_map: dict[str, SecuritasState] = {}

        for ha_state, conf_key in HA_STATE_TO_CONF_KEY.items():
            sec_state_str = self.client.config.get(conf_key)
            if not sec_state_str:
                continue
            sec_state = SecuritasState(sec_state_str)
            if sec_state == SecuritasState.NOT_USED:
                continue
            self._command_map[ha_state] = STATE_TO_COMMAND[sec_state]
            self._securitas_state_map[ha_state] = sec_state
            for code, proto_state in PROTO_TO_STATE.items():
                if proto_state == sec_state and code not in self._status_map:
                    self._status_map[code] = ha_state
                    break
        scan_seconds = client.config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        # _update_interval is also used as the retention window for force-arm
        # context, so keep it at DEFAULT_SCAN_INTERVAL when polling is off.
        self._update_interval: timedelta = timedelta(
            seconds=scan_seconds if scan_seconds > 0 else DEFAULT_SCAN_INTERVAL
        )
        if scan_seconds > 0:
            self._update_unsub = async_track_time_interval(
                hass, self.async_update_status, self._update_interval
            )
        else:
            self._update_unsub = None
        self._operation_in_progress: bool = False
        self._operation_epoch: int = 0
        self._code: str | None = client.config.get(CONF_CODE, None)
        self._attr_code_format: CodeFormat | None = None
        if self._code:
            self._attr_code_format = (
                CodeFormat.NUMBER if self._code.isdigit() else CodeFormat.TEXT
            )
        self._attr_code_arm_required: bool = (
            client.config.get(CONF_CODE_ARM_REQUIRED, False) if self._code else False
        )

        self._last_arm_result: ArmStatus | DisarmStatus | None = None

        # Force-arm context: stored when arming fails due to non-blocking
        # exceptions (e.g. open window).  Consumed on the next arm attempt to
        # override the exception.  Cleared on status refresh.
        self._force_context: dict[str, Any] | None = None
        self._mobile_action_unsub = None

        self._attr_device_info: DeviceInfo | None = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            manufacturer="Securitas Direct",
            model=installation.panel,
            name=installation.alias,
            hw_version=installation.type,
        )
        self.update_status_alarm(state)

    def __force_state(self, state: str) -> None:
        self._last_status = self._state
        self._state = state
        if self.hass is not None:
            self.async_schedule_update_ha_state()

    def _notify_error(self, title: str, message: str) -> None:
        """Notify user with persistent notification."""
        notification_id = re.sub(r"\W+", "_", title.lower()).strip("_")
        self.hass.async_create_task(
            self.hass.services.async_call(
                domain="persistent_notification",
                service="create",
                service_data={
                    "title": title,
                    "message": message,
                    "notification_id": f"{DOMAIN}.{notification_id}_{self.installation.number}",
                },
            )
        )

    @property
    def name(self) -> str:  # type: ignore[override]
        """Return the name of the device."""
        return self.installation.alias

    async def async_added_to_hass(self) -> None:
        """Register mobile notification action listener when added to HA."""
        self._mobile_action_unsub = self.hass.bus.async_listen(
            "mobile_app_notification_action",
            self._handle_mobile_action,
        )

    @callback
    def _handle_mobile_action(self, event: Event) -> None:
        """Handle Force Arm / Cancel taps from mobile notification."""
        action = event.data.get("action")
        num = self.installation.number
        if action == f"SECURITAS_FORCE_ARM_{num}":
            self.hass.async_create_task(self.async_force_arm())
        elif action == f"SECURITAS_CANCEL_FORCE_ARM_{num}":
            self.hass.async_create_task(self.async_force_arm_cancel())

    async def async_will_remove_from_hass(self) -> None:
        """When entity will be removed from Home Assistant."""
        if self._update_unsub:
            self._update_unsub()
        if self._mobile_action_unsub:
            self._mobile_action_unsub()

    async def async_update(self) -> None:
        """Update the status of the alarm based on the configuration.

        This is called when HA reloads.
        """
        await self.async_update_status()

    async def async_update_status(self, _now=None) -> None:
        """Update the status of the alarm."""
        if self.hass is None:
            return
        if self._operation_in_progress:
            _LOGGER.debug(
                "Skipping status poll for %s - arm/disarm operation in progress",
                self.installation.number,
            )
            return
        self._clear_force_context()
        # Snapshot the operation epoch before the poll. If an arm/disarm
        # starts (and possibly finishes) while update_overview is awaited,
        # the epoch will have changed and this poll result is stale.
        epoch_before = self._operation_epoch
        alarm_status: CheckAlarmStatus = CheckAlarmStatus()
        try:
            alarm_status = await self.client.update_overview(self.installation)
        except SecuritasDirectError as err:
            if self._operation_epoch != epoch_before:
                _LOGGER.debug(
                    "Discarding stale poll error for %s - operation occurred during poll",
                    self.installation.number,
                )
                return
            _LOGGER.warning(
                "Error updating alarm status for %s: %s",
                self.installation.number,
                err.log_detail(),
            )
            if getattr(err, "http_status", None) == 403:
                self._set_waf_blocked(True)
            self.async_write_ha_state()
        else:
            if self._operation_epoch != epoch_before:
                _LOGGER.debug(
                    "Discarding stale poll result for %s - operation occurred during poll",
                    self.installation.number,
                )
                return
            self._set_waf_blocked(False)
            self.update_status_alarm(alarm_status)
            self.async_write_ha_state()

    def update_status_alarm(self, status: CheckAlarmStatus | None = None) -> None:
        """Update alarm status, from last alarm setting register or EST."""
        if status is not None and hasattr(status, "message"):
            self._message = status.message
            self._attr_extra_state_attributes["message"] = status.message
            self._attr_extra_state_attributes["response_data"] = (
                status.protomResponseData
            )

            if not status.protomResponse:
                _LOGGER.debug(
                    "Received empty protomResponse for %s"
                    " (operation_status: %s, message: %s, status: %s,"
                    " protomResponseData: %s), ignoring",
                    self.installation.number,
                    status.operation_status,
                    status.message,
                    status.status,
                    status.protomResponseData,
                )
                return
            # Only update _last_proto_code when protomResponse is a known proto
            # code.  Periodic polling uses xSStatus which returns values like
            # "ARMED_TOTAL" instead of proto codes; those must not overwrite
            # the last proto code or the resolver's state-based command
            # selection will break.
            if (
                status.protomResponse == PROTO_DISARMED
                or status.protomResponse in PROTO_TO_STATE
            ):
                self._last_proto_code = status.protomResponse
            if status.protomResponse == PROTO_DISARMED:
                self._state = AlarmControlPanelState.DISARMED
            elif status.protomResponse in self._status_map:
                self._state = self._status_map[status.protomResponse]
            else:
                self._state = AlarmControlPanelState.ARMED_CUSTOM_BYPASS
                _LOGGER.info(
                    "Unmapped alarm status code '%s' from Securitas. "
                    "Check your Alarm State Mappings in the integration options",
                    status.protomResponse,
                )

    def _check_code_for_arm_if_required(self, code: str | None) -> bool:
        """Check the code only if arming requires a code and a PIN is configured."""
        if not self._code or not self.code_arm_required:
            return True
        return self._check_code(code)

    def _check_code(self, code: str | None) -> bool:
        """Check that the code entered in the panel matches the code in the config."""
        result: bool = not self._code or self._code == code
        if not result:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_pin_code",
                translation_placeholders={
                    "entity_id": self.entity_id,
                },
            )
        return result

    async def _execute_transition(
        self,
        target: AlarmState,
        **force_params: str,
    ) -> ArmStatus | DisarmStatus:
        """Execute a state transition, retrying once if state was stale.

        After executing the resolved command sequence, checks whether the
        panel's actual state matches the target.  If not (e.g. because
        ``_last_proto_code`` was stale), updates the proto code from the
        real response and retries with the corrected current state.
        """
        result: ArmStatus | DisarmStatus | None = None

        for attempt in range(2):
            current = PROTO_TO_ALARM_STATE.get(
                self._last_proto_code or "D",
                AlarmState(InteriorMode.OFF, PerimeterMode.OFF),
            )
            steps = self._resolver.resolve(current, target)

            if not steps:
                # Resolver says we're already in the target state.
                return DisarmStatus(protomResponse=self._last_proto_code or "D")

            for step in steps:
                result = await self._execute_step(step, **force_params)

            assert result is not None

            # Check whether we actually reached the target state.
            actual_proto = result.protomResponse
            if actual_proto and actual_proto in PROTO_TO_ALARM_STATE:
                actual_state = PROTO_TO_ALARM_STATE[actual_proto]
                if actual_state == target:
                    return result

                if attempt == 0:
                    _LOGGER.warning(
                        "State mismatch: expected %s, got %s (proto %s). "
                        "Retrying with corrected state.",
                        target,
                        actual_state,
                        actual_proto,
                    )
                    self._last_proto_code = actual_proto
                    continue

            # No proto code to compare, or second attempt — accept as-is.
            return result

        assert result is not None
        return result

    async def _execute_step(
        self,
        step: CommandStep,
        **force_params: str,
    ) -> ArmStatus | DisarmStatus:
        """Execute a single command step, trying alternatives on failure."""
        last_err: SecuritasDirectError | None = None

        for command in step.commands:
            if command in self._resolver.unsupported:
                continue

            try:
                _LOGGER.info("Sending command: %s", command)
                if "+" in command:
                    # Multi-step: split and execute sequentially
                    sub_commands = command.split("+")
                    result: ArmStatus | DisarmStatus | None = None
                    for sub_cmd in sub_commands:
                        _LOGGER.info("Sending sub-command: %s", sub_cmd)
                        result = await self._send_single_command(
                            sub_cmd, **force_params
                        )
                        self._last_arm_result = result
                    assert result is not None
                    return result
                result = await self._send_single_command(command, **force_params)
                self._last_arm_result = result
                return result
            except ArmingExceptionError:
                raise  # Arming exceptions need special handling upstream
            except SecuritasDirectError as err:
                if err.http_status == 403:
                    self._notify_error(
                        "Securitas: Rate limited",
                        "Too many requests — blocked by Securitas servers. "
                        "Please wait a few minutes before trying again.",
                    )
                    raise
                if err.http_status == 409:
                    raise  # Server busy — don't try alternatives
                if err.http_status is not None:
                    # GraphQL validation error (e.g. BAD_USER_INPUT) —
                    # command not in panel's enum, mark as unsupported
                    _LOGGER.info(
                        "Command %s not supported by panel (status %s),"
                        " trying next alternative: %s",
                        command,
                        err.http_status,
                        err.log_detail(),
                    )
                    self._resolver.mark_unsupported(command)
                else:
                    # Panel-level error (e.g. TECHNICAL_ERROR after polling) —
                    # panel communication failure, not a command issue.
                    # Don't try alternatives (they'll likely also fail).
                    raise
                last_err = err

        if last_err and last_err.http_status == 400:
            raise SecuritasDirectError(
                "This alarm mode is not supported by your panel. "
                "Check the state mappings in the integration options "
                "(Settings → Devices & Services → Securitas → Configure).",
                http_status=400,
            )
        raise last_err or SecuritasDirectError(
            "No supported command found for this panel. "
            "Check the state mappings in the integration options "
            "(Settings → Devices & Services → Securitas → Configure).",
        )

    async def _send_single_command(
        self,
        command: str,
        **force_params: str,
    ) -> ArmStatus | DisarmStatus:
        """Send a single arm or disarm command to the API."""
        if command.startswith("D"):
            return await self.client.disarm_alarm(self.installation, command)
        return await self.client.arm_alarm(self.installation, command, **force_params)

    def _set_refresh_failed(self, failed: bool) -> None:
        """Track whether the last manual refresh timed out."""
        if failed:
            self._attr_extra_state_attributes["refresh_failed"] = True
        else:
            self._attr_extra_state_attributes.pop("refresh_failed", None)

    def _set_waf_blocked(self, blocked: bool) -> None:
        """Track WAF rate-limit state for the alarm card."""
        if blocked:
            self._attr_extra_state_attributes["waf_blocked"] = True
        else:
            if self._attr_extra_state_attributes.pop("waf_blocked", None):
                # Dismiss the rate-limited persistent notification
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        domain="persistent_notification",
                        service="dismiss",
                        service_data={
                            "notification_id": (
                                f"{DOMAIN}.securitas_rate_limited"
                                f"_{self.installation.number}"
                            ),
                        },
                    )
                )

    def _mode_to_alarm_state(self, mode: str) -> AlarmState:
        """Convert an HA alarm mode to an AlarmState using the securitas state map."""
        securitas_state = self._securitas_state_map.get(mode)
        if securitas_state is None:
            raise SecuritasDirectError(f"Unsupported alarm mode: {mode}")
        return SECURITAS_STATE_TO_ALARM_STATE[securitas_state]

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Send disarm command."""
        if not self._check_code(code):
            return
        self.__force_state(AlarmControlPanelState.DISARMING)
        self._operation_in_progress = True
        self._operation_epoch += 1
        try:
            target = AlarmState(InteriorMode.OFF, PerimeterMode.OFF)
            result = await self._execute_transition(target)
            self._set_waf_blocked(False)
            self.update_status_alarm(
                CheckAlarmStatus(
                    operation_status=result.operation_status,
                    message=getattr(result, "message", ""),
                    status="",
                    InstallationNumer="",
                    protomResponse=result.protomResponse,
                    protomResponseData="",
                )
            )
            self.async_write_ha_state()
        except SecuritasDirectError as err:
            self._state = self._last_status
            _LOGGER.error(
                "Disarm failed for %s: %s", self.installation.number, err.log_detail()
            )
            if getattr(err, "http_status", None) == 403:
                self._set_waf_blocked(True)
            else:
                self._notify_error("Securitas: Error disarming", err.message)
            self.async_write_ha_state()
        finally:
            self._operation_in_progress = False

    async def set_arm_state(
        self,
        mode: str,
        *,
        force_arming_remote_id: str | None = None,
        suid: str | None = None,
    ) -> None:
        """Set the arm state using the command resolver."""
        self._operation_in_progress = True
        self._operation_epoch += 1
        self._last_arm_result = ArmStatus()

        force_params: dict[str, str] = {}
        if force_arming_remote_id:
            force_params["force_arming_remote_id"] = force_arming_remote_id
        if suid:
            force_params["suid"] = suid

        try:
            target = self._mode_to_alarm_state(mode)
            result = await self._execute_transition(target, **force_params)
            self._set_waf_blocked(False)
            self.update_status_alarm(
                CheckAlarmStatus(
                    operation_status=getattr(result, "operation_status", ""),
                    message=getattr(result, "message", ""),
                    status="",
                    InstallationNumer="",
                    protomResponse=result.protomResponse,
                    protomResponseData="",
                )
            )
            self.async_write_ha_state()
        except ArmingExceptionError as exc:
            self._set_force_context(exc, mode)
            self._state = self._last_status
            self._notify_arm_exceptions(exc)
        except SecuritasDirectError as err:
            if self._last_arm_result.protomResponse:
                self.update_status_alarm(
                    CheckAlarmStatus(
                        operation_status=self._last_arm_result.operation_status,
                        message="",
                        status="",
                        InstallationNumer="",
                        protomResponse=self._last_arm_result.protomResponse,
                        protomResponseData="",
                    )
                )
            else:
                self._state = self._last_status
            _LOGGER.error(
                "Arm failed for %s: %s", self.installation.number, err.log_detail()
            )
            if getattr(err, "http_status", None) == 403:
                self._set_waf_blocked(True)
            else:
                self._notify_error("Securitas: Arming failed", err.message)
            self.async_write_ha_state()
        finally:
            self._operation_in_progress = False

    def _set_force_context(self, exc: ArmingExceptionError, mode: str) -> None:
        """Store force-arm context from an arming exception."""
        self._force_context = {
            "reference_id": exc.reference_id,
            "suid": exc.suid,
            "mode": mode,
            "exceptions": exc.exceptions,
            "created_at": datetime.datetime.now(),
        }
        self._attr_extra_state_attributes["arm_exceptions"] = [
            e.get("alias", "unknown") for e in exc.exceptions
        ]
        self._attr_extra_state_attributes["force_arm_available"] = True

    def _clear_force_context(self, force: bool = False) -> None:
        """Clear stored force-arm context and related attributes.

        When called from async_update_status (force=False), only clears if
        the context has aged past one scan interval.  HA triggers an immediate
        status refresh after every service call, so without this guard the
        context would be wiped before the user can re-arm.
        """
        if not force and self._force_context is not None:
            age = datetime.datetime.now() - self._force_context["created_at"]
            if age < self._update_interval:
                return
        self._force_context = None
        self._attr_extra_state_attributes.pop("arm_exceptions", None)
        self._attr_extra_state_attributes.pop("force_arm_available", None)

    @property
    def _arming_exception_notification_id(self) -> str:
        """Return a per-installation persistent-notification ID."""
        return f"{DOMAIN}.arming_exception_{self.installation.number}"

    def _notify_arm_exceptions(self, exc: ArmingExceptionError) -> None:
        """Send notifications about arming exceptions."""
        if exc.exceptions:
            sensor_list = "\n".join(
                f"- {e.get('alias', 'unknown')}" for e in exc.exceptions
            )
            short_details = ", ".join(e.get("alias", "unknown") for e in exc.exceptions)
        else:
            sensor_list = "- (unknown sensor)"
            short_details = "open sensor"

        title = "Securitas: Arm blocked — open sensor(s)"
        persistent_message = (
            f"Arming was blocked because the following sensor(s) are open:\n"
            f"{sensor_list}\n\n"
            f"To arm anyway, call the **securitas.force_arm** service, "
            f"or tap **Force Arm** on your mobile notification."
        )
        mobile_message = f"Arm blocked — open sensor(s): {short_details}. Arm anyway?"

        self.hass.async_create_task(
            self.hass.services.async_call(
                domain="persistent_notification",
                service="create",
                service_data={
                    "title": title,
                    "message": persistent_message,
                    "notification_id": self._arming_exception_notification_id,
                },
            )
        )

        # Notify configured group if set (Companion App with action buttons)
        notify_group = self.client.config.get(CONF_NOTIFY_GROUP)
        if notify_group:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    domain="notify",
                    service=notify_group,
                    service_data={
                        "title": title,
                        "message": mobile_message,
                        "data": {
                            "tag": self._arming_exception_notification_id,
                            "actions": [
                                {
                                    "action": (
                                        "SECURITAS_FORCE_ARM"
                                        f"_{self.installation.number}"
                                    ),
                                    "title": "Force Arm",
                                },
                                {
                                    "action": (
                                        "SECURITAS_CANCEL_FORCE_ARM"
                                        f"_{self.installation.number}"
                                    ),
                                    "title": "Cancel",
                                },
                            ],
                        },
                    },
                )
            )

    def _dismiss_arming_exception_notification(self) -> None:
        """Dismiss the persistent and mobile arming-exception notifications."""
        self.hass.async_create_task(
            self.hass.services.async_call(
                domain="persistent_notification",
                service="dismiss",
                service_data={
                    "notification_id": self._arming_exception_notification_id
                },
            )
        )
        # Clear mobile notification if notify group is configured
        notify_group = self.client.config.get(CONF_NOTIFY_GROUP)
        if notify_group:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    domain="notify",
                    service=notify_group,
                    service_data={
                        "message": "clear_notification",
                        "data": {
                            "tag": self._arming_exception_notification_id,
                        },
                    },
                )
            )

    async def async_force_arm_cancel(self) -> None:
        """Cancel a pending force-arm context.

        Called by the securitas.force_arm_cancel service. Clears the stored
        exception context and dismisses the arming-exception notification.
        """
        if self._force_context is None:
            _LOGGER.warning(
                "force_arm_cancel called for %s but no force context available",
                self.installation.number,
            )
            return
        _LOGGER.info("Force-arm cancelled by user")
        self._clear_force_context(force=True)
        self._dismiss_arming_exception_notification()
        self.async_write_ha_state()

    async def async_force_arm(self) -> None:
        """Force-arm using stored exception context.

        Called by the securitas.force_arm service. Re-arms in the same mode
        that previously failed, passing the stored referenceId and suid to
        override non-blocking exceptions.
        """
        if self._force_context is None:
            _LOGGER.warning(
                "force_arm called for %s but no force context available",
                self.installation.number,
            )
            return
        mode = self._force_context["mode"]
        ref_id = self._force_context["reference_id"]
        suid = self._force_context["suid"]
        _LOGGER.info(
            "Force-arming: overriding previous exceptions %s",
            [e.get("alias") for e in self._force_context.get("exceptions", [])],
        )
        self._clear_force_context(force=True)
        self._dismiss_arming_exception_notification()
        self.__force_state(AlarmControlPanelState.ARMING)
        await self.set_arm_state(mode, force_arming_remote_id=ref_id, suid=suid)

    async def async_alarm_arm_home(self, code: str | None = None):
        """Send arm home command."""
        if self._check_code_for_arm_if_required(code):
            self.__force_state(AlarmControlPanelState.ARMING)
            await self.set_arm_state(AlarmControlPanelState.ARMED_HOME)

    async def async_alarm_arm_away(self, code: str | None = None):
        """Send arm away command."""
        if self._check_code_for_arm_if_required(code):
            self.__force_state(AlarmControlPanelState.ARMING)
            await self.set_arm_state(AlarmControlPanelState.ARMED_AWAY)

    async def async_alarm_arm_night(self, code: str | None = None):
        """Send arm night command."""
        if self._check_code_for_arm_if_required(code):
            self.__force_state(AlarmControlPanelState.ARMING)
            await self.set_arm_state(AlarmControlPanelState.ARMED_NIGHT)

    async def async_alarm_arm_custom_bypass(self, code: str | None = None):
        """Send arm perimeter command."""
        if self._check_code_for_arm_if_required(code):
            self.__force_state(AlarmControlPanelState.ARMING)
            await self.set_arm_state(AlarmControlPanelState.ARMED_CUSTOM_BYPASS)

    async def async_alarm_arm_vacation(self, code: str | None = None):
        """Send arm vacation command."""
        if self._check_code_for_arm_if_required(code):
            self.__force_state(AlarmControlPanelState.ARMING)
            await self.set_arm_state(AlarmControlPanelState.ARMED_VACATION)

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:  # type: ignore[override]
        """Return the state of the alarm."""
        if self._state is None:
            return None
        try:
            return getattr(AlarmControlPanelState, self._state.upper())
        except AttributeError:
            return None

    @property
    def supported_features(self) -> int:  # type: ignore[override]
        """Return the list of supported features."""
        features = 0
        if AlarmControlPanelState.ARMED_HOME in self._command_map:
            features |= AlarmControlPanelEntityFeature.ARM_HOME
        if AlarmControlPanelState.ARMED_AWAY in self._command_map:
            features |= AlarmControlPanelEntityFeature.ARM_AWAY
        if AlarmControlPanelState.ARMED_NIGHT in self._command_map:
            features |= AlarmControlPanelEntityFeature.ARM_NIGHT
        if AlarmControlPanelState.ARMED_CUSTOM_BYPASS in self._command_map:
            features |= AlarmControlPanelEntityFeature.ARM_CUSTOM_BYPASS
        if AlarmControlPanelState.ARMED_VACATION in self._command_map:
            features |= AlarmControlPanelEntityFeature.ARM_VACATION
        return features
