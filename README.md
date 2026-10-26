# Fusion Web Solar

Home-Assistant-Integration für Huawei **FusionSolar** – liest die Werte aus der
Weboberfläche und stellt sie als Sensoren bereit.

Sie liefert, was die FusionSolar-App auch zeigt:

* **PV-Erzeugung**, **Hausverbrauch**, **Netzbezug / Einspeisung**
* **Batterie**: Füllstand, Lade-/Entladeleistung, Lademodus, gespeicherte Energie
* **Energiefluss** („Pfeile") als eigene Entität inklusive Rohdaten als Attribute
* **Tageswerte** und **Autarkie** – direkt nutzbar im Energie-Dashboard

## Warum die Weboberfläche und nicht die offizielle API?

Die offizielle Northbound-API liefert **keinen** Hausverbrauch, **keine**
Energiefluss-Daten und benötigt einen separat anzulegenden API-Benutzer. Diese
Integration nutzt deshalb dieselben Endpunkte wie die Weboberfläche
(`/rest/pvms/web/station/v3/overview/energy-flow` und `station-real-kpi`).

> ⚠️ Das ist eine **inoffizielle** Schnittstelle. Huawei kann sie jederzeit
> ändern. Die Integration ist daher nicht für sicherheitskritische Anwendungen
> geeignet.

## Installation über HACS

1. HACS öffnen → **Benutzerdefinierte Repositories**
2. URL eintragen: `https://github.com/Christoph3573/ha-fusion-web-solar`
3. Kategorie: **Integration**
4. Installieren, danach Home Assistant neu starten

<details>
<summary>Manuelle Installation</summary>

Ordner `custom_components/fusion_web_solar/` aus diesem Repository nach
`config/custom_components/fusion_web_solar/` kopieren und Home Assistant neu
starten.
</details>

## Einrichtung

**Einstellungen → Geräte und Dienste → Integration hinzufügen → „Fusion Web Solar"**

Es gibt zwei Wege:

### E-Mail und Passwort (empfohlen)

Die Zugangsdaten deines normalen FusionSolar-Kontos. Die Integration meldet sich
selbst an und erneuert die Session automatisch, wenn sie abläuft.

Verlangt FusionSolar einen Sicherheitscode (CAPTCHA), bricht die Einrichtung mit
einem Hinweis ab. Einmal im Browser einloggen und erneut versuchen – oder den
Cookie-Weg nehmen.

### Browser-Cookies

Falls 2FA aktiv ist oder kein Passwort gespeichert werden soll:

1. Im Browser bei FusionSolar einloggen
2. DevTools öffnen (F12) → **Netzwerk**
3. Beliebige Anfrage an `...fusionsolar.huawei.com` anklicken
4. Den kompletten `Cookie`-Header-Wert kopieren
5. Als „Cookies" einfügen

Cookies laufen nach einigen Stunden bis Tagen ab. Dann die Einrichtung
wiederholen (der Reauth-Dialog öffnet sich automatisch).

Die Server-URL ist standardmäßig `https://uni002eu5.fusionsolar.huawei.com`.
Andere Regionen erkennst du an der Adresse, unter der du dich im Browser
anmeldest (z. B. `uni001eu5…`).

## Sensoren

| Entität | Einheit | Bedeutung |
| --- | --- | --- |
| `sensor.<anlage>_pv_leistung` | kW | Erzeugung |
| `sensor.<anlage>_hausverbrauch` | kW | Verbrauch im Haus |
| `sensor.<anlage>_netzleistung` | kW | vorzeichenbehaftet: + Bezug, − Einspeisung |
| `sensor.<anlage>_netzbezug` | kW | Bezug (immer ≥ 0) |
| `sensor.<anlage>_einspeisung` | kW | Einspeisung (immer ≥ 0) |
| `sensor.<anlage>_batterieleistung` | kW | + laden, − entladen |
| `sensor.<anlage>_batterieladung` | kW | Laden |
| `sensor.<anlage>_batterieentladung` | kW | Entladen |
| `sensor.<anlage>_batteriefuellstand` | % | SOC |
| `sensor.<anlage>_batterieenergie` | kWh | aktuell gespeicherte Energie |
| `sensor.<anlage>_lademodus` | – | z. B. „Maximaler Eigenverbrauch" |
| `sensor.<anlage>_pv_erzeugung_heute` | kWh | Tagesertrag |
| `sensor.<anlage>_verbrauch_heute` | kWh | Tagesverbrauch |
| `sensor.<anlage>_netzbezug_heute` | kWh | Tagesbezug |
| `sensor.<anlage>_einspeisung_heute` | kWh | Tageseinspeisung |
| `sensor.<anlage>_batterie_geladen_heute` | kWh | geladene Energie |
| `sensor.<anlage>_batterie_entladen_heute` | kWh | entladene Energie |
| `sensor.<anlage>_autarkie` | % | Autarkiegrad |
| `sensor.<anlage>_energiefluss` | – | alle aktiven Pfeile als Text, Rohdaten als Attribut `flows` |

Die `*_heute`-Sensoren sind `total_increasing` und damit direkt für das
Energie-Dashboard geeignet.

### Beispiel: Energiefluss-Entität

Zustand:

```
PV -> Wechselrichter 7,66 kW | Wechselrichter -> Batterie 1,58 kW | ...
```

Attribut `flows`:

```json
[
  {"from": "pv", "to": "inverter", "kw": 7.662, "active": true},
  {"from": "inverter", "to": "battery", "kw": 1.58, "active": true},
  {"from": "meter", "to": "grid", "kw": 4.801, "active": true}
]
```

## Aktualisierung

Standardmäßig alle **5 Minuten** – die Geräte laden nur so oft in die Cloud hoch.
Ein kürzeres Intervall liefert keine neuen Werte, lässt sich aber in den
Optionen der Integration einstellen (Minimum 60 s).

## Berechnung des Hausverbrauchs

Die Weboberfläche liefert den Verbrauch direkt. Für die Pfeile und die
Vorzeichen gilt:

```
Hausverbrauch = PV + Batterieentladung − Batterieladung − Einspeisung + Bezug
```

Die API beschriftet den Netz-Pfeil statisch als „Bezug", unabhängig von der
Richtung. Die Integration bestimmt die Richtung deshalb über die
Leistungsbilanz und ergänzt fehlende Pfeilwerte daraus.

## Deinstallation

Integration entfernen, danach den Ordner
`config/custom_components/fusion_web_solar/` löschen.

## Lizenz

MIT – siehe [LICENSE](LICENSE).
