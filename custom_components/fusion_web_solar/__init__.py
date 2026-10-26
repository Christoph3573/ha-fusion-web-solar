"""Setup der Integration fusion_web_solar."""

from __future__ import annotations

import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import FusionSolarWebApi
from .const import (
    CONF_BASE_URL,
    CONF_COOKIES,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_TIMEZONE_OFFSET,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEZONE_OFFSET,
    DOMAIN,
)
from .coordinator import FusionSolarCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Integration aus einem Config Entry starten."""
    session = async_create_clientsession(
        hass, cookie_jar=aiohttp.CookieJar(unsafe=True)
    )
    api = FusionSolarWebApi(
        session,
        base_url=entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        email=entry.data.get(CONF_EMAIL),
        password=entry.data.get(CONF_PASSWORD),
        cookies=entry.data.get(CONF_COOKIES),
        timezone_offset=int(
            entry.data.get(CONF_TIMEZONE_OFFSET, DEFAULT_TIMEZONE_OFFSET)
        ),
    )

    coordinator = FusionSolarCoordinator(
        hass,
        entry,
        api,
        int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
    )

    # Loest bei Auth-Fehlern automatisch einen Reauth-Flow aus und macht aus
    # Verbindungsfehlern ein ConfigEntryNotReady.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Integration entladen."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Neu laden, wenn Optionen geaendert wurden."""
    await hass.config_entries.async_reload(entry.entry_id)
