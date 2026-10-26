#!/usr/bin/env python3
"""
fusionsolar.py - Live-Werte einer Huawei FusionSolar Anlage im Terminal.

Eigenstaendiges CLI (gleiche Logik wie die Home-Assistant-Integration).
Praktisch zum Debuggen, wenn in Home Assistant etwas nicht stimmt.

Zeigt aktuell:
  * PV-Erzeugung
  * Hausverbrauch
  * Batterie: Fuellstand (SOC) und Lade-/Entladeleistung
  * Netz: Bezug oder Einspeisung
  * die Energiefluesse ("Pfeile") zwischen den Komponenten
  * Tageswerte und Autarkie

Anmeldung
---------
Das Skript loggt sich mit E-Mail und Passwort in der FusionSolar-Cloud ein
und holt sich daraus die Session fuer die Live-Daten:

    ./fusionsolar.py --login

E-Mail und Passwort werden einmalig abgefragt und in
~/.config/fusionsolar/config.json (Rechte 0600) gespeichert. Laeuft die
Session ab, meldet sich das Skript automatisch neu an.

Alternativ ohne Passwort-Speicherung: Cookies aus dem Browser verwenden

    ./fusionsolar.py --cookie "JSESSIONID=...; dp-session=..."
    ./fusionsolar.py --from-har netzwerk.har

Nur Python-Standardbibliothek, benoetigt `openssl` (auf macOS vorhanden).
"""

from __future__ import annotations

import argparse
import base64
import getpass
import http.cookiejar
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://uni002eu5.fusionsolar.huawei.com"
CONFIG_DIR = os.path.expanduser("~/.config/fusionsolar")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
COOKIE_PATH = os.path.join(CONFIG_DIR, "cookies.txt")

# mocId -> Rolle im Energiefluss der FusionSolar-Weboberflaeche
NODE_ROLES = {
    20812: "pv",         # PV-Strings
    20822: "inverter",   # Wechselrichter
    20816: "meter",      # Zaehler
    90001: "grid",       # Netz
    20815: "battery",    # Batterie / Energiespeicher
    90002: "load",       # Hausverbrauch
}

ROLE_LABELS = {
    "pv": "PV",
    "inverter": "Wechselrichter",
    "meter": "Zaehler",
    "grid": "Netz",
    "battery": "Batterie",
    "load": "Haus",
}

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:158.0) Firefox/158.0"

# Anzeigereihenfolge der Pfeile (topologisch statt alphabetisch)
FLOW_ORDER = {
    ("pv", "inverter"): 0,
    ("inverter", "battery"): 1,
    ("battery", "inverter"): 2,
    ("inverter", "load"): 3,
    ("inverter", "meter"): 4,
    ("meter", "load"): 5,
    ("meter", "grid"): 6,
    ("grid", "meter"): 7,
}


# ==========================================================================
# Fehler
# ==========================================================================

class FusionSolarError(Exception):
    pass


class SessionExpired(FusionSolarError):
    pass


class LoginFailed(FusionSolarError):
    pass


# ==========================================================================
# Hilfsfunktionen
# ==========================================================================

def sso_host_for(base_url: str) -> str:
    """uni002eu5.fusionsolar.huawei.com -> eu5.fusionsolar.huawei.com"""
    host = urllib.parse.urlsplit(base_url).netloc
    if host.startswith("uni"):
        parts = host.split(".")
        parts[0] = parts[0].replace("uni", "").lstrip("0") or "0"
        # "002eu5" -> "eu5"
        zone = parts[0]
        for i, ch in enumerate(zone):
            if ch.isalpha():
                parts[0] = zone[i:]
                break
        host = ".".join(parts)
    return f"https://{host}"


def read_config() -> dict:
    for path in (os.environ.get("FUSIONSOLAR_CONFIG"), CONFIG_PATH):
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, ValueError) as exc:
                print(f"Warnung: {path} nicht lesbar ({exc})", file=sys.stderr)
    return {}


def write_config(cfg: dict) -> str:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)
    return CONFIG_PATH


def normalize_cookie(raw: str) -> str:
    """Akzeptiert "a=1; b=2", "Cookie: a=1; b=2" oder mehrzeilige Header."""
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1]
    parts = []
    for chunk in raw.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if "=" in chunk:
            name, value = chunk.split("=", 1)
            parts.append(f"{name.strip()}={value.strip()}")
    if not parts:
        raise FusionSolarError("Keine Cookies erkannt (Format: JSESSIONID=...; dp-session=...)")
    return "; ".join(parts)


