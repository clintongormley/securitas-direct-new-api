# Lock API Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix lock GraphQL queries to match the Securitas Direct website and remove unused Danalock API code.

**Architecture:** Replace the `xSGetSmartlockConfig` array-parameter query with flat scalar parameters matching the website HAR. Parse features from the SmartLock config response. Remove all Danalock config code (`xSGetDanalockConfig`, `xSGetDanalockConfigStatus`, `DanalockConfig` dataclass, and related methods/tests).

**Tech Stack:** Python 3.13, Home Assistant custom integration, pytest

**Working directory:** `/workspaces/ha-dev/securitas-direct-new-api/.worktrees/locks`

**Spec:** `docs/superpowers/specs/2026-03-20-lock-api-alignment-design.md`

**Run tests with:** `cd /workspaces/ha-dev/securitas-direct-new-api/.worktrees/locks && python -m pytest tests/ -x -v`

---

### Task 1: Rename Danalock dataclasses to generic Lock names

**Files:**
- Modify: `custom_components/securitas/securitas_direct_new_api/dataTypes.py`

- [ ] **Step 1: Rename DanalockAutolock to LockAutolock**

In `dataTypes.py`, change lines 163-168:

```python
@dataclass
class LockAutolock:
    """Lock auto-lock configuration."""

    active: bool | None = None
    timeout: str | int | None = None
```

Note: the API returns `timeout` as a string (e.g. `"1800"`) per the website HAR.
Change the type from `int | None` to `str | int | None` to match reality.

- [ ] **Step 2: Rename DanalockFeatures to LockFeatures, update reference**

Change lines 171-177:

```python
@dataclass
class LockFeatures:
    """Lock feature configuration."""

    holdBackLatchTime: int = 0
    calibrationType: int = 0
    autolock: LockAutolock | None = None
```

- [ ] **Step 3: Add features field to SmartLock, remove type field**

Change the `SmartLock` dataclass (lines 128-141) to:

```python
@dataclass
class SmartLock:
    """Smart lock discovery response."""

    res: str | None = None
    location: str | None = None
    deviceId: str = ""
    referenceId: str = ""
    zoneId: str = ""
    serialNumber: str = ""
    family: str = ""
    label: str = ""
    features: LockFeatures | None = None
```

