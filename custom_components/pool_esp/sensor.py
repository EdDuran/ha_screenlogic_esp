from __future__ import annotations

from ctypes import cast
import logging

from custom_components.pool_esp.util import ESP, Context
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.components.sensor import ConfigEntry, SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfTime

from .const import (
    ADDONS_COORDINATOR,
    BODY_TYPES,
    CONF_HEATER_FUEL_TYPE,
    CONF_GAS_COST_PER_THERM,
    CONF_GAS_HEATER_BTU,
    CONF_ELECTRIC_HEATER_KW,
    CONF_ELECTRIC_COST_PER_KWH,
    DOMAIN,
    INTEGRATION_NAME,
    MANUFACTURER,
    POOL_NAME,
    POOL_PREFIX,
    POOL_TECHNOLOGY,
    POOL_UNIQUE_ID,
    SM_START
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .coordinator import ESPCoordinator

_LOG = logging.getLogger(__name__)

async def async_setup_entry(hass:HomeAssistant, config_entry:ConfigEntry, async_add_entities):
    from .coordinator import ESPCoordinator

    coordinator:ESPCoordinator = hass.data[DOMAIN][config_entry.entry_id][ADDONS_COORDINATOR]

    for body_type in BODY_TYPES:
        coordinator.add_sensor(body_type, ESPSensor(coordinator, body_type))
        coordinator.add_sensor(body_type, HeaterCostSensor(coordinator, body_type))
        coordinator.add_sensor(body_type, HeaterRuntimeSensor(coordinator, body_type))

    sensors = coordinator.get_sensors()
    _LOG.debug(f"async_setup_entry: Adding sensors: {sensors}")
    async_add_entities(sensors)

###############################################################################
###
### ----- ESPEntity:
###       Defines the 'device_info'
###
###############################################################################

class ESPEntity(CoordinatorEntity):
    @property
    def device_info(self) -> DeviceInfo:
        """Links this entity to the Pentair device."""

        adapter_config = self.coordinator.adapter_config

        device_info = DeviceInfo(
            identifiers={(DOMAIN, adapter_config[POOL_UNIQUE_ID])},
            name=adapter_config[POOL_NAME],
            manufacturer=MANUFACTURER,
            model=f"{INTEGRATION_NAME} via {adapter_config[POOL_TECHNOLOGY]} {adapter_config[POOL_NAME]}",
            sw_version=self.coordinator.version
        )

        return device_info
    

###############################################################################
###
### ----- HeaterCostSensor:
###       Exposes estimated heater costs as a SensorEntity. One per body type.
###
###############################################################################

class HeaterCostSensor(ESPEntity, SensorEntity):
    """Accumulates heater operating cost over time."""

    _attr_device_class               = SensorDeviceClass.MONETARY
    _attr_state_class                = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "USD"
    _attr_icon                       = "mdi:currency-usd"

    def __init__(self, coordinator:ESPCoordinator, body_type:str):

        super().__init__(coordinator)

        self._total_cost   = 0.0

        self._coordinator:ESPCoordinator = coordinator
        self._body_type    = body_type
        prefix             = coordinator.adapter_config.get(POOL_PREFIX)

        self._attr_unique_id = f"{prefix}_{body_type}_heater_cost"
        self._attr_name      = f"{body_type.capitalize()} Heater Cost"
    
    def __str__(self) -> str:
        return f"HeaterCostSensor({self._attr_unique_id}) total_cost[${self._total_cost:.2f}]"

    @property
    def native_value(self) -> float:
        return round(self._coordinator.get_persistence().total_cost(self._body_type), 2)
    
    @property
    def extra_state_attributes(self) -> dict:
        options      = self._coordinator.config_entry.options
        fuel_type    = options.get(CONF_HEATER_FUEL_TYPE, "unknown")
        cost_per_hour = self._get_cost_per_hour(options, fuel_type)
        runtime_hours  = self._coordinator.get_persistence().total_runtime_hours(self._body_type)

        attrs = {
            "fuel_type":             fuel_type,
            "cost_per_hour":         cost_per_hour,
            "total_runtime_hours":   round(runtime_hours, 2),
        }

        # Fuel-specific attributes
        if fuel_type == "gas":
            attrs["btu_rating"]       = options.get(CONF_GAS_HEATER_BTU, 0)
            attrs["cost_per_therm"]   = options.get(CONF_GAS_COST_PER_THERM, 0.0)
        elif fuel_type in ("heat_pump", "electric"):
            attrs["heater_kw"]        = options.get(CONF_ELECTRIC_HEATER_KW, 0)
            attrs["cost_per_kwh"]     = options.get(CONF_ELECTRIC_COST_PER_KWH, 0.0)
            attrs["total_kwh"]        = round(runtime_hours * options.get(CONF_ELECTRIC_HEATER_KW, 0), 2)

        return attrs

    def _get_cost_per_hour(self, options, fuel_type) -> float:
        """Calculate effective cost per hour based on fuel type."""
        if fuel_type == "gas":
            btu        = options.get(CONF_GAS_HEATER_BTU, 0)
            per_therm  = options.get(CONF_GAS_COST_PER_THERM, 0.0)
            return round((btu / 100_000) * per_therm, 2)
        elif fuel_type in ("heat_pump", "electric"):
            kw         = options.get(CONF_ELECTRIC_HEATER_KW, 0)
            per_kwh    = options.get(CONF_ELECTRIC_COST_PER_KWH, 0.0)
            return round(kw * per_kwh, 2)
        return 0.0


    def add_interval_cost(self, duration_minutes: float):
        """Called when a heating interval completes."""
        duration_hours = duration_minutes / 60.0

        heating_type = self._coordinator.get_option(CONF_HEATER_FUEL_TYPE, 0.0)
        if heating_type == "gas":
            cost_per_unit = self._coordinator.get_option(CONF_GAS_COST_PER_THERM, 0.0)
            btu_per_unit = self._coordinator.get_option(CONF_GAS_HEATER_BTU, 0.0)
            if cost_per_unit > 0 and btu_per_unit > 0:
                cost = duration_hours * (btu_per_unit / 100000) * cost_per_unit
                self._total_cost += cost
                self.async_write_ha_state()
                _LOG.debug(f"HeaterCostSensor({self._attr_unique_id}) added [${cost:.2f}] for [{duration_hours:.2f} hours] total[${self._total_cost:.2f}]")
        elif heating_type in ("heat_pump", "electric"):
            cost_per_hour = self._coordinator.get_option(CONF_ELECTRIC_COST_PER_KWH, 0.0)
            heater_kw = self._coordinator.get_option(CONF_ELECTRIC_HEATER_KW, 0.0)
            if cost_per_hour > 0 and heater_kw > 0:
                cost = (duration_minutes / 60.0) * heater_kw * cost_per_hour
                self._total_cost += cost
                self.async_write_ha_state()
                _LOG.debug(f"HeaterCostSensor({self._attr_unique_id}) added [${cost:.2f}] for [{duration_hours:.2f} hours] total[${self._total_cost:.2f}]")

# end class HeaterCostSensor

###############################################################################
###
### ----- HeaterRuntimeSensor:
###       Exposes cumulative heater runtime as a SensorEntity. One per body type.
###
###############################################################################

class HeaterRuntimeSensor(ESPEntity, SensorEntity):
    """Accumulates heater runtime in minutes."""

    _attr_device_class               = SensorDeviceClass.DURATION
    _attr_state_class                = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_icon                       = "mdi:timer-outline"

    def __init__(self, coordinator:ESPCoordinator, body_type:str):

        super().__init__(coordinator)

        self._total_hours    = 0.0
        self._coordinator    = coordinator
        self._body_type      = body_type
        prefix               = coordinator.adapter_config.get(POOL_PREFIX)

        self._attr_unique_id = f"{prefix}_{body_type}_heater_runtime"
        self._attr_name      = f"{body_type.capitalize()} Heater Runtime"

    def __str__(self) -> str:
        return f"HeaterRuntimeSensor({self._attr_unique_id}) total_hours[{self._total_hours:.1f}]"

    @property
    def native_value(self) -> float:
        return round(self._coordinator.get_persistence().total_runtime_hours(self._body_type), 1)

    def add_interval_runtime(self, duration_minutes:float):
        """Called when a heating interval completes."""
        duration_hours = duration_minutes / 60.0
        self._total_hours += duration_hours
        self.async_write_ha_state()
        _LOG.debug(f"HeaterRuntimeSensor({self._attr_unique_id}) added [{duration_hours:.2f} hours] total[{self._total_hours:.2f} hours]")

# end class HeaterRuntimeSensor

###############################################################################
###
### ----- ESPSensor:
###       Exposes ESP state and attributes as a SensorEntity. One per body type.
###
###############################################################################

class ESPSensor(ESPEntity, SensorEntity):
    
    def __init__(self, coordinator:ESPCoordinator, body_type:str):

        from custom_components.pool_esp.bricks import STATE_OFF, STATE_ENABLED, STATE_SENSING, STATE_HEATING, STATE_READY, STATE_MAINTAINING, STATE_STANDBY, STATE_DISABLED

        super().__init__(coordinator)

        self._coordinator = coordinator
        self._body_type   = body_type
        prefix            = coordinator.adapter_config.get(POOL_PREFIX)
        
        self._attr_unique_id = f"{prefix}_{body_type}_esp"
        self._attr_name = f"{body_type.capitalize()} ESP"
        self._attr_icon = "mdi:pool-thermometer"
        self._attr_device_class = SensorDeviceClass.ENUM
        self._attr_options = [
            STATE_OFF,
            STATE_ENABLED,
            STATE_SENSING,
            STATE_HEATING,
            STATE_READY,
            STATE_MAINTAINING,
            STATE_STANDBY,
            STATE_DISABLED
        ]

    def __str__(self) -> str:
        return f"ESPSensor({self._attr_unique_id}) state[{self.native_value}]"

    @property
    def native_value(self):
        """The State Machine state string."""
        context:Context = self.coordinator.contexts.get(self._body_type)
        if context is None:
            _LOG.error(f"ESPSensor.native_value: [{self._body_type}]Context is None")
            return None
        
        return context.machine_state if context.machine_state != SM_START else None

    @property
    def extra_state_attributes(self):
        """All the attributes."""
        context:Context = self.coordinator.get_context(self._body_type)
        if context is None:
            _LOG.error(f"ESPSensor.extra_state_attributes: [{self._body_type}] Context is None")
            return {}
        
        esp = context.esp
        if esp is None:
            _LOG.debug(f"ESPSensor.extra_state_attributes: [{self._body_type}] ESP is None")
            return {}
        
        if type(esp) is not ESP:
            _LOG.error(f"ESPSensor.extra_state_attributes: [{self._body_type}] ESP is not of type ESP but [{type(esp)}]")
            return {}
        
        return {
            "body"           : self._body_type,
            "status"         : context.status,
            "seconds"        : context.seconds,
            "confidence_pct" : context.confidence_pct
        }

# end class ESPSensor