def _kw(value, unit="kW") -> str:
    if value is None:
        return "-"
    return f"{value:.2f} {unit}".replace(".", ",")


def _energy(value) -> str:
    if value is None:
        return "-"
    return f"{value:.2f} kWh".replace(".", ",")


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_kw(text):
    if not text:
        return None
    return _num(str(text).replace("kW", "").replace(",", ".").strip())


# ==========================================================================
# FusionSolar-Client (Login + API)
# ==========================================================================

class FusionSolar:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, email: str = None,
                 password: str = None, timeout: int = 25, tz_offset: int = 60):
        self.base_url = base_url.rstrip("/")
        self.sso = sso_host_for(self.base_url)
        self.email = email
        self.password = password
        self.timeout = timeout
        self.tz_offset = tz_offset
        self.last_keep_alive = 0.0

        self.jar = http.cookiejar.MozillaCookieJar(COOKIE_PATH)
        if os.path.exists(COOKIE_PATH):
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except (OSError, http.cookiejar.LoadError):
                pass
        self._build_opener()

    # -- Infrastruktur ---------------------------------------------------
    def _build_opener(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.opener.addheaders = [("User-Agent", USER_AGENT)]

    def save_cookies(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        try:
            self.jar.save(ignore_discard=True, ignore_expires=True)
            os.chmod(COOKIE_PATH, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def add_cookies(self, cookie_string: str):
        cookie_string = normalize_cookie(cookie_string)
        host = urllib.parse.urlsplit(self.base_url).netloc
        for part in cookie_string.split("; "):
            name, value = part.split("=", 1)
            self.jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=name, value=value, port=None, port_specified=False,
                domain=host, domain_specified=True, domain_initial_dot=False,
                path="/", path_specified=True, secure=True, expires=None,
                discard=False, comment=None, comment_url=None, rest={}, rfc2109=False,
            ))
        self.save_cookies()

    def reset_session(self):
        """Alle Cookies verwerfen - jeder Login startet mit frischer Session."""
        self.jar.clear()
        if os.path.exists(COOKIE_PATH):
            try:
                os.remove(COOKIE_PATH)
            except OSError:
                pass

    def _open(self, url, data=None, headers=None, host_headers=False):
        hdrs = {"Accept": "application/json", "Accept-Language": "de,en-US;q=0.9,en;q=0.8"}
        if host_headers:
            hdrs.update({
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "x-non-renewal-session": "true",
                "x-timezone-offset": str(self.tz_offset),
                "Referer": self.base_url + "/uniportal/pvmswebsite/assets/build/cloud.html",
            })
        if data is not None:
            hdrs["Content-Type"] = "application/json;charset=UTF-8"
        hdrs.update(headers or {})

        req = urllib.request.Request(url, data=data, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
                final = resp.geturl()
                charset = resp.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 401, 403):
                raise SessionExpired(f"HTTP {exc.code}") from exc
            raise FusionSolarError(f"HTTP {exc.code} fuer {url}") from exc
        except urllib.error.URLError as exc:
            raise FusionSolarError(f"Netzwerkfehler: {exc.reason}") from exc

        self.save_cookies()
        return body.decode(charset, errors="replace"), final

    def _json(self, url, data=None, host_headers=False):
        text, final = self._open(url, data=data, host_headers=host_headers)
        if "/unisso/login" in final or "/unisso/logout" in final:
            raise SessionExpired("Weiterleitung auf die Login-Seite")
        try:
            return json.loads(text)
        except ValueError:
            raise SessionExpired("Antwort ist kein JSON (Login-Seite)")

    def _fetch_bytes(self, url):
        req = urllib.request.Request(url, headers={
            "Accept": "image/png,image/*,*/*",
            "User-Agent": USER_AGENT,
            "Referer": self.sso + "/unisso/login.action",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            raise LoginFailed(f"CAPTCHA konnte nicht geladen werden (HTTP {exc.code})") from exc
        except urllib.error.URLError as exc:
            raise LoginFailed(f"CAPTCHA konnte nicht geladen werden: {exc.reason}") from exc

    # -- Login -----------------------------------------------------------
    @staticmethod
    def _pem_to_der_encrypt(pub_pem: str, data: bytes) -> bytes:
        """RSA-OAEP mit SHA-384 ueber die openssl-CLI (kein Zusatzpaket noetig)."""
        with tempfile.TemporaryDirectory() as tmp:
            key_path = os.path.join(tmp, "pub.pem")
            msg_path = os.path.join(tmp, "msg.bin")
            with open(key_path, "w", encoding="utf-8") as fh:
                fh.write(pub_pem)
            with open(msg_path, "wb") as fh:
                fh.write(data)
            result = subprocess.run(
                ["openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", key_path,
                 "-pkeyopt", "rsa_padding_mode:oaep",
                 "-pkeyopt", "rsa_oaep_md:sha384",
                 "-pkeyopt", "rsa_mgf1_md:sha384",
                 "-in", msg_path],
                capture_output=True,
            )
            if result.returncode != 0:
                raise LoginFailed(
                    "Passwort konnte nicht verschluesselt werden: "
                    + result.stderr.decode(errors="replace").strip()
                )
            return result.stdout

    def _encrypt_password(self, pub_key: str, version: str, password: str) -> str:
        """Chunkweise RSA-OAEP-SHA384, Bloecke mit '00000001' verbunden."""
        encoded = urllib.parse.quote(password, safe="")
        chunks = [encoded[i:i + 270] for i in range(0, len(encoded), 270)] or [""]
        parts = []
        for index, chunk in enumerate(chunks):
            cipher = self._pem_to_der_encrypt(pub_key, chunk.encode())
            parts.append(("" if index == 0 else "00000001") + base64.b64encode(cipher).decode())
        return "".join(parts) + version

    def login(self, verify_code: str = "", service: str = None,
              captcha_prompt=None, max_attempts: int = 3,
              keep_session: bool = False) -> None:
        if not self.email or not self.password:
            raise LoginFailed("E-Mail oder Passwort fehlt.")

        # Frische Session: verhindert, dass alte Cookies oder ein alter
        # Captcha-Status den Login blockieren. Ausnahme: mit --verify-code
        # wird die Session des ersten Aufrufs weiterverwendet, weil der
        # Sicherheitscode daran gebunden ist.
        if not keep_session:
            self.reset_session()

        service = service or (
            self.base_url + "/uniportal/pvmswebsite/assets/build/cloud.html"
            "?app-id=smartpvms&instance-id=smartpvms"
        )

        pubkey = self._json(self.sso + "/unisso/pubkey")
        password = (
            self._encrypt_password(pubkey["pubKey"], pubkey["version"], self.password)
            if pubkey.get("enableEncrypt")
            else urllib.parse.quote(self.password, safe="")
        )

        query = urllib.parse.urlencode({
            "service": service,
            "timeStamp": str(pubkey.get("timeStamp", int(time.time() * 1000))),
            "nonce": os.urandom(16).hex(),
        })
        endpoint = self.sso + "/unisso/v3/validateUser.action?" + query

        redirect = None
        for attempt in range(max_attempts):
            body = json.dumps({
                "organizationName": "",
                "username": self.email,
                "password": password,
                "verifycode": verify_code,
                "multiRegionName": "",
                "useFederationRouter": False,
                "scene": "",
            }).encode()

            result = self._json(endpoint, data=body)

            if result.get("twoFactorStatus"):
                raise LoginFailed(
                    "2FA_ERFORDERLICH: Fuer dieses Konto ist eine Zwei-Faktor-Anmeldung aktiv.\n"
                    "Bitte die 2FA in der FusionSolar-Weboberflaeche deaktivieren oder die\n"
                    "Cookies aus dem Browser verwenden (--cookie / --from-har)."
                )

            redirect = result.get("redirectURL")
            if redirect:
                break

            if result.get("verifyCodeCreate"):
                if verify_code and attempt > 0:
                    print("Sicherheitscode war falsch, bitte erneut eingeben.", file=sys.stderr)
                verify_code = (captcha_prompt or self.prompt_captcha)()
                continue

            if result.get("multiRegions"):
                raise LoginFailed(
                    "Dieses Konto gilt fuer mehrere Regionen. Bitte die passende "
                    "Server-URL mit --base-url angeben."
                )

            message = result.get("errorMsg") or result.get("errorMessage") or "unbekannter Fehler"
            raise LoginFailed(
                f"Anmeldung fehlgeschlagen: {message} (Code {result.get('errorCode')})"
            )
        else:
            raise LoginFailed("Sicherheitscode wurde nicht akzeptiert.")

        # Ticket einloesen: setzt SSO-Cookies und die Session der Weboberflaeche.
        self._open(redirect)
        self._open(self.base_url + "/uniportal/pvmswebsite/assets/build/cloud.html"
                   "?app-id=smartpvms&instance-id=smartpvms")
        try:
            self._json(self.base_url + "/rest/pvms/web/station/v1/station/station-list",
                       data=json.dumps({"curPage": 1, "pageSize": 1, "gridConnectedTime": "",
                                        "queryTime": int(time.time() * 1000), "timeZone": 1,
                                        "sortId": "createTime", "sortDir": "DESC",
                                        "locale": "de_DE"}).encode(),
                       host_headers=True)
        except SessionExpired as exc:
            raise LoginFailed(
                "Anmeldung war erfolgreich, aber die Session der Weboberflaeche "
                f"konnte nicht uebernommen werden ({exc})."
            )

    # -- API -------------------------------------------------------------
    def api(self, path: str, payload: dict = None) -> dict:
        url = path if path.startswith("http") else self.base_url + path
        data = json.dumps(payload).encode() if payload is not None else None
        result = self._json(url, data=data, host_headers=True)
        if isinstance(result, dict) and result.get("success") is False:
            raise FusionSolarError(
                f"API-Fehler {result.get('failCode')}: {result.get('message') or ''}".strip()
            )
        return result

    def prompt_captcha(self) -> str:
        """CAPTCHA-Bild laden, anzeigen und Code abfragen."""
        stamp = int(time.time() * 1000)
        image, headers = self._fetch_bytes(self.sso + f"/unisso/verifycode?timestamp={stamp}")
        kind = headers.get("x-verifycode-type", "")
        content_type = headers.get("Content-Type", "") or ""
        suffix = ".png" if "png" in content_type else ".jpg"
        os.makedirs(CONFIG_DIR, exist_ok=True)
        path = os.path.join(CONFIG_DIR, "captcha" + suffix)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(image)

        hint = {"calculate": "Rechnung", "randomStr6": "6 Zeichen"}.get(kind, "")
        print(f"Sicherheitscode noetig ({hint or 'Zeichen im Bild'}).")
        print(f"Bild: {path}")
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen([opener, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
        try:
            return input("Code: ").strip()
        except EOFError:
            raise LoginFailed(
                f"Sicherheitscode erforderlich. Bild liegt unter {path}; "
                "mit --verify-code den Code uebergeben."
            )

    def keep_alive(self, max_age: float = 240) -> None:
        if time.time() - self.last_keep_alive < max_age:
            return
        try:
            self.api("/rest/dpcloud/auth/v1/keep-alive")
            self.last_keep_alive = time.time()
        except FusionSolarError:
            pass

    # -- Endpunkte -------------------------------------------------------
    def station_list(self) -> dict:
        return self.api("/rest/pvms/web/station/v1/station/station-list", {
            "curPage": 1,
            "pageSize": 10,
            "gridConnectedTime": "",
            "queryTime": int(time.time() * 1000),
            "timeZone": 1,
            "sortId": "createTime",
            "sortDir": "DESC",
            "locale": "de_DE",
        }).get("data", {})

    def real_kpi(self, station_dn: str) -> dict:
        query = urllib.parse.urlencode({
            "stationDn": station_dn,
            "clientTime": int(time.time() * 1000),
            "timeZone": 1,
        })
        return self.api("/rest/pvms/web/station/v1/overview/station-real-kpi?" + query).get("data", {})

    def energy_flow(self, station_dn: str) -> dict:
        query = urllib.parse.urlencode({"stationDn": station_dn, "featureId": "aifc"})
        return self.api("/rest/pvms/web/station/v3/overview/energy-flow?" + query).get("data", {})

    def snapshot(self, station_dn: str = None) -> dict:
        stations = self.station_list().get("list") or []
        if not stations:
            raise FusionSolarError("Keine Anlage im Konto gefunden.")

        station = stations[0]
        if station_dn:
            for candidate in stations:
                if candidate.get("dn") == station_dn:
                    station = candidate
                    break
        dn = station["dn"]

        kpi = self.real_kpi(dn)
        flow = self.energy_flow(dn)
        return build_snapshot(station, kpi, flow)


# ==========================================================================
# Auswertung
# ==========================================================================

def build_snapshot(station: dict, kpi: dict, flow: dict) -> dict:
    nodes = {n["id"]: n for n in flow.get("flow", {}).get("nodes", [])}
    links = flow.get("flow", {}).get("links", [])

    by_role: dict = {}
    for node in nodes.values():
        role = NODE_ROLES.get(node.get("mocId"))
        if role:
            by_role.setdefault(role, []).append(node)

    def node_value(role: str) -> float:
        return sum(_num(n.get("value"), 0.0) or 0.0 for n in by_role.get(role, []))

    def role_of(node_id: str):
        node = nodes.get(node_id)
        return NODE_ROLES.get(node.get("mocId")) if node else None

    pv_kw = node_value("pv")
    load_kw = node_value("load")

    # --- Batterie -------------------------------------------------------
    battery_nodes = by_role.get("battery", [])
    battery_power = node_value("battery")
    tips = (battery_nodes[0].get("deviceTips") or {}) if battery_nodes else {}
    soc = _num(tips.get("SOC"))
    if soc is None and battery_nodes:
        soc = _num((battery_nodes[0].get("customAttr") or {}).get("10006"))

    # Richtung des Batteriepfeils: Wechselrichter->Batterie = laden.
    battery_link = None
    for link in links:
        roles = {role_of(link.get("fromNode")), role_of(link.get("toNode"))}
        if roles == {"inverter", "battery"}:
            battery_link = (role_of(link.get("fromNode")), role_of(link.get("toNode")))
            break
    charging = not (battery_link and battery_link[0] == "battery")

    battery = {
        "soc_percent": soc,
        "power_kw": round(battery_power, 3),
        "state": ("charging" if charging else "discharging") if battery_power > 0.001 else "idle",
        "capacity_kwh": _num(station.get("batteryCapacity")),
        "devices": len(battery_nodes),
        "mode": tips.get("CHARGE_MODE_VALUE"),
    }
    if soc is not None and battery["capacity_kwh"]:
        battery["stored_kwh"] = round(soc / 100 * battery["capacity_kwh"], 2)

    # --- Netz -----------------------------------------------------------
    # Richtung kommt aus dem Pfeil (Zaehler->Netz = Einspeisung). Zusaetzlich
    # Gegenprobe ueber die Leistungsbilanz, weil das Label der API statisch ist.
    grid_link = None
    for link in links:
        if "grid" in (role_of(link.get("fromNode")), role_of(link.get("toNode"))):
            grid_link = link
            break

    grid_kw = _parse_kw((grid_link or {}).get("description", {}).get("value"))
    grid_direction = None
    if grid_link:
        grid_direction = "export" if role_of(grid_link.get("fromNode")) == "meter" else "import"

    batt_charge = battery["power_kw"] if charging else 0.0
    batt_discharge = 0.0 if charging else battery["power_kw"]
    inverter_out = max(0.0, pv_kw - batt_charge + batt_discharge)
    balance = pv_kw - load_kw - batt_charge + batt_discharge

    if pv_kw is not None and load_kw is not None and (
        grid_kw is None or abs(abs(balance) - grid_kw) > 0.15
    ):
        grid_kw = abs(balance)
        grid_direction = "export" if balance >= 0 else "import"
        grid_source = "computed"
    else:
        grid_source = "flow" if grid_link else "computed"

    grid = {
        "power_kw": round(grid_kw, 3) if grid_kw is not None else None,
        "direction": grid_direction,
        "source": grid_source,
    }

    # --- Fluesse / Pfeile ----------------------------------------------
    # Die API liefert nur fuer einige Pfeile einen Wert. Fehlende Werte
    # werden aus der Leistungsbilanz ergaenzt, damit das Bild vollstaendig ist.
    importing = grid["direction"] == "import"
    load_from_inverter = max(0.0, load_kw - (max(0.0, load_kw - inverter_out) if importing else 0.0))
    load_from_grid = max(0.0, load_kw - inverter_out) if importing else 0.0

    derived = {
        ("pv", "inverter"): pv_kw,
        ("inverter", "meter"): inverter_out,
        ("inverter", "battery"): batt_charge,
        ("battery", "inverter"): batt_discharge,
        ("inverter", "load"): load_from_inverter,
        ("meter", "load"): load_from_grid,
        ("meter", "grid"): grid_kw if not importing else 0.0,
        ("grid", "meter"): grid_kw if importing else 0.0,
    }

    flows = []
    for link in links:
        src, dst = role_of(link.get("fromNode")), role_of(link.get("toNode"))
        if link.get("flowing") == "BACKWARD":
            src, dst = dst, src
        if not src or not dst:
            continue
        kw = _parse_kw(link.get("description", {}).get("value"))
        if kw is None:
            kw = derived.get((src, dst))
        kw = round(kw, 3) if kw is not None else None
        flows.append({
            "from": src,
            "to": dst,
            "kw": kw,
            "active": bool(kw and kw > 0.001),
        })

    flows.sort(key=lambda f: (not f["active"], FLOW_ORDER.get((f["from"], f["to"]), 99)))

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


# ==========================================================================
# Ausgabe
# ==========================================================================

def render(snap: dict) -> str:
    lines = []
    station, batt, grid = snap["station"], snap["battery"], snap["grid"]

    lines.append(f"{station['name']}  ({station['time']})")
    lines.append("")

    def row(label, value, note=""):
        lines.append(f"  {label:<22}{value:>12}  {note}".rstrip())

    row("Erzeugung (PV)", _kw(snap["pv_kw"]))
    row("Hausverbrauch", _kw(snap["load_kw"]))

    soc = batt.get("soc_percent")
    soc_text = f"{soc:.0f} %" if soc is not None else "-"
    if batt["state"] == "charging":
        note = f"laedt mit {_kw(batt['power_kw'])}"
    elif batt["state"] == "discharging":
        note = f"entlaedt mit {_kw(batt['power_kw'])}"
    else:
        note = "keine Leistung"
    if batt.get("stored_kwh") is not None and batt.get("capacity_kwh"):
        note += f"  ({batt['stored_kwh']:.1f} von {batt['capacity_kwh']:.1f} kWh)".replace(".", ",")
    row("Batterie", soc_text, note)

    if grid["power_kw"] is None:
        row("Netz", "-")
    else:
        row("Netz", _kw(grid["power_kw"]),
            "Einspeisung" if grid["direction"] == "export" else "Bezug")

    active = [f for f in snap["flows"] if f["active"]]
    if active:
        lines.append("")
        lines.append("  Energiefluesse")
        for f in active:
            src = ROLE_LABELS.get(f["from"], f["from"])
            dst = ROLE_LABELS.get(f["to"], f["to"])
            lines.append(f"  {src + ' -> ' + dst:<30}{_kw(f['kw']):>12}")

    today = snap["today"]
    if today.get("pv_kwh") is not None:
        lines.append("")
        lines.append(
            "  Heute: "
            f"PV {_energy(today['pv_kwh'])} | "
            f"Verbrauch {_energy(today['load_kwh'])} | "
            f"Bezug {_energy(today['grid_import_kwh'])} | "
            f"Einspeisung {_energy(today['grid_export_kwh'])}"
        )
        if today.get("autarky_percent") is not None:
            lines.append(f"  Autarkie: {today['autarky_percent']:.1f} %".replace(".", ","))
    return "\n".join(lines)


def render_compact(snap: dict) -> str:
    batt, grid = snap["battery"], snap["grid"]
    soc = batt.get("soc_percent")
    direction = "EXP" if grid["direction"] == "export" else "IMP"
    return "  ".join([
        snap["station"]["time"][11:],
        f"PV {_kw(snap['pv_kw'])}",
        f"Haus {_kw(snap['load_kw'])}",
        f"Akku {soc:.0f} %" if soc is not None else "Akku -",
        f"Netz {_kw(grid['power_kw'])} {direction}",
    ])


# ==========================================================================
# HAR-Import (Fallback ohne Passwort)
# ==========================================================================

def cookie_candidates_from_har(path: str) -> tuple:
    with open(path, "r", encoding="utf-8") as fh:
        har = json.load(fh)

    candidates, station_dn, base_url = [], None, None
    for entry in har["log"]["entries"]:
        request = entry.get("request", {})
        parsed = urllib.parse.urlsplit(request.get("url", ""))
        if "fusionsolar" not in parsed.netloc:
            continue

        for key, values in urllib.parse.parse_qs(parsed.query).items():
            if key in ("stationDn", "dn", "plantDn", "domainDn") and values[0].startswith("NE="):
                station_dn = station_dn or values[0]

        cookies = {c["name"]: c["value"] for c in request.get("cookies") or [] if c.get("name")}
        if not cookies:
            continue
        base_url = base_url or f"{parsed.scheme}://{parsed.netloc}"
        cookie_string = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if cookie_string not in [c[0] for c in candidates]:
            candidates.append((cookie_string, entry.get("response", {}).get("status")))

    if not candidates:
        raise FusionSolarError("In der HAR-Datei wurden keine FusionSolar-Cookies gefunden.")
    return candidates, station_dn, base_url


# ==========================================================================
# CLI
# ==========================================================================

def cmd_login(client: FusionSolar, args, cfg: dict) -> int:
    email = args.email or os.environ.get("FUSIONSOLAR_EMAIL") or cfg.get("email")
    password = args.password or os.environ.get("FUSIONSOLAR_PASSWORD") or cfg.get("password")

    try:
        if not email:
            email = input("E-Mail: ").strip()
        if not password:
            password = getpass.getpass("Passwort: ")
    except EOFError:
        print("E-Mail/Passwort fehlen. Nutze --email und --password.", file=sys.stderr)
        return 1

    client.email, client.password = email, password
    try:
        client.login(verify_code=args.verify_code or "",
                     keep_session=bool(args.verify_code))
    except LoginFailed as exc:
        message = str(exc)
        if "Sicherheitscode" in message:
            # Session behalten: der Code ist an sie gebunden.
            print(f"Login fehlgeschlagen: {exc}", file=sys.stderr)
            print("Aufruf mit dem Code aus dem Bild wiederholen, z. B.:", file=sys.stderr)
            print("  ./fusionsolar.py --login --verify-code ABC123", file=sys.stderr)
            return 3
        client.reset_session()
        print(f"Login fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    except FusionSolarError as exc:
        client.reset_session()
        print(f"Login fehlgeschlagen: {exc}", file=sys.stderr)
        return 1

    stations = client.station_list().get("list") or []
    cfg = read_config()
    cfg.update({"email": email, "password": password})
    if stations:
        cfg.setdefault("station_dn", stations[0]["dn"])
    path = write_config(cfg)

    print(f"Anmeldung erfolgreich, Session gespeichert ({path}).")
    print("E-Mail/Passwort liegen dort mit Rechten 0600; '--forget' loescht sie.")
    for station in stations:
        print(f"  {station.get('name')}  ({station.get('dn')})  {station.get('plantStatus')}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="fusionsolar",
        description="Live-Werte der FusionSolar-Anlage abrufen (PV, Haus, Batterie, Netz, Fluesse).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--login", action="store_true", help="Mit E-Mail/Passwort anmelden und Session speichern")
    parser.add_argument("--email", help="FusionSolar-E-Mail")
    parser.add_argument("--password", help="FusionSolar-Passwort (sonst interaktive Abfrage)")
    parser.add_argument("--verify-code", help="Sicherheitscode, falls FusionSolar einen verlangt")
    parser.add_argument("--from-har", metavar="DATEI", help="Cookies aus HAR-Export importieren")
    parser.add_argument("--cookie", help="Cookie-String aus dem Browser")
    parser.add_argument("--cookie-file", help="Datei mit Cookie-String")
    parser.add_argument("--station", help="Anlagen-DN (Standard: erste Anlage im Konto)")
    parser.add_argument("--base-url", help="Server-URL (Standard: %s)" % DEFAULT_BASE_URL)
    parser.add_argument("--json", action="store_true", help="Rohdaten als JSON")
    parser.add_argument("--watch", type=int, metavar="SEK", help="Dauerhaft aktualisieren")
    parser.add_argument("--check", action="store_true", help="Nur Verbindung/Anlagen pruefen")
    parser.add_argument("--forget", action="store_true", help="Gespeicherte Zugangsdaten und Cookies loeschen")
    args = parser.parse_args(argv)

    if args.forget:
        for path in (CONFIG_PATH, COOKIE_PATH):
            if os.path.exists(path):
                os.remove(path)
                print(f"Geloescht: {path}")
        return 0

    cfg = read_config()
    base_url = args.base_url or os.environ.get("FUSIONSOLAR_BASE_URL") or cfg.get("base_url") or DEFAULT_BASE_URL

    client = FusionSolar(base_url)
    client.email = args.email or os.environ.get("FUSIONSOLAR_EMAIL") or cfg.get("email")
    client.password = args.password or os.environ.get("FUSIONSOLAR_PASSWORD") or cfg.get("password")

    # -- Login -----------------------------------------------------------
    if args.login:
        return cmd_login(client, args, cfg)

    # -- Cookies importieren (Fallback) ----------------------------------
    if args.from_har:
        candidates, station_dn, har_base = cookie_candidates_from_har(args.from_har)
        if har_base:
            client = FusionSolar(har_base)
        for cookie_string, status in candidates:
            probe = FusionSolar(client.base_url)
            try:
                probe.add_cookies(cookie_string)
                stations = probe.station_list().get("list") or []
            except FusionSolarError:
                continue
            if stations:
                cfg.update({"station_dn": station_dn or stations[0]["dn"],
                            "base_url": client.base_url})
                cfg.pop("password", None)
                path = write_config(cfg)
                print(f"Cookies uebernommen ({path}).")
                print(f"  {stations[0].get('name')}  ({stations[0].get('dn')})")
                return 0
        print("Die Cookies aus der HAR-Datei sind abgelaufen.", file=sys.stderr)
        print("Bitte stattdessen anmelden: ./fusionsolar.py --login", file=sys.stderr)
        return 2

    if args.cookie or args.cookie_file:
        raw = args.cookie
        if args.cookie_file:
            with open(args.cookie_file, "r", encoding="utf-8") as fh:
                raw = fh.read()
        client.add_cookies(raw)

    # -- Session sicherstellen -------------------------------------------
    try:
        stations = client.station_list().get("list") or []
    except SessionExpired:
        if not (client.email and client.password):
            print("Keine gueltige Session. Bitte anmelden:\n  ./fusionsolar.py --login",
                  file=sys.stderr)
            return 2
        try:
            client.login()
        except LoginFailed as exc:
            print(f"Login fehlgeschlagen: {exc}", file=sys.stderr)
            return 1
        except FusionSolarError as exc:
            print(f"Login fehlgeschlagen: {exc}", file=sys.stderr)
            return 1
        stations = client.station_list().get("list") or []
    except LoginFailed as exc:
        print(f"Login fehlgeschlagen: {exc}", file=sys.stderr)
        return 1

    if not stations:
        print("Keine Anlage im Konto gefunden.", file=sys.stderr)
        return 2

    if args.check:
        print("Verbindung OK")
        for station in stations:
            print(f"  {station.get('name')}  ({station.get('dn')})  {station.get('plantStatus')}")
        return 0

    # Anlagen-DN in der Konfiguration merken, damit --station nicht noetig ist.
    if not cfg.get("station_dn"):
        cfg["station_dn"] = stations[0]["dn"]
        cfg.setdefault("base_url", client.base_url)
        write_config(cfg)

    # -- Daten abrufen ---------------------------------------------------
    station_dn = args.station or cfg.get("station_dn")

    def fetch() -> dict:
        return client.snapshot(station_dn)

    try:
        if not args.watch:
            snap = fetch()
            print(json.dumps(snap, indent=2, ensure_ascii=False) if args.json else render(snap))
            return 0

        while True:
            try:
                client.keep_alive()
                snap = fetch()
            except SessionExpired:
                if not (client.email and client.password):
                    print("Session abgelaufen. Bitte neu anmelden: ./fusionsolar.py --login",
                          file=sys.stderr)
                    return 2
                client.login()
                snap = fetch()
            if args.json:
                print(json.dumps(snap, ensure_ascii=False), flush=True)
            elif sys.stdout.isatty():
                sys.stdout.write("\033[H\033[2J" + render(snap) + "\n")
                sys.stdout.flush()
            else:
                print(render_compact(snap), flush=True)
            time.sleep(max(5, args.watch))
    except KeyboardInterrupt:
        return 130
    except FusionSolarError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
