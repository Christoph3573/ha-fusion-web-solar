"""Sensoren der Integration fusion_web_solar."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, SHORT_LABELS
from .coordinator import FusionSolarCoordinator


@dataclass(frozen=True, kw_only=True)
class FusionSolarSensorDescription(SensorEntityDescription):
    """Sensor-Beschreibung mit Zugriffsfunktion auf die Coordinator-Daten."""

    value_fn: Callable[[dict[str, Any]], Any]
    attributes_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _grid_signed(data: dict[str, Any]) -> float | None:
    grid = data.get("grid") or {}
    if grid.get("power_kw") is None:
        return None
    return round((grid.get("import_kw") or 0) - (grid.get("export_kw") or 0), 3)


def _battery_signed(data: dict[str, Any]) -> float | None:
    battery = data.get("battery") or {}
    return round((battery.get("charge_kw") or 0) - (battery.get("discharge_kw") or 0), 3)


def _flow_summary(data: dict[str, Any]) -> str:
    """Kompakte Textfassung der aktiven Pfeile (HA-Limit: 255 Zeichen)."""
    active = [flow for flow in data.get("flows", []) if flow.get("active")]
    if not active:
        return "keine Leistung"

    parts = []
    for flow in active:
        src = SHORT_LABELS.get(flow["from"], flow["from"])
        dst = SHORT_LABELS.get(flow["to"], flow["to"])
        value = f"{flow['kw']:.2f}".replace(".", ",") if flow.get("kw") is not None else "-"
        parts.append(f"{src}->{dst} {value}")

    summary = " | ".join(parts)
    if len(summary) > 250:
        summary = summary[:247] + "..."
    return summary


SENSORS: tuple[FusionSolarSensorDescription, ...] = (
    FusionSolarSensorDescription(
        key="pv_power",
        name="PV-Leistung",
        icon="mdi:solar-power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("pv_kw"),
    ),
    FusionSolarSensorDescription(
        key="load_power",
        name="Hausverbrauch",
        icon="mdi:home-lightning-bolt",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("load_kw"),
    ),
    FusionSolarSensorDescription(
        key="grid_power",
        name="Netzleistung",
        icon="mdi:transmission-tower",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=_grid_signed,
    ),
    FusionSolarSensorDescription(
        key="grid_import_power",
        name="Netzbezug",
        icon="mdi:transmission-tower-import",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: (data.get("grid") or {}).get("import_kw"),
    ),
    FusionSolarSensorDescription(
        key="grid_export_power",
        name="Einspeisung",
        icon="mdi:transmission-tower-export",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: (data.get("grid") or {}).get("export_kw"),
    ),
    FusionSolarSensorDescription(
        key="battery_power",
        name="Batterieleistung",
        icon="mdi:battery-sync",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=_battery_signed,
    ),
    FusionSolarSensorDescription(
        key="battery_charge_power",
        name="Batterieladung",
        icon="mdi:battery-plus",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: (data.get("battery") or {}).get("charge_kw"),
    ),
    FusionSolarSensorDescription(
        key="battery_discharge_power",
        name="Batterieentladung",
        icon="mdi:battery-minus",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: (data.get("battery") or {}).get("discharge_kw"),
    ),
    FusionSolarSensorDescription(
        key="battery_soc",
        name="Batteriefuellstand",
        icon="mdi:battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: (data.get("battery") or {}).get("soc_percent"),
    ),
    FusionSolarSensorDescription(
        key="battery_stored_energy",
        name="Batterieenergie",
        icon="mdi:battery-high",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: (data.get("battery") or {}).get("stored_kwh"),
    ),
    FusionSolarSensorDescription(
        key="battery_mode",
        name="Lademodus",
        icon="mdi:battery-heart-variant",
        value_fn=lambda data: (data.get("battery") or {}).get("mode"),
    ),
    FusionSolarSensorDescription(
        key="pv_energy_today",
        name="PV-Erzeugung heute",
        icon="mdi:solar-power-variant",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda data: (data.get("today") or {}).get("pv_kwh"),
    ),
    FusionSolarSensorDescription(
        key="load_energy_today",
        name="Verbrauch heute",
        icon="mdi:home-lightning-bolt",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda data: (data.get("today") or {}).get("load_kwh"),
    ),
    FusionSolarSensorDescription(
        key="grid_import_energy_today",
        name="Netzbezug heute",
        icon="mdi:transmission-tower-import",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda data: (data.get("today") or {}).get("grid_import_kwh"),
    ),
    FusionSolarSensorDescription(
        key="grid_export_energy_today",
        name="Einspeisung heute",
        icon="mdi:transmission-tower-export",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda data: (data.get("today") or {}).get("grid_export_kwh"),
    ),
    FusionSolarSensorDescription(
        key="battery_charged_energy_today",
        name="Batterie geladen heute",
        icon="mdi:battery-plus",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda data: (data.get("today") or {}).get("battery_charged_kwh"),
    ),
    FusionSolarSensorDescription(
        key="battery_discharged_energy_today",
        name="Batterie entladen heute",
        icon="mdi:battery-minus",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda data: (data.get("today") or {}).get("battery_discharged_kwh"),
    ),
    FusionSolarSensorDescription(
        key="autarky",
        name="Autarkie",
        icon="mdi:home-battery",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: (data.get("today") or {}).get("autarky_percent"),
    ),
    FusionSolarSensorDescription(
        key="energy_flow",
        name="Energiefluss",
        icon="mdi:transit-connection-variant",
        value_fn=_flow_summary,
        attributes_fn=lambda data: {"flows": data.get("flows", [])},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Sensoren anlegen."""
    coordinator: FusionSolarCoordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data or {}
    station = data.get("station") or {}

    device_info = DeviceInfo(
        identifiers={(DOMAIN, station.get("dn") or entry.entry_id)},
        name=station.get("name") or "FusionSolar",
        manufacturer=MANUFACTURER,
        model="FusionSolar Cloud",
        configuration_url=entry.data.get("base_url"),
    )

    async_add_entities(
        FusionSolarSensor(coordinator, description, device_info)
        for description in SENSORS
    )


class FusionSolarSensor(CoordinatorEntity[FusionSolarCoordinator], SensorEntity):
    """Ein Messwert der Anlage."""

    _attr_has_entity_name = True
    entity_description: FusionSolarSensorDescription

    def __init__(
        self,
        coordinator: FusionSolarCoordinator,
        description: FusionSolarSensorDescription,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        station_dn = (coordinator.data or {}).get("station", {}).get("dn") or "station"
        self._attr_unique_id = f"{DOMAIN}_{station_dn}_{description.key}"
        self._attr_device_info = device_info

    @property
    def native_value(self) -> Any:
        if not self.coordinator.data:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self.entity_description.attributes_fn or not self.coordinator.data:
            return None
        return self.entity_description.attributes_fn(self.coordinator.data)
