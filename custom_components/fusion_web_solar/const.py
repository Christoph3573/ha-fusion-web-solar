"""Konstanten der Integration fusion_web_solar."""

from __future__ import annotations

DOMAIN = "fusion_web_solar"
NAME = "Fusion Web Solar"
MANUFACTURER = "Huawei"

# Die Web-URL unterscheidet sich je Region (z. B. uni002eu5 / uni001eu5).
DEFAULT_BASE_URL = "https://uni002eu5.fusionsolar.huawei.com"

# Die Geraete laden nur alle ~5 Minuten in die Cloud hoch. Schneller zu
# pollen liefert keine neuen Werte und belastet das Konto unnoetig.
DEFAULT_SCAN_INTERVAL = 300
MIN_SCAN_INTERVAL = 60

DEFAULT_TIMEZONE_OFFSET = 60

CONF_BASE_URL = "base_url"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_COOKIES = "cookies"
CONF_STATION_DN = "station_dn"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_TIMEZONE_OFFSET = "timezone_offset"

# mocId -> Rolle im Energiefluss der FusionSolar-Weboberflaeche
NODE_ROLES = {
    20812: "pv",        # PV-Strings
    20822: "inverter",  # Wechselrichter
    20816: "meter",     # Zaehler
    90001: "grid",      # Netz
    20815: "battery",   # Batterie / Energiespeicher
    90002: "load",      # Hausverbrauch
}

ROLE_LABELS = {
    "pv": "PV",
    "inverter": "Wechselrichter",
    "meter": "Zaehler",
    "grid": "Netz",
    "battery": "Batterie",
    "load": "Haus",
}

# Kurzform fuer den Zustand der Energiefluss-Entitaet (HA-Limit: 255 Zeichen)
SHORT_LABELS = {
    "pv": "PV",
    "inverter": "WR",
    "meter": "Zaehler",
    "grid": "Netz",
    "battery": "Akku",
    "load": "Haus",
}

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
