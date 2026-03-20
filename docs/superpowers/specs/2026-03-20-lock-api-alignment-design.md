# Lock API alignment with Securitas website

## Problem

The lock implementation uses GraphQL queries that don't match what the Securitas
Direct website actually sends. This causes lock operations to fail or produce
incomplete data for users.

Three issues identified by comparing HAR logs from the website with our code:

1. **`xSGetSmartlockConfig` query shape mismatch** — the website sends flat
   scalar parameters (`deviceId`, `deviceType`, `keytype`), but our code wraps
   them in a `devices: [SmartlockDevicesInfo]!` array. This may cause the query
   to return null for some lock models.

2. **Features data discarded** — `xSGetSmartlockConfig` already returns
   `features { holdBackLatchTime, calibrationType, autolock { active, timeout }}`
   but `get_smart_lock_config()` ignores it. This is the primary source for
   `holdBackLatchTime` which controls the `OPEN` (unlatch) feature.

3. **Danalock config never used by the website** — `xSGetDanalockConfig` and
   `xSGetDanalockConfigStatus` are never called by the Securitas website. Git
   history confirms these were our own invention (commit `25bedaf`), not based on
   observed traffic. The website gets all lock features from `xSGetSmartlockConfig`.

## Approach

Drop Danalock entirely. Use `xSGetSmartlockConfig` with the correct query shape
as the sole source of lock configuration and features.

## Breaking changes

The lock entity previously exposed six `extra_state_attributes` sourced from
`DanalockConfig`: `battery_low_threshold`, `lock_before_full_arm`,
`lock_before_partial_arm`, `lock_before_perimeter_arm`, `unlock_after_disarm`,
and `auto_lock_time`. These are removed. Risk is negligible because the Danalock
config fetch failed for every known user (the API endpoint was speculative), so
these attributes were always empty or missing in practice.

## Changes

### graphql_queries.py

Replace `SMARTLOCK_CONFIG_QUERY` — change from `$devices: [SmartlockDevicesInfo]!`
array parameter to flat scalar parameters:

```graphql
query xSGetSmartlockConfig($numinst: String!, $panel: String!,
    $deviceId: String, $keytype: String, $deviceType: String) {
  xSGetSmartlockConfig(numinst: $numinst, panel: $panel,
    deviceId: $deviceId, keytype: $keytype, deviceType: $deviceType) {
    res referenceId zoneId serialNumber location family label
    features { holdBackLatchTime calibrationType autolock { active timeout } }
  }
}
```

Note: the existing query also selects a `type` field. The website HAR does not
request it. Drop `type` from the query and remove `SmartLock.type` from the
dataclass.

Delete `DANALOCK_CONFIG_QUERY` and `DANALOCK_CONFIG_STATUS_QUERY`.

### dataTypes.py

Rename `DanalockAutolock` to `LockAutolock` and `DanalockFeatures` to
`LockFeatures`. Same fields, generic names since they're shared between the
SmartLock config response and the (now removed) Danalock config response.

Add `features: LockFeatures | None = None` to the `SmartLock` dataclass.

Delete the `DanalockConfig` dataclass.

### apimanager.py

**`get_smart_lock_config()`** — send flat variables (`numinst`, `panel`,
`deviceType`, `deviceId`, `keytype`) instead of the `devices` array. Parse the
`features` nested object from the response into `SmartLock.features`.

**Delete** `get_danalock_config()`, `submit_danalock_config_request()`,
`check_danalock_config_status()`, and `_parse_danalock_config()`.

### hub.py

Delete `get_danalock_config()` method.

### lock.py

Remove all Danalock config references:
- Delete `_MAX_DANALOCK_RETRIES`, `_danalock_config` field,
  `_danalock_config_retries`, and the lazy-fetch logic in `async_update_status`.
- Use `lock_config.features` (from `SmartLock`) as the source for
  `holdBackLatchTime` and `LockEntityFeature.OPEN`. The features check happens
  at construction time — `lock_config` is fetched eagerly during `_discover_locks`.
  No retry mechanism needed; if the config fetch fails, the lock still works for
  lock/unlock, it just won't expose the `OPEN` feature.
- Remove `extra_state_attributes` that came from `DanalockConfig`
  (`battery_low_threshold`, `lock_before_full_arm`, `lock_before_partial_arm`,
  `lock_before_perimeter_arm`, `unlock_after_disarm`, `auto_lock_time`).
- Keep attributes derivable from `SmartLock.features`: `hold_back_latch_time`,
  `autolock_active`, `autolock_timeout`. Return empty dict when `lock_config` or
  `lock_config.features` is `None`.

### __init__.py re-exports

Remove `DanalockConfig` from re-exports. Add `LockFeatures`, `LockAutolock` if
needed by external consumers (lock.py imports from the sub-package).

### docs/architecture.md

Update references to `DanalockConfig`, `DanalockFeatures`, `DanalockAutolock`,
and `get_danalock_config()` to reflect the new structure.

### Tests

- `tests/test_smart_lock.py`: use `LockFeatures`/`LockAutolock` instead of
  `DanalockFeatures`/`DanalockAutolock`. Remove Danalock config test cases. Add
  test for `SmartLock` with features parsed from the config response. Update the
  `xSGetSmartlockConfig` mock to use flat params.
- `tests/mock_graphql.py`: delete `graphql_danalock_config()` and
  `graphql_danalock_config_status()` helpers.
- `tests/test_services.py`: delete
  `test_submit_danalock_config_request_returns_reference_id`.
- `tests/test_hub.py`: remove Danalock-related test cases if any.
