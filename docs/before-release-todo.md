# Before-release manual testing todo

This branch combines the `subpanels` work (three-axis alarm model + opt-in
sub-panels) with the `eventlog` work (xSActV2 activity timeline → events
bus, sensor, Lovelace card). Automated tests cover the unit-level
behavior; the items below need a real HA install + real Verisure account
to validate, since they depend on actual API responses, multi-axis
state, or hardware configuration.

## Required before merge

### Sub-panel work

- [ ] **Sub-panel disarm preserves siblings.** Pressing Disarm on the
      Perimeter (or Annex) sub-panel must disarm only that axis, leaving
      the interior axis armed. Regression test for the bug behind
      [b509c13](https://github.com/clintongormley/securitas-direct-new-api/commit/b509c13)
      / [8cf541d](https://github.com/clintongormley/securitas-direct-new-api/commit/8cf541d).
      Setup: arm interior + perimeter (PARTIAL_DAY_PERI). Press Disarm on
      the Perimeter sub-panel. Expected:
      - integration sends `DARMPERI` (not `DARM1` or `DARM1DARMPERI`)
      - alarm transitions to PARTIAL_DAY (interior armed, perimeter off)
      - Interior sub-panel still shows ARMED_HOME

- [ ] **Italy: rejected-command notification names the failed command.**
      Map "Night" to "Partial Night" and press Night on the main panel.
      The panel rejects ARMNIGHT1 (Italian SDVECU). Expected
      user-facing notification text:
      > "This alarm mode is not supported by your panel (rejected: ARMNIGHT1).
      > Check the state mappings…"

      Then restore Night to "Not Used".

- [ ] **Sub-panel toggle visibility in options flow.** With the
      capability-gating change in
      [894953f](https://github.com/clintongormley/securitas-direct-new-api/commit/894953f),
      the Interior toggle should be visible whenever any sibling capability
      (`has_peri` or `has_annex`) is supported, *regardless* of whether the
      sibling toggle is currently enabled. Settings → Devices & Services →
      Securitas → Configure:
      - With `has_peri=True`: Perimeter and Interior toggles both visible.
      - Toggling Perimeter on/off must NOT cause the Interior toggle to
        appear/disappear.

- [ ] **Toggle-off removes the entity.** Disable a sub-panel toggle in
      options. The corresponding `alarm_control_panel.<alias>_<axis>`
      entity must disappear from the entity registry / dashboard.
      Re-enable: entity reappears.

### Activity log work

- [ ] **An HA automation can be triggered by `securitas_activity`
      events.** The integration documents these as the primary
      automation entrypoint, but no one has wired one up on a real
      install yet. Create an automation in the UI:

      ```yaml
      trigger:
        - platform: event
          event_type: securitas_activity
          event_data:
            category: alarm   # or tampering / sabotage / disarmed / …
      action:
        - service: notify.persistent_notification
          data:
            message: "fired: {{ trigger.event.data.alias }}"
      ```

      Trigger that category from the panel (or app) and confirm the
      notification fires once. Then add the documented "skip
      HA-issued" template condition and confirm it skips events
      injected from HA (arm/disarm via the alarm panel entity) but
      still fires for panel/app-originated activity:

      ```yaml
      condition:
        - condition: template
          value_template: "{{ not trigger.event.data.injected }}"
      ```

- [ ] **Force-arm injects `armed_with_exceptions` with the exception
      list.** Trigger an arm with an open sensor → expect the integration
      to surface the persistent notification with Force Arm. Press Force
      Arm. The activity timeline should show:
      - an entry with category `armed_with_exceptions` (HA badge), and
      - the open zone(s) listed inline when the row is expanded
      - `event.exceptions[]` populated on the bus event
      Then trigger a hard arm failure (5802/5824) and confirm an
      `arming_failed` row appears with the exceptions list.

- [ ] **Disabling the activity log sensor does not break bus events.**
      Originally the `securitas_activity` listener was attached inside
      `ActivityLogSensor.async_added_to_hass`, so disabling the sensor
      entity in the entity registry silently killed all bus events too.
      Commit `a084e19` moved the listener to `async_setup_entry` so it
      lives for the lifetime of the integration. Unit tests cover the
      decoupling, but the actual HA "disable entity" path isn't
      automation-testable. Sanity check on a real install:
      1. Set up an automation that listens for `securitas_activity`
         (any category) and writes a persistent notification.
      2. Disable `sensor.<alias>_activity_log` in
         Settings → Devices & Services → Entities.
      3. Arm / disarm at the panel.
      4. Confirm the notification still fires.

      Then re-enable the sensor.

## Post-merge follow-ups

- [ ] **Annex commands `ARMANNEX1` / `DARMANNEX1`.** Switched from
      suffix-less `ARMANNEX` / `DARMANNEX` in
      [f698cb8](https://github.com/clintongormley/securitas-direct-new-api/commit/f698cb8)
      to match the Verisure web app's dispatch. Has **never** been tested
      against a real annex installation — needs a Vatrinus UK user (or
      anyone with `ARMANNEX`/`DARMANNEX` in their JWT cap) to confirm
      arm/disarm of the annex axis works. Watch for a 4xx response on
      the new command names — the resolver's `mark_unsupported` runtime
      fallback will catch a wrong command name, and a 4xx HA log entry
      will appear, but there's no fallback to the old form on file.

- [ ] **Compound transition optimization (optional).** Verisure web app
      uses single-API-call compound commands when transitioning between
      partial states (e.g. `ARMINTFPART1` to go DAY → TOTAL without an
      explicit DARM1 in between). Our resolver always emits
      `DARM1 + <new-mode>`. Functionally correct but costs an extra
      round-trip and briefly transitions through DISARMED, which can
      fire HA automations. Decoded JS analysis in
      `docs/handoffs/2026-05-05-verisure-web-dispatch-findings.md`
      (gitignored).

- [ ] **Activity log: catalogue smart-lock event types.** Lock/unlock
      actions surfaced in `xSActV2` haven't been catalogued — no entries
      in `_ACTIVITY_TYPE_TO_CATEGORY` and no `lock_*` categories in
      `ActivityCategory`. Capture fixtures for HA-issued and
      panel/app-issued lock + unlock, then add categories + type-code
      mappings + injection from `lock.py` (mirroring how
      `alarm_control_panel.py` injects arm/disarm). Until then, lock
      events surface as either a `unknown`-category polled row or
      nothing at all.

## Done

- [x] **Force-arm flow.** Persistent notification with Force Arm /
      Cancel buttons fires when arming with an open sensor; Force Arm
      bypasses the exception. Confirmed working on the user's install.

- [x] **Backwards compatibility.** Existing users keep their
      `entity_id`, mappings, and PIN configuration. `CONF_HAS_PERI` is
      dropped from stored data and recomputed at load time.

- [x] **Sub-panel state derivation.** Multi-axis state from the API is
      correctly projected onto each sub-panel via `_extract_state`.
      Confirmed by user during regular use.
