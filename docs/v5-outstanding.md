# v5 Verisure Rebrand — Outstanding Work

**Branch:** `migration` at `02e95e9` (one commit on top of `main`).
**Status:** READY_FOR_PR (preview), **not yet READY_FOR_RELEASE**.
**Spec:** [docs/superpowers/specs/2026-05-05-verisure-rebrand-design.md](superpowers/specs/2026-05-05-verisure-rebrand-design.md)

This document is the punch list of everything that still has to land before v5.0.0 ships, plus everything that's been deferred.

The branch's principle, restated by the user on 2026-05-07: **migrate everything to Verisure. The only exceptions are shims that stay in the codebase for 6 months and notify users they are deprecated.**

---

## 1. Hard blockers

These must land before tagging v5.0.0.

### 1.1 Annex sub-zone disambiguation (spec §5, Open Question 1)

**Status:** Information requested from issue #441 reporter; not yet received.

**Why blocking:** Today, installations with a Verisure annex sub-zone silently lose their annex camera and capture button at startup because main and annex sub-panels return the same `zone_id`, colliding on the camera unique-id. Locks plausibly have the same shape (unconfirmed).

**What's needed before code:**

1. **Unredacted `xSDeviceList` response** from Vatrinus's account.
2. **Expanded `xSDeviceList` query** trying candidate fields: `panel`, `installationId`, `area`, `subzone`, `parent`, `zone`, `zoneType`, `__typename`. Determine which the server accepts and which (if any) discriminates main from annex.
3. **`xSGetSmartlockConfig` for any annex lock** in the same install — confirms whether locks need the same fix.

**What lands once the field is identified:**

- Add the discriminator field to `CameraDevice` (and `SmartLock` if needed).
- Append a `[_{discriminator}]` suffix to `v5_verisure_owa.{numinst}_camera_{zone_id}` and friends, only when populated.
- Apply `_disambiguator()` helper to friendly names for cameras, sentinels, and locks.
- Forward-only — existing main-panel devices keep clean unique IDs; only newly-registered annex devices get the suffix.

Tracked in [docs/handoffs/duplicate_id_fix.md](handoffs/duplicate_id_fix.md).

---

## 2. Scope expansion (user request, 2026-05-07)

The original spec §1 deferred the vendored library and JS card class names. The user has reversed that decision: **migrate everything except deprecation shims, and shims must surface their deprecation to users.**

### 2.1 Rename the vendored API library

The directory `custom_components/verisure_owa/securitas_direct_new_api/` and its public class names are still Securitas-branded. They need to migrate.

**Scope:**

- **Directory rename:** `securitas_direct_new_api/` → **`verisure_owa_api/`** (decision: 2026-05-07).
- **Class renames** inside the package:
  - `SecuritasDirectError` → `VerisureOwaError` (base exception)
  - `SecuritasClient` → `VerisureOwaClient`
  - `SecuritasState` → `VerisureOwaState`
  - Subclass error names (`AuthenticationError`, `TwoFactorRequiredError`, `SessionExpiredError`, `APIResponseError`, `WAFBlockedError`, `APIConnectionError`, `OperationTimeoutError`, `OperationFailedError`, `ArmingExceptionError`, `ImageCaptureError`, `UnexpectedStateError`) — keep their names; they're already brand-neutral.
- **Update every import** in `custom_components/verisure_owa/*.py` and the test suite (~30 test files).
- **Update `__version__.py`** strings (title, url).
- **Update package docstrings** in `__init__.py`, `client.py`, `models.py`, `exceptions.py`, etc.
- **Update example file** at `verisure_owa_api/examples/basic_operations.py`.

**Compatibility (decision: 2026-05-07):** No backwards-compat shim. Rename freely. Document the rename in v5 release notes — anyone using the vendored library directly (e.g. cloning the repo and importing it standalone for non-HA scripts) updates their imports. Audience for this group is essentially zero (no PyPI package, no documented standalone-library workflow), so the absence of a compat shim is acceptable.

**Effort:** Mechanical, ~15 minutes via `git mv` + sed sweep. Tests should still pass once imports are updated.

### 2.2 Rename JS card classes + add deprecation shim for the tag names