Note: `type` field removed (website doesn't request it). `features` field added.

- [ ] **Step 4: Delete DanalockConfig dataclass**

Delete lines 180-194 (the entire `DanalockConfig` class).

- [ ] **Step 5: Run tests to see what breaks**

Run: `python -m pytest tests/ -x -v 2>&1 | head -60`

Expected: Several import errors and test failures referencing `DanalockConfig`, `DanalockFeatures`, `DanalockAutolock`.

- [ ] **Step 6: Commit**

```bash
git add custom_components/securitas/securitas_direct_new_api/dataTypes.py
git commit -m "refactor: rename Danalock dataclasses to LockFeatures/LockAutolock

Rename DanalockAutolock → LockAutolock, DanalockFeatures → LockFeatures.
Add features field to SmartLock. Remove SmartLock.type (not used by website).
Delete DanalockConfig dataclass."
```

---

### Task 2: Fix GraphQL queries

**Files:**
- Modify: `custom_components/securitas/securitas_direct_new_api/graphql_queries.py`

- [ ] **Step 1: Replace SMARTLOCK_CONFIG_QUERY with flat params**

Replace lines 151-159 with:

```python
SMARTLOCK_CONFIG_QUERY = (
    "query xSGetSmartlockConfig($numinst: String!, $panel: String!, "
    "$deviceId: String, $keytype: String, $deviceType: String) {\n"
    "  xSGetSmartlockConfig(\n    numinst: $numinst\n    panel: $panel\n"
    "    deviceId: $deviceId\n    keytype: $keytype\n"
    "    deviceType: $deviceType\n  ) {\n    res\n    referenceId\n"
    "    zoneId\n    serialNumber\n    location\n    family\n    label\n"
    "    features {\n      holdBackLatchTime\n      calibrationType\n"
    "      autolock {\n        active\n        timeout\n      }\n"
    "    }\n  }\n}"
)
```

- [ ] **Step 2: Delete DANALOCK_CONFIG_STATUS_QUERY**

Delete lines 177-188 (`DANALOCK_CONFIG_STATUS_QUERY`).

- [ ] **Step 3: Delete DANALOCK_CONFIG_QUERY**

Delete lines 216-222 (`DANALOCK_CONFIG_QUERY`).

- [ ] **Step 4: Commit**

```bash
git add custom_components/securitas/securitas_direct_new_api/graphql_queries.py
git commit -m "fix: align xSGetSmartlockConfig query with website HAR

Use flat scalar parameters (deviceId, keytype, deviceType) instead of
the devices array. Drop type from selection set. Delete Danalock queries."
```

---

### Task 3: Fix apimanager — update get_smart_lock_config, delete Danalock methods

**Files:**
- Modify: `custom_components/securitas/securitas_direct_new_api/apimanager.py`

- [ ] **Step 1: Update get_smart_lock_config to send flat params and parse features**

Replace lines 779-815 with:

```python
    async def get_smart_lock_config(
        self, installation: Installation, device_id: str = SMARTLOCK_DEVICE_ID
    ) -> SmartLock:
        """Fetch smart lock configuration for the installation."""
        content = {
            "operationName": "xSGetSmartlockConfig",
            "variables": {
                "numinst": installation.number,
                "panel": installation.panel,
                "deviceType": SMARTLOCK_DEVICE_TYPE,
                "deviceId": device_id,
                "keytype": SMARTLOCK_KEY_TYPE,
            },
            "query": SMARTLOCK_CONFIG_QUERY,
        }
        await self._ensure_auth(installation)
        response = await self._execute_request(
            content, "xSGetSmartlockConfig", installation
        )

        raw_data = response.get("data", {}).get("xSGetSmartlockConfig")
        if raw_data is None:
            return SmartLock()

        features = None
        if raw_features := raw_data.get("features"):
            autolock = None
            if raw_autolock := raw_features.get("autolock"):
                autolock = LockAutolock(
                    active=raw_autolock.get("active"),
                    timeout=raw_autolock.get("timeout"),
                )
            features = LockFeatures(
                holdBackLatchTime=raw_features.get("holdBackLatchTime", 0),
                calibrationType=raw_features.get("calibrationType", 0),
                autolock=autolock,
            )

        return SmartLock(
            res=raw_data.get("res"),
            location=raw_data.get("location"),
            referenceId=raw_data.get("referenceId", ""),
            zoneId=raw_data.get("zoneId", ""),
            serialNumber=raw_data.get("serialNumber", ""),
            family=raw_data.get("family", ""),
            label=raw_data.get("label", ""),
            features=features,
        )
```

- [ ] **Step 2: Update imports at top of apimanager.py**

Add `LockAutolock, LockFeatures` to the import from `dataTypes`. Remove `DanalockAutolock, DanalockConfig, DanalockFeatures`. Remove `DANALOCK_CONFIG_QUERY, DANALOCK_CONFIG_STATUS_QUERY` from graphql_queries import.

- [ ] **Step 3: Delete Danalock methods**

Delete these methods entirely:
- `get_danalock_config()` (lines 876-903)
- `check_danalock_config_status()` (lines 905-923)
- `_parse_danalock_config()` (lines 925-953)
- `submit_danalock_config_request()` (lines 1033-1052)

- [ ] **Step 4: Commit**

```bash
git add custom_components/securitas/securitas_direct_new_api/apimanager.py
git commit -m "fix: send flat params for xSGetSmartlockConfig, parse features

Match the website HAR: send deviceId/deviceType/keytype as flat variables
instead of a devices array. Parse features into SmartLock.features.
Remove all Danalock API methods."
```

---

### Task 4: Update __init__.py re-exports

**Files:**
- Modify: `custom_components/securitas/securitas_direct_new_api/__init__.py`

- [ ] **Step 1: Replace DanalockConfig with LockFeatures in imports**

Change the `dataTypes` import block (lines 18-32) — remove `DanalockConfig`, add `LockFeatures`:

```python
from .dataTypes import (  # noqa: F401
    Attribute,
    Attributes,
    CameraDevice,
    Installation,
    LockFeatures,
    OperationStatus,
    OtpPhone,
    Service,
    SStatus,
    SmartLock,
    SmartLockMode,
    SmartLockModeStatus,
    ThumbnailResponse,
)
```

- [ ] **Step 2: Commit**

```bash
git add custom_components/securitas/securitas_direct_new_api/__init__.py
git commit -m "refactor: update re-exports — replace DanalockConfig with LockFeatures"
```

---

### Task 5: Delete hub.get_danalock_config

**Files:**
- Modify: `custom_components/securitas/hub.py`

- [ ] **Step 1: Delete get_danalock_config method**

Delete lines 685-694 (the `get_danalock_config` method).

- [ ] **Step 2: Commit**

```bash
git add custom_components/securitas/hub.py
git commit -m "refactor: remove hub.get_danalock_config"
```

---

### Task 6: Rewrite lock.py — remove Danalock, use SmartLock features

**Files:**
- Modify: `custom_components/securitas/lock.py`

- [ ] **Step 1: Update imports**

Replace the import block (lines 24-29):

```python
from .securitas_direct_new_api import (
    Installation,
    SecuritasDirectError,
    SmartLock,
)
```

Remove `DanalockConfig` from imports.

- [ ] **Step 2: Remove Danalock fields from __init__**

Remove `_MAX_DANALOCK_RETRIES` (line 62), `danalock_config` parameter (line 71),
`_danalock_config` field (line 85), and `_danalock_config_retries` (lines 86-88).

The `__init__` signature becomes:

```python
    def __init__(
        self,
        installation: Installation,
        client: SecuritasHub,
        hass: HomeAssistant,
        device_id: str = SMARTLOCK_DEVICE_ID,
        initial_status: str = LOCK_STATUS_LOCKED,
        lock_config: SmartLock | None = None,
    ) -> None:
```

- [ ] **Step 3: Remove Danalock lazy-fetch from async_update_status**

Delete lines 150-178 (the entire Danalock config lazy-fetch block). The method becomes:

```python
    async def async_update_status(self, _now=None) -> None:
        """Poll lock status from the API."""
        if self.hass is None:
            return

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

        if _now is not None:
            self.async_write_ha_state()
```

- [ ] **Step 4: Rewrite extra_state_attributes to use SmartLock features**

Replace lines 229-244 with:

```python
    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:  # type: ignore[override]
        """Return lock configuration as state attributes."""
        attrs: dict[str, Any] = {}
        cfg = self._lock_config
        if cfg and cfg.features:
            attrs["hold_back_latch_time"] = cfg.features.holdBackLatchTime
            if cfg.features.autolock:
                attrs["autolock_active"] = cfg.features.autolock.active
                attrs["autolock_timeout"] = cfg.features.autolock.timeout
        return attrs
```

- [ ] **Step 5: Rewrite supported_features to use SmartLock features**

Replace lines 296-304 with:

```python
    @property
    def supported_features(self) -> lock.LockEntityFeature:  # type: ignore[override]
        """Return the list of supported features."""
        cfg = self._lock_config
        if (
            cfg
            and cfg.features
            and cfg.features.holdBackLatchTime
            and cfg.features.holdBackLatchTime > 0
        ):
            return lock.LockEntityFeature.OPEN
        return lock.LockEntityFeature(0)
```

- [ ] **Step 6: Commit**

```bash
git add custom_components/securitas/lock.py
git commit -m "fix: use SmartLock features for holdBackLatchTime, remove Danalock

Use lock_config.features (from xSGetSmartlockConfig) as the sole source
for holdBackLatchTime and the OPEN feature. Remove all Danalock config
fields, lazy-fetch retry logic, and Danalock-sourced state attributes."
```

---

### Task 7: Update tests — mock_graphql.py and test_services.py

**Files:**
- Modify: `tests/mock_graphql.py`
- Modify: `tests/test_services.py`

- [ ] **Step 1: Delete Danalock helpers from mock_graphql.py**

Delete `graphql_danalock_config()` (lines 574-584) and
`graphql_danalock_config_status()` (lines 587-634).

- [ ] **Step 2: Delete Danalock test from test_services.py**

Delete `test_submit_danalock_config_request_returns_reference_id` (lines 787-802).

- [ ] **Step 3: Commit**

```bash
git add tests/mock_graphql.py tests/test_services.py
git commit -m "test: remove Danalock mock helpers and test cases"
```

---

### Task 8: Update test_smart_lock.py

**Files:**
- Modify: `tests/test_smart_lock.py`

- [ ] **Step 1: Update imports**

Replace `DanalockConfig` with `LockFeatures, LockAutolock` in the import on line 7.

- [ ] **Step 2: Delete TestGetDanalockConfig class**

Delete the entire `TestGetDanalockConfig` class (lines 292 to end of file, approximately lines 292-490).

- [ ] **Step 3: Update existing tests for flat params and removed `type` field**

In `test_success_returns_all_fields` (line 36):
- Remove `"type": 1` from mock data (line 44)
- Remove `assert result.type == 1` (line 59)
- Add `assert result.features is None` after the existing assertions

In `test_device_id_passed_to_query` (line 67):
- Remove `"type": 1` from mock data (line 75)
- Replace the assertion (lines 82-84):
  ```python
  call_args = mock_execute.call_args[0][0]
  assert call_args["variables"]["deviceId"] == "02"
  ```

In `test_missing_fields_use_defaults` (line 86):
- Remove `"type": 2` from mock data (line 94)

In `test_error_in_response_returns_empty_smart_lock` (line 108):
- Remove `assert result.type is None` (line 118)

In `test_none_data_returns_empty_smart_lock` (line 120):
- Remove `assert result.type is None` (line 130)

In `test_no_data_key_returns_empty_smart_lock` (line 132):
- Remove `assert result.type is None` (line 142)

- [ ] **Step 4: Add test for SmartLock features parsing**

Add a test that verifies `get_smart_lock_config()` correctly parses `features` from the response:

```python
    async def test_features_parsed_from_response(self, authed_api, installation):
        """Features from xSGetSmartlockConfig are parsed into SmartLock."""
        authed_api._execute_request = AsyncMock(
            return_value={
                "data": {
                    "xSGetSmartlockConfig": {
                        "res": "OK",
                        "referenceId": None,
                        "zoneId": "DR02",
                        "serialNumber": "326V8W84",
                        "location": "Pl_0_Hall",
                        "family": "User",
                        "label": "Cerradura",
                        "features": {
                            "holdBackLatchTime": 3,
                            "calibrationType": 0,
                            "autolock": {
                                "active": True,
                                "timeout": "1800",
                            },
                        },
                    }
                }
            }
        )
        result = await authed_api.get_smart_lock_config(installation, "02")
        assert result.features is not None
        assert result.features.holdBackLatchTime == 3
        assert result.features.calibrationType == 0
        assert result.features.autolock is not None
        assert result.features.autolock.active is True
        assert result.features.autolock.timeout == "1800"
```

- [ ] **Step 5: Add test for SmartLock with no features**

```python
    async def test_no_features_in_response(self, authed_api, installation):
        """SmartLock with no features field returns features=None."""
        authed_api._execute_request = AsyncMock(
            return_value={
                "data": {
                    "xSGetSmartlockConfig": {
                        "res": "OK",
                        "referenceId": None,
                        "zoneId": "DR01",
                        "serialNumber": "ABC123",
                        "location": "Hall",
                        "family": "User",
                        "label": "Lock",
                        "features": None,
                    }
                }
            }
        )
        result = await authed_api.get_smart_lock_config(installation, "01")
        assert result.features is None
```

- [ ] **Step 6: Run all tests**

Run: `python -m pytest tests/ -x -v`

Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add tests/test_smart_lock.py
git commit -m "test: update SmartLock tests for flat params and features parsing

Remove DanalockConfig tests. Add tests for features parsing from
xSGetSmartlockConfig response. Update mock variables to flat params."
```

---

### Task 9: Update architecture docs

**Files:**
- Modify: `docs/architecture.md`

- [ ] **Step 1: Replace Danalock references**

Search for `DanalockConfig`, `DanalockFeatures`, `DanalockAutolock`,
`get_danalock_config` in `docs/architecture.md` and replace with the new names
(`LockFeatures`, `LockAutolock`) or remove references to deleted methods.

- [ ] **Step 2: Run full test suite one final time**

Run: `python -m pytest tests/ -x -v`

Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add docs/architecture.md
git commit -m "docs: update architecture for lock API alignment"
```
