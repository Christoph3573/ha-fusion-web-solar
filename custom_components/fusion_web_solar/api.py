"""Client fuer die FusionSolar-Weboberflaeche.

Die offizielle Northbound-API liefert keine Energiefluss-Daten und keinen
Hausverbrauch. Diese Integration nutzt deshalb - wie die Weboberflaeche
selbst - die internen REST-Endpunkte:

  * /unisso/...                                  Login (UniSSO)
  * /rest/pvms/web/station/v3/.../energy-flow    PV, Haus, Batterie, Netz, Pfeile
  * /rest/pvms/web/station/v1/.../station-real-kpi   Tageswerte
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.parse
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from yarl import URL

from .const import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEZONE_OFFSET,
    FLOW_ORDER,
    NODE_ROLES,
)

_LOGGER = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:158.0) Firefox/158.0"
)

MAX_CHUNK_CHARS = 270
CHUNK_SEPARATOR = "00000001"


class FusionSolarError(Exception):
    """Basisklasse."""


class FusionSolarAuthError(FusionSolarError):
    """Zugangsdaten ungueltig, CAPTCHA oder 2FA aktiv."""


class FusionSolarSessionExpired(FusionSolarError):
    """Session abgelaufen, Login-Weiterleitung erhalten."""


class FusionSolarConnectionError(FusionSolarError):
    """Netzwerk- oder Serverfehler."""


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def sso_host_for(base_url: str) -> str:
    """uni002eu5.fusionsolar.huawei.com -> eu5.fusionsolar.huawei.com"""
    host = URL(base_url).host or ""
    if host.startswith("uni"):
        parts = host.split(".")
        zone = parts[0][3:].lstrip("0")
        for index, char in enumerate(zone):
            if char.isalpha():
                parts[0] = zone[index:]
                break
        host = ".".join(parts)
    return f"https://{host}"


def normalize_cookie(raw: str) -> str:
    """Akzeptiert 'a=1; b=2', 'Cookie: a=1; b=2' oder mehrzeilige Header."""
    raw = (raw or "").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1]
    parts = []
    for chunk in raw.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if "=" in chunk:
            name, value = chunk.split("=", 1)
            parts.append(f"{name.strip()}={value.strip()}")
    return "; ".join(parts)


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_kw(text: Any) -> float | None:
    if not text:
        return None
    return _num(str(text).replace("kW", "").replace(",", ".").strip())


def _encrypt_password(pub_key_pem: str, version: str, password: str) -> str:
    """RSA-OAEP-SHA384, chunkweise, Bloecke mit '00000001' verbunden.

    Entspricht dem Login-JavaScript der FusionSolar-Weboberflaeche.
    """
    public_key = serialization.load_pem_public_key(pub_key_pem.encode())
    encoded = urllib.parse.quote(password, safe="")
    chunks = [
        encoded[i:i + MAX_CHUNK_CHARS]
        for i in range(0, len(encoded), MAX_CHUNK_CHARS)
    ] or [""]

    parts: list[str] = []
    for index, chunk in enumerate(chunks):
        cipher = public_key.encrypt(
            chunk.encode(),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA384()),
                algorithm=hashes.SHA384(),
                label=None,
            ),
        )
        prefix = "" if index == 0 else CHUNK_SEPARATOR
        parts.append(prefix + base64.b64encode(cipher).decode())
    return "".join(parts) + version


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class FusionSolarWebApi:
    """Duenner async-Client um die Web-Endpunkte."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = DEFAULT_BASE_URL,
        email: str | None = None,
        password: str | None = None,
        cookies: str | None = None,
        timezone_offset: int = DEFAULT_TIMEZONE_OFFSET,
        timeout: int = 30,
    ) -> None:
        self._session = session
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.sso_url = sso_host_for(self.base_url)
        self._email = email
        self._password = password
        self._cookies = normalize_cookie(cookies) if cookies else None
        self._tz_offset = timezone_offset
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._apply_cookies()

    # -- Cookies --------------------------------------------------------
    def _apply_cookies(self) -> None:
        if not self._cookies:
            return
        jar = self._session.cookie_jar
        host_url = URL(self.base_url)
        for part in self._cookies.split("; "):
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            jar.update_cookies({name: value}, response_url=host_url)

    @property
    def can_relogin(self) -> bool:
        return bool(self._email and self._password)

    # -- HTTP -----------------------------------------------------------
    async def _request(
        self,
        method: str,
        url: str,
        data: dict | None = None,
        host_headers: bool = False,
    ) -> tuple[str, str]:
        headers = {
            "Accept": "application/json",
            "Accept-Language": "de,en-US;q=0.9,en;q=0.8",
            "User-Agent": USER_AGENT,
        }
        if host_headers:
            headers.update(
                {
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                    "x-non-renewal-session": "true",
                    "x-timezone-offset": str(self._tz_offset),
                    "Referer": (
                        self.base_url
                        + "/uniportal/pvmswebsite/assets/build/cloud.html"
                    ),
                }
            )

        payload = None
        if data is not None:
            payload = json.dumps(data)
            headers["Content-Type"] = "application/json;charset=UTF-8"

        try:
            async with self._session.request(
                method, url, data=payload, headers=headers, timeout=self._timeout
            ) as response:
                text = await response.text()
                status = response.status
                final_url = str(response.url)
        except asyncio.TimeoutError as err:
            raise FusionSolarConnectionError("Zeitueberschreitung") from err
        except aiohttp.ClientError as err:
            raise FusionSolarConnectionError(str(err)) from err

        if status in (401, 403) or "/unisso/login.action" in final_url:
            raise FusionSolarSessionExpired(f"HTTP {status}")
        if status >= 500:
            raise FusionSolarConnectionError(f"HTTP {status}")
        return text, final_url

    async def _json(
        self,
        method: str,
        url: str,
        data: dict | None = None,
        host_headers: bool = False,
    ) -> dict:
        text, _ = await self._request(method, url, data=data, host_headers=host_headers)
        try:
            result = json.loads(text)
        except ValueError as err:
            raise FusionSolarSessionExpired("Antwort ist kein JSON") from err
        if isinstance(result, dict) and result.get("success") is False:
            raise FusionSolarError(
                f"API-Fehler {result.get('failCode')}: {result.get('message') or ''}".strip()
            )
        return result

    # -- Login ----------------------------------------------------------
    async def async_login(self) -> None:
        if not self.can_relogin:
            raise FusionSolarAuthError("Keine Zugangsdaten hinterlegt")

        # Frische Session, damit ein alter CAPTCHA-Status nicht blockiert.
        self._session.cookie_jar.clear()
        self._cookies = None

        pubkey = await self._json("GET", f"{self.sso_url}/unisso/pubkey")
        password = (
            _encrypt_password(pubkey["pubKey"], pubkey["version"], self._password)
            if pubkey.get("enableEncrypt")
            else urllib.parse.quote(self._password or "", safe="")
        )

        service = (
            self.base_url
            + "/uniportal/pvmswebsite/assets/build/cloud.html"
            "?app-id=smartpvms&instance-id=smartpvms"
        )
        query = urllib.parse.urlencode(
            {
                "service": service,
                "timeStamp": str(pubkey.get("timeStamp", int(time.time() * 1000))),
                "nonce": _nonce(),
            }
        )
        endpoint = f"{self.sso_url}/unisso/v3/validateUser.action?{query}"

        result = await self._json(
            "POST",
            endpoint,
            data={
                "organizationName": "",
                "username": self._email,
                "password": password,
                "verifycode": "",
                "multiRegionName": "",
                "useFederationRouter": False,
                "scene": "",
            },
        )

        if result.get("twoFactorStatus"):
            raise FusionSolarAuthError(
                "two_factor: Zwei-Faktor-Anmeldung ist aktiv"
            )
        if result.get("verifyCodeCreate"):
            raise FusionSolarAuthError(
                "captcha_required: FusionSolar verlangt einen Sicherheitscode"
            )

        redirect = result.get("redirectURL")
        if not redirect:
            message = (
                result.get("errorMsg")
                or result.get("errorMessage")
                or "Anmeldung fehlgeschlagen"
            )
            raise FusionSolarAuthError(f"{message} (Code {result.get('errorCode')})")

        # SSO-Ticket gegen die Session der Weboberflaeche tauschen.
        await self._request("GET", redirect)
        await self._request(
            "GET",
            self.base_url
            + "/uniportal/pvmswebsite/assets/build/cloud.html"
            "?app-id=smartpvms&instance-id=smartpvms",
        )
        _LOGGER.debug("Login erfolgreich, Session fuer %s aktiv", self.base_url)

    # -- Endpunkte ------------------------------------------------------
    async def async_get_stations(self) -> list[dict]:
        """Anlagen des Kontos. Loest bei Bedarf einen Login aus."""
        try:
            return await self._station_list()
        except FusionSolarSessionExpired:
            if not self.can_relogin:
                raise FusionSolarAuthError("session_expired") from None
            await self.async_login()
            return await self._station_list()

    async def _station_list(self) -> list[dict]:
        result = await self._json(
            "POST",
            f"{self.base_url}/rest/pvms/web/station/v1/station/station-list",
            data={
                "curPage": 1,
                "pageSize": 50,
                "gridConnectedTime": "",
                "queryTime": int(time.time() * 1000),
                "timeZone": 1,
                "sortId": "createTime",
                "sortDir": "DESC",
                "locale": "de_DE",
            },
            host_headers=True,
        )
        return result.get("data", {}).get("list") or []

    async def async_get_snapshot(self, station_dn: str | None = None) -> dict:
        """Vollstaendiger Messwert-Satz fuer eine Anlage."""
        try:
            return await self._fetch_snapshot(station_dn)
        except FusionSolarSessionExpired:
            if not self.can_relogin:
                raise FusionSolarAuthError("session_expired") from None
            await self.async_login()
            return await self._fetch_snapshot(station_dn)

    async def _fetch_snapshot(self, station_dn: str | None) -> dict:
        stations = await self._station_list()
        if not stations:
            raise FusionSolarError("Keine Anlage im Konto gefunden")

        station = stations[0]
        if station_dn:
            for candidate in stations:
                if candidate.get("dn") == station_dn:
                    station = candidate
                    break

        dn = station["dn"]
        kpi = await self._real_kpi(dn)
        flow = await self._energy_flow(dn)
        return build_snapshot(station, kpi, flow)

    async def _real_kpi(self, station_dn: str) -> dict:
        query = urllib.parse.urlencode(
            {
                "stationDn": station_dn,
                "clientTime": int(time.time() * 1000),
                "timeZone": 1,
            }
        )
        result = await self._json(
            "GET",
            f"{self.base_url}/rest/pvms/web/station/v1/overview/station-real-kpi?{query}",
            host_headers=True,
        )
        return result.get("data", {})

    async def _energy_flow(self, station_dn: str) -> dict:
        query = urllib.parse.urlencode({"stationDn": station_dn, "featureId": "aifc"})
        result = await self._json(
            "GET",
            f"{self.base_url}/rest/pvms/web/station/v3/overview/energy-flow?{query}",
            host_headers=True,
        )
        _LOGGER.debug("energy-flow: %s", result.get("data"))
        return result.get("data", {})

    async def async_close(self) -> None:
        await self._session.close()