Today the JS classes are `SecuritasAlarmCard`, `SecuritasAlarmCardEditor`, `SecuritasAlarmBadge`, `SecuritasAlarmChip`, `SecuritasCameraCard`. Each is registered with a `securitas-*` custom-element tag.

**Distinction:** The tag name (e.g. `securitas-alarm-card`) IS user-facing — users embed `type: custom:securitas-alarm-card` in their dashboards. The class name backing the tag is purely internal JS.

**Plan:**

1. **Class rename** (no shim — internal JS). `SecuritasAlarmCard` → `VerisureOwaAlarmCard`, etc. Same pattern for the other 4 classes. Touch both `verisure_owa-alarm-card.js` and `verisure_owa-camera-card.js` plus their `securitas-*.js` legacy-name copies.

2. **Tag-name shim** (THIS is the user-facing compat). Register both tags during the deprecation window:

   ```js
   // Canonical
   customElements.define("verisure-owa-alarm-card", VerisureOwaAlarmCard);
   customElements.define("verisure-owa-alarm-badge", VerisureOwaAlarmBadge);
   customElements.define("verisure-owa-alarm-chip", VerisureOwaAlarmChip);

   // Legacy shim — deprecated, removed in v6
   class _SecuritasAlarmCardLegacyShim extends VerisureOwaAlarmCard {
     connectedCallback() {
       super.connectedCallback();
       if (!this._deprecationLogged) {
         console.warn(
           "Lovelace card type 'custom:securitas-alarm-card' is deprecated " +
           "and will be removed in v6. Update your dashboard to " +
           "'custom:verisure-owa-alarm-card'."
         );
         this._deprecationLogged = true;
       }
     }
   }
   customElements.define("securitas-alarm-card", _SecuritasAlarmCardLegacyShim);
   ```

   Same pattern for badge, chip, card-editor, camera-card.

3. **`window.customCards` registration (decision: 2026-05-07):** register ONLY the new `verisure-owa-*` types as canonical entries — visible in the HACS / Lovelace card picker. The legacy `securitas-*` types are NOT added to `window.customCards`, so users see only the new option in the picker. Existing dashboards with `type: custom:securitas-alarm-card` continue to render correctly via the tag-name shim above (browser knows the tag from `customElements.define`); they just don't appear as a "create new card" option.

4. **HACS card-picker name strings:** the canonical `verisure-owa-*` entries get clean names: "Verisure OWA Alarm Card", "Verisure OWA Alarm Badge", "Verisure OWA Alarm Chip", "Verisure OWA Camera Card".

