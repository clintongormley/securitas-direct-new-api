# Force Arm & Status Message — Native Alarm Card Support

## Context

The Securitas integration already has a complete force-arm mechanism:
notifications, mobile actions, stored force context, and a custom
`force_arm` service. HA core now supports `force_arm_available`,
`status_message`, and a `force_arm` field on arm services natively
(PR clintongormley/homeassistant-core#1).

This design wires the existing Securitas logic into the new base class
attributes so the alarm panel card can show status and offer force-arm
natively.

## Changes (alarm_control_panel.py only)

### 1. Use base class properties instead of extra_state_attributes

- `self._attr_force_arm_available = True/False` replaces
  `self._attr_extra_state_attributes["force_arm_available"]`
- `self._attr_status_message` set to `"Open: Kitchen Window, Front Door"`
  on arming exception, cleared to `None` on success
- Keep `arm_exceptions` list in extra_state_attributes (no base class
  equivalent; useful for automations)

### 2. Accept force_arm parameter in arm methods

Update signatures:
- `async_alarm_arm_home(code, force_arm)`
- `async_alarm_arm_away(code, force_arm)`
- `async_alarm_arm_night(code, force_arm)`
- `async_alarm_arm_custom_bypass(code, force_arm)`

When `force_arm=True` and force context exists: pass stored
`reference_id` + `suid` to `set_arm_state()`.
When `force_arm=True` but no context: arm normally.

### 3. Clear status on state transitions

- On successful arm or disarm: `_attr_status_message = None`,
  `_attr_force_arm_available = False`
- On arming exception: `_attr_status_message = "Open: ..."`,
  `_attr_force_arm_available = True`

### 4. Dismiss notifications on widget force-arm

When `force_arm=True` leads to a successful arm, call
`_dismiss_arming_exception_notification()` to clear persistent
notifications.

## What stays the same

- Persistent notifications + mobile action buttons (kept alongside)
- Custom `force_arm` service (backward compatibility)
- Force context lifecycle (`_set_force_context` / `_clear_force_context`)
- All API layer code (no changes needed)
- `arm_exceptions` extra_state_attribute

## Status message format

Comma-separated device aliases: `"Open: Kitchen Window, Front Door"`
