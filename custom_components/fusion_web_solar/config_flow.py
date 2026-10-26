"""Config Flow der Integration fusion_web_solar."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    FusionSolarAuthError,
    FusionSolarConnectionError,
    FusionSolarError,
    FusionSolarWebApi,
)
from .const import (
    CONF_BASE_URL,
    CONF_COOKIES,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_STATION_DN,
    CONF_TIMEZONE_OFFSET,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEZONE_OFFSET,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    NAME,
)

_LOGGER = logging.getLogger(__name__)


def _normalize_base_url(value: str | None) -> str:
    """Akzeptiert auch "uni002eu5.fusionsolar.huawei.com" ohne Schema."""
    value = (value or "").strip().rstrip("/")
    if not value:
        return DEFAULT_BASE_URL
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


class FusionWebSolarConfigFlow(ConfigFlow, domain=DOMAIN):
    """Einrichtung ueber die Home-Assistant-Oberflaeche."""

    VERSION = 1

    def __init__(self) -> None:
        self._base_url = DEFAULT_BASE_URL
        self._auth: dict[str, Any] = {}
        self._stations: list[dict[str, Any]] = []
        self._reauth_entry: ConfigEntry | None = None

    # -- Einstieg -------------------------------------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="user", menu_options=["password", "cookies"]
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Erneut anmelden, wenn die Session nicht mehr erneuert werden kann."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context.get("entry_id", "")
        )
        if self._reauth_entry:
            self._base_url = self._reauth_entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL)
            self._auth = {
                CONF_EMAIL: self._reauth_entry.data.get(CONF_EMAIL),
            }
        return await self.async_step_password()

    # -- Anmeldung mit Passwort ----------------------------------------
    async def async_step_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._base_url = _normalize_base_url(user_input[CONF_BASE_URL])
            self._auth = {
                CONF_BASE_URL: self._base_url,
                CONF_EMAIL: user_input[CONF_EMAIL],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            errors = await self._async_try_login()

            if not errors:
                return await self._async_finish()

        schema = self.add_suggested_values_to_schema(
            vol.Schema(
                {
                    vol.Required(CONF_BASE_URL, default=DEFAULT_BASE_URL): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.URL)
                    ),
                    vol.Required(CONF_EMAIL): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.EMAIL)
                    ),
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            self._auth,
        )
        return self.async_show_form(
            step_id="password", data_schema=schema, errors=errors
        )

    # -- Anmeldung mit Browser-Cookies ---------------------------------
    async def async_step_cookies(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._base_url = _normalize_base_url(user_input[CONF_BASE_URL])
            self._auth = {
                CONF_BASE_URL: self._base_url,
                CONF_COOKIES: user_input[CONF_COOKIES],
            }
            errors = await self._async_try_login()

            if not errors:
                return await self._async_finish()

        schema = self.add_suggested_values_to_schema(
            vol.Schema(
                {
                    vol.Required(CONF_BASE_URL, default=DEFAULT_BASE_URL): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.URL)
                    ),
                    vol.Required(CONF_COOKIES): TextSelector(
                        TextSelectorConfig(multiline=True)
                    ),
                }
            ),
            self._auth,
        )
        return self.async_show_form(
            step_id="cookies",
            data_schema=schema,
            errors=errors,
        )

    # -- Anlagenauswahl -------------------------------------------------
    async def async_step_station(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            dn = user_input[CONF_STATION_DN]
            station = next(
                (item for item in self._stations if item.get("dn") == dn), None
            )
            if station:
                return await self._async_create(station)

        options = [
            SelectOptionDict(
                value=station["dn"],
                label=station.get("name") or station["dn"],
            )
            for station in self._stations
            if station.get("dn")
        ]
        return self.async_show_form(
            step_id="station",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STATION_DN): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
        )

    # -- Helfer ---------------------------------------------------------
    async def _async_try_login(self) -> dict[str, str]:
        """Zugangsdaten pruefen und Anlagen laden."""
        session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        try:
            api = FusionSolarWebApi(
                session,
                base_url=self._base_url,
                email=self._auth.get(CONF_EMAIL),
                password=self._auth.get(CONF_PASSWORD),
                cookies=self._auth.get(CONF_COOKIES),
            )
            self._stations = await api.async_get_stations()
        except FusionSolarAuthError as err:
            _LOGGER.debug("Login fehlgeschlagen: %s", err)
            message = str(err)
            if "captcha_required" in message:
                return {"base": "captcha_required"}
            if "two_factor" in message:
                return {"base": "two_factor"}
            return {"base": "invalid_auth"}
        except FusionSolarConnectionError as err:
            _LOGGER.warning("Verbindungsfehler: %s", err)
            return {"base": "cannot_connect"}
        except FusionSolarError as err:
            _LOGGER.warning("Unerwarteter Fehler: %s", err)
            return {"base": "unknown"}
        finally:
            await session.close()

        if not self._stations:
            return {"base": "no_stations"}
        return {}

    async def _async_finish(self) -> ConfigFlowResult:
        if len(self._stations) == 1:
            return await self._async_create(self._stations[0])
        return await self.async_step_station()

    async def _async_create(self, station: dict[str, Any]) -> ConfigFlowResult:
        data = {
            **{key: value for key, value in self._auth.items() if value},
            CONF_STATION_DN: station["dn"],
            CONF_BASE_URL: self._base_url,
            CONF_TIMEZONE_OFFSET: DEFAULT_TIMEZONE_OFFSET,
        }

        if self._reauth_entry:
            return self.async_update_reload_and_abort(self._reauth_entry, data=data)

        await self.async_set_unique_id(station["dn"])
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=station.get("name") or NAME,
            data=data,
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return FusionWebSolarOptionsFlow()


class FusionWebSolarOptionsFlow(OptionsFlow):
    """Nur das Abfrageintervall ist einstellbar."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=current
                    ): vol.All(
                        cv.positive_int,
                        vol.Range(min=MIN_SCAN_INTERVAL, max=3600),
                    )
                }
            ),
        )
