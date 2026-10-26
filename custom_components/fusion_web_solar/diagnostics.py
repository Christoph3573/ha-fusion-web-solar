"""Diagnose-Daten fuer die Integration fusion_web_solar."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

REDACTED = "**REDACTED**"
SECRET_KEYS = ("password", "cookies")


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Anonymisierter Zustand fuer Fehlermeldungen."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)

    entry_data = {
        key: (REDACTED if key in SECRET_KEYS else value)
        for key, value in entry.data.items()
    }

    return {
        "entry": {
            "title": entry.title,
            "data": entry_data,
            "options": dict(entry.options),
        },
        "last_update_success": getattr(coordinator, "last_update_success", None),
        "update_interval": str(getattr(coordinator, "update_interval", None)),
        "data": getattr(coordinator, "data", None),
    }