**Effort:** Couple of hours with TDD (manual browser-console check for the deprecation warning is the only thing that's hard to unit-test in JS).

### 2.3 Audit deprecation surfaces — every shim must tell users it's deprecated

**Principle:** if v6 will remove it, v5 must tell the user, in a way they will actually see.

Audit each deprecation surface and ensure it has a clear deprecation signal:

| Surface | Current state | Needed |
|---|---|---|
| `custom_components/securitas/` shim integration | Persistent notification "Migration complete, restart required" | **ENHANCE:** also state "the legacy `securitas` integration is deprecated and will be removed in v6.0.0 (~6 months from now). No action needed beyond the restart." |
| `services.yaml` legacy services description | Says "DEPRECATED — use verisure_owa.* instead" | ✅ adequate (visible in HA UI service picker) |
| `securitas.force_arm` runtime call | Logs `_LOGGER.warning` once per call | ✅ adequate (visible in HA logs to anyone with WARNING level on, which is the default) |
| `securitas_arming_exception` event | Fires silently alongside new event | **ADD:** when an HA event listener subscribes specifically to `securitas_arming_exception` (not the new name), emit a one-time persistent notification telling the user to update their automation YAML. Detection: `hass.bus.async_listeners()` includes the legacy event name. Log once on first fire, with the calling automation's entity_id if available (HA fires from a known context). If detection is too brittle, fall back to a one-time setup-time notification listing the deprecated names regardless of whether anything is listening. |
| `/securitas_panel/` static URL | No warning | **ADD:** when a request hits `/securitas_panel/...`, log a `_LOGGER.warning` once per HA boot. Also: HACS dashboard resource list will show the user has both `/securitas_panel/...js` and `/verisure_owa_panel/...js` resources after upgrade — leave this for the release notes (no UI surface to clean up automatically). |
| `custom:securitas-alarm-card` JS tag | No warning | **ADD:** browser-console `console.warn` in the legacy class shim (described in 2.2 above) |

**Single bundled in-product deprecation summary on first v5 boot:**

When the shim's `async_setup_entry` runs migration successfully, the persistent notification it shows (currently "Migration complete, restart required") should be expanded to:

```
Title: Verisure OWA migration: restart required
Message:
Your Securitas Direct integration has been migrated to Verisure OWA.
Please restart Home Assistant to complete the upgrade.

What's deprecated in v5 and will be removed in v6 (~6 months from now):
- Service calls: securitas.force_arm[_cancel] → use verisure_owa.force_arm[_cancel]
- Events: securitas_arming_exception → use verisure_owa_arming_exception
- Lovelace card URLs: /securitas_panel/... → use /verisure_owa_panel/...
- Lovelace card types: custom:securitas-alarm-card → use custom:verisure-owa-alarm-card

Update your automations and dashboards at your pace within the deprecation
window. All your devices, entities, and customizations are preserved
through the migration.
```

This is one notification users will read. The per-surface warnings (logs, console.warn, service-picker descriptions) are the second line of defence for users who didn't read.

**Effort:** Small per-surface change; one well-written notification body covers most of the user-facing communication.

---

## 3. Manual verification

These can't be automated. The user has to run them.

### 3.1 Real-HA upgrade smoke test

Required before tagging v5.0.0. Steps:

1. Spin up an HA dev instance (Docker / hass venv) on the v4 codebase (currently `main` minus this branch).
2. Configure a `securitas` integration entry, log in, let cameras/sensors discover.
3. Note the entity_ids generated (especially of sensors and the alarm panel).
4. Customize one entity's name and area assignment in the HA UI.
5. Stop HA. Update the codebase to this branch (or v5.0.0 if tagged).
6. Start HA.
7. **Verify:**
   - Persistent notification appears ("Migration complete, restart required" + deprecation summary).
   - Logs show the migration ran without errors.
   - Restart HA again.
   - All entities re-appear with their original entity_ids.
   - The customized entity still has its custom name and area.
   - `verisure_owa.force_arm` works in Developer Tools → Services.
   - `securitas.force_arm` (legacy alias) works AND emits a deprecation warning in the log.
   - `/securitas_panel/securitas-alarm-card.js` and `/verisure_owa_panel/verisure_owa-alarm-card.js` both serve a 200 response with the same content.
   - A dashboard with `type: custom:securitas-alarm-card` still renders correctly.

### 3.2 Browser-console verification (after 2.2 lands)

The custom-element deprecation `console.warn` should appear in the browser dev tools when a dashboard with `custom:securitas-alarm-card` loads. Verify visually.

---

## 4. Release-day operations (per spec §8)

Not part of this branch's commits. Run on tagging day.

### 4.1 Repo rename

**Recommendation (spec §8.1):** Rename `guerrerotook/securitas-direct-new-api` → `guerrerotook/verisure-owa-ha`.

**Sequence (spec §8 has the full version; condensed here):**

1. Update `manifest.json` `documentation` and `issue_tracker` URLs to the new repo.
2. Update `hacs.json` listing.
3. Update README badges, screenshots, links.
4. Update GitHub Actions workflows that reference the repo by URL.
5. Commit & merge those changes to `main`.
6. Settings → General → Repository name → rename. (GitHub creates a permanent 301 redirect.)
7. Verify the redirect works (clone old URL, fetch raw README).
8. Update repo description and topics for HACS discoverability: `verisure`, `home-assistant`, `hacs`, `alarm`, `home-automation`.
9. Tag v5.0.0 under the new repo URL.
10. Open a PR to [`hacs/default`](https://github.com/hacs/default) updating the listed repo URL.

### 4.2 Release notes

Required content:

- **The rename.** Both names mentioned, both URLs working via redirect.
- **Migration is automatic** on first v5 launch; HA restart required.
- **Deprecation window: 6 months.** Hard cutoff in v6.0.0. Specific list of what's deprecated:
  - `securitas` integration domain (config entries auto-migrate)
  - `securitas.force_arm[_cancel]` services
  - `securitas_arming_exception` event
  - `/securitas_panel/` static URL
  - `custom:securitas-alarm-card` (and badge/chip/camera-card) Lovelace types
- **Lovelace resource cleanup hint:** users may see a duplicate resource entry (old `/securitas_panel/...js` + new `/verisure_owa_panel/...js`) — both work; either can be deleted manually if desired.
- **Spain users:** API now goes to `customers.verisure.es` automatically — no action required.
- **Peru added** as a new supported country.
- **Lower-priority cosmetic improvements:** sentinel sensor names propagate device renames now, so renaming the installation in the HA UI updates all sensor names automatically.
- **Class rename:** any third-party tooling that imports `from custom_components.securitas...` must update. (Unlikely to exist, but worth mentioning for plugin developers.)

---

## 5. Low-severity test coverage gaps (not blockers)

These were flagged in the final code review. Adding them strengthens the migration test suite. Each is small.

### 5.1 Multi-installation migration test

**Gap:** `tests/test_migrate.py`'s end-to-end fixture seeds one legacy entry. The migration is per-entry by design, but there's no integration test that seeds two legacy entries with different `unique_id`s and asserts they migrate independently.

**Add:** Test that creates two `MockConfigEntry`s with `domain="securitas"` and different `unique_id`s, runs `migrate_legacy_entry` on each, asserts both new entries exist under `verisure_owa` with their original data preserved.

### 5.2 Camera device end-to-end migration test

**Gap:** The migration mapping for cameras is unit-tested at the function level (`OLD_TO_NEW_UNIQUE_ID("v4_100001_camera_YR08")`), and the schema is verified by the CI completeness test. But the `legacy_entry_with_state` fixture seeds an alarm panel + lock + sensor; cameras aren't seeded.

**Add:** Extend the fixture (or add a sibling fixture) to also seed a legacy camera device + entity, run migration, assert the camera device is re-platformed with the v5 identifier and stays linked via `via_device` to the panel.

---

## 6. Future v6 work (NOT v5)

Documented here only so v6 has a starting point. Not part of this branch.

- Delete `custom_components/securitas/` shim entirely.
- Remove all legacy aliases from `verisure_owa`:
  - `securitas.force_arm[_cancel]` services + `register_legacy_service_aliases`
  - `securitas_arming_exception` event firing + listener
  - `/securitas_panel/` static URL registration
  - `securitas-alarm-card.js` / `securitas-camera-card.js` legacy-name files in `www/`
  - JS tag-name shims (`customElements.define("securitas-*", ...)`)
- v6 detects pre-v5 state and shows a repair issue: "v6 cannot upgrade from pre-v5; please install v5 first to migrate."

---

## Suggested ordering

If you want to keep the migration branch tight and ship a single PR, the natural order is:

1. **Vendored library rename** (item 2.1) — tight, mechanical, biggest win for cohesion.
2. **JS card class rename + tag shim** (item 2.2) — needs a couple of test runs in a real browser.
3. **Deprecation surfaces audit** (item 2.3) — write the unified persistent notification body, add the missing per-surface warnings.
4. **Multi-install / camera migration tests** (items 5.1, 5.2) — strengthens the test suite.
5. **Manual smoke test** (item 3.1) — once items 1–4 land, before tagging.
6. **Annex investigation lands** (item 1.1) — adds Plan 4 / annex disambiguation.
7. **Release-day ops** (item 4) — repo rename, release notes, hacs PR.

Items 1–5 can land on the `migration` branch now. Items 6 and 7 are gated externally.

---

## Updates / log

- 2026-05-07 — initial document
- 2026-05-07 — scope expanded per user: vendored library and JS classes are in scope; only shims are exempt, and shims must notify users of their deprecation.
- 2026-05-07 — decisions locked: vendored library directory renamed to `verisure_owa_api/`; vendored library rename uses no compat shim (path 1); JS legacy tag stays registered for compat but is NOT added to `window.customCards` so the picker shows only the new card; deprecation warning fires via console only.
