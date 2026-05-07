"""Legacy 'securitas' shim — migrates config entries to 'verisure_owa'.

Removed entirely in v6.0.0. Until then, any user upgrading from a pre-v5
install lands in this shim, which moves their state to verisure_owa and
removes itself.
"""

from __future__ import annotations

import logging

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DOMAIN = "securitas"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """No YAML config to set up — entries handled in async_setup_entry."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a legacy 'securitas' config entry to 'verisure_owa'.

    Returns False because we never actually set up the legacy entry —
    we replace it with a new one under verisure_owa and remove this one.
    """
    from custom_components.verisure_owa.migrate import migrate_legacy_entry

    _LOGGER.warning(
        "The 'securitas' integration is deprecated. Migrating config entry %s "
        "to 'verisure_owa'. The legacy 'securitas' shim will be removed entirely "
        "in v6.0.0 (~6 months from now).",
        entry.entry_id,
    )
    await migrate_legacy_entry(hass, entry)

    # Inform the user they need to restart for the migrated integration to come up.
    # Body lists every deprecated surface so users can grep their config and update
    # at their pace within the v5 deprecation window.
    persistent_notification.async_create(
        hass,
        message=(
            "Your Securitas Direct integration has been migrated to Verisure OWA. "
            "**Please restart Home Assistant** to complete the upgrade. "
            "All your devices, entities, and customizations are preserved.\n\n"
            "**The following will be removed in v6.0.0 (~6 months from now). "
            "Update your automations and dashboards at your pace:**\n\n"
            "- Service calls: `securitas.force_arm` / `securitas.force_arm_cancel` "
            "→ use `verisure_owa.force_arm` / `verisure_owa.force_arm_cancel`\n"
            "- Events: `securitas_arming_exception` "
            "→ use `verisure_owa_arming_exception`\n"
            "- Events: `securitas_activity` "
            "→ use `verisure_owa_activity`\n"
            "- Lovelace card URLs: `/securitas_panel/...` "
            "→ use `/verisure_owa_panel/...`\n"
            "- Lovelace card types: `custom:securitas-alarm-card` (and `-badge`, "
            "`-chip`, `-camera-card`, `-events-card`) "
            "→ use `custom:verisure-owa-alarm-card` (and matching new names)"
        ),
        title="Verisure OWA migration: restart required",
        notification_id="verisure_owa_migration_complete",
    )

    # Remove the legacy entry now that its state has been moved.
    hass.async_create_task(hass.config_entries.async_remove(entry.entry_id))
    return False


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Nothing to unload — the entry was never set up."""
    return True
