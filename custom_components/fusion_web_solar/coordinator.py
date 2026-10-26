"""Datenabruf fuer die FusionSolar-Integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    FusionSolarAuthError,
    FusionSolarConnectionError,
    FusionSolarError,
    FusionSolarWebApi,
)
from .const import CONF_STATION_DN, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class FusionSolarCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Holt die Messwerte im konfigurierten Intervall."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: FusionSolarWebApi,
        update_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=update_interval),
        )
        self.entry = entry
        self.api = api
        self.station_dn: str | None = entry.data.get(CONF_STATION_DN)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.api.async_get_snapshot(self.station_dn)
        except FusionSolarAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except FusionSolarConnectionError as err:
            raise UpdateFailed(f"Verbindungsfehler: {err}") from err
        except FusionSolarError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:  # noqa: BLE001 - Coordinator soll nicht abstuerzen
            raise UpdateFailed(f"Unerwarteter Fehler: {err}") from err