def _nonce() -> str:
    import os

    return os.urandom(16).hex()


# --------------------------------------------------------------------------
# Auswertung
# --------------------------------------------------------------------------

def build_snapshot(station: dict, kpi: dict, flow: dict) -> dict:
    """Rechnet die Energiefluss-Rohdaten in ein flaches Messwert-Dict um.

    Die API liefert nicht fuer jeden Pfeil einen Wert und beschriftet den
    Netz-Pfeil statisch als "Bezug". Die Richtung wird deshalb aus der
    Leistungsbilanz bestimmt, fehlende Pfeilwerte werden daraus ergaenzt.
    """
    nodes = {node["id"]: node for node in flow.get("flow", {}).get("nodes", [])}
    links = flow.get("flow", {}).get("links", [])

    by_role: dict[str, list[dict]] = {}
    for node in nodes.values():
        role = NODE_ROLES.get(node.get("mocId"))
        if role:
            by_role.setdefault(role, []).append(node)

    def role_of(node_id: str) -> str | None:
        node = nodes.get(node_id)
        return NODE_ROLES.get(node.get("mocId")) if node else None

    pv_kw = sum(_num(n.get("value"), 0.0) or 0.0 for n in by_role.get("pv", []))
    load_kw = sum(_num(n.get("value"), 0.0) or 0.0 for n in by_role.get("load", []))

    # --- Batterie -------------------------------------------------------
    battery_nodes = by_role.get("battery", [])
    battery_power = sum(_num(n.get("value"), 0.0) or 0.0 for n in battery_nodes)
    tips = (battery_nodes[0].get("deviceTips") or {}) if battery_nodes else {}
    soc = _num(tips.get("SOC"))
    if soc is None and battery_nodes:
        soc = _num((battery_nodes[0].get("customAttr") or {}).get("10006"))

    charging = True
    for link in links:
        roles = {role_of(link.get("fromNode")), role_of(link.get("toNode"))}
        if roles == {"inverter", "battery"}:
            charging = role_of(link.get("fromNode")) != "battery"
            break

    capacity = _num(station.get("batteryCapacity"))
    battery_charge = battery_power if charging else 0.0
    battery_discharge = 0.0 if charging else battery_power

    battery = {
        "soc_percent": soc,
        "power_kw": round(battery_power, 3),
        "state": (
            ("charging" if charging else "discharging")
            if battery_power > 0.001
            else "idle"
        ),
        "charge_kw": round(battery_charge, 3),
        "discharge_kw": round(battery_discharge, 3),
        "capacity_kwh": capacity,
        "stored_kwh": (
            round(soc / 100 * capacity, 2) if soc is not None and capacity else None
        ),
        "devices": len(battery_nodes),
        "mode": tips.get("CHARGE_MODE_VALUE"),
    }

    # --- Netz -----------------------------------------------------------
    grid_link = None
    for link in links:
        if "grid" in (role_of(link.get("fromNode")), role_of(link.get("toNode"))):
            grid_link = link
            break

    grid_kw = _parse_kw((grid_link or {}).get("description", {}).get("value"))
    grid_direction = None
    if grid_link:
        grid_direction = (
            "export" if role_of(grid_link.get("fromNode")) == "meter" else "import"
        )

    inverter_out = max(0.0, pv_kw - battery_charge + battery_discharge)
    balance = pv_kw - load_kw - battery_charge + battery_discharge
    grid_source = "flow"

    if grid_kw is None or abs(abs(balance) - grid_kw) > 0.15:
        grid_kw = abs(balance)
        grid_direction = "export" if balance >= 0 else "import"
        grid_source = "computed"

    importing = grid_direction == "import"
    grid = {
        "power_kw": round(grid_kw, 3) if grid_kw is not None else None,
        "direction": grid_direction,
        "import_kw": round(grid_kw, 3) if importing and grid_kw is not None else 0.0,
        "export_kw": round(grid_kw, 3) if not importing and grid_kw is not None else 0.0,
        "source": grid_source,
    }

    # --- Pfeile ---------------------------------------------------------
    load_from_grid = max(0.0, load_kw - inverter_out) if importing else 0.0
    load_from_inverter = max(0.0, load_kw - load_from_grid)

    derived = {
        ("pv", "inverter"): pv_kw,
        ("inverter", "meter"): inverter_out,
        ("inverter", "battery"): battery_charge,
        ("battery", "inverter"): battery_discharge,
        ("inverter", "load"): load_from_inverter,
        ("meter", "load"): load_from_grid,
        ("meter", "grid"): 0.0 if importing else (grid_kw or 0.0),
        ("grid", "meter"): (grid_kw or 0.0) if importing else 0.0,
    }

    flows = []
    for link in links:
        src = role_of(link.get("fromNode"))
        dst = role_of(link.get("toNode"))
        if link.get("flowing") == "BACKWARD":
            src, dst = dst, src
        if not src or not dst:
            continue
        kw = _parse_kw(link.get("description", {}).get("value"))
        if kw is None:
            kw = derived.get((src, dst))
        kw = round(kw, 3) if kw is not None else None
        flows.append(
            {
                "from": src,
                "to": dst,
                "kw": kw,
                "active": bool(kw and kw > 0.001),
            }
        )
    flows.sort(key=lambda item: (not item["active"], FLOW_ORDER.get((item["from"], item["to"]), 99)))

    # --- Tageswerte -----------------------------------------------------
    daily_use = _num(kpi.get("dailyUseEnergy"))
    daily_buy = _num(station.get("dailyBuyEnergy"))
    today = {
        "pv_kwh": _num(kpi.get("dailyEnergy")),
        "load_kwh": daily_use,
        "self_consumption_kwh": _num(kpi.get("dailySelfUseEnergy")),
        "grid_import_kwh": daily_buy,
        "grid_export_kwh": _num(station.get("dailyOnGridEnergy")),
        "battery_charged_kwh": _num(station.get("dailyChargeCapacity")),
        "battery_discharged_kwh": _num(station.get("dailyDisChargeCapacity")),
        "autarky_percent": None,
    }
    if daily_use and daily_buy is not None and daily_use > 0:
        today["autarky_percent"] = round((daily_use - daily_buy) / daily_use * 100, 1)

    return {
        "station": {
            "name": station.get("name"),
            "dn": station.get("dn"),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp": int(time.time()),
        },
        "pv_kw": round(pv_kw, 3),
        "load_kw": round(load_kw, 3),
        "battery": battery,
        "grid": grid,
        "flows": flows,
        "today": today,
    }
