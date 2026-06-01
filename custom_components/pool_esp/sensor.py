from __future__ import annotations

from ctypes import cast
import logging

from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import DeviceInfo, HomeAssistant
from homeassistant.components.sensor import ConfigEntry, SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfTime

from .util import *
from .const import CONF_HEATER_FUEL_TYPE, CONF_GAS_COST_PER_THERM, CONF_GAS_HEATER_BTU, CONF_ELECTRIC_HEATER_KW, CONF_ELECTRIC_COST_PER_KWH


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

###
### ----- HeaterCostSensor: Exposes estimated heater costs as a SensorEntity. One per body type. -----
###

class HeaterCostSensor(CoordinatorEntity, SensorEntity):
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
        return round(self._coordinator.get_persistence(self._body_type).total_cost, 2)
    
    @property
    def extra_state_attributes(self) -> dict:
        options      = self._coordinator.config_entry.options
        fuel_type    = options.get(CONF_HEATER_FUEL_TYPE, "unknown")
        cost_per_hour = self._get_cost_per_hour(options, fuel_type)
        runtime_min  = self._coordinator.get_persistence(self._body_type).total_runtime_minutes

        attrs = {
            "fuel_type":             fuel_type,
            "cost_per_hour":         cost_per_hour,
            "total_runtime_minutes": round(runtime_min, 1),
            "total_runtime_hours":   round(runtime_min / 60.0, 2),
        }

        # Fuel-specific attributes
        if fuel_type == "gas":
            attrs["btu_rating"]       = options.get(CONF_GAS_HEATER_BTU, 0)
            attrs["cost_per_therm"]   = options.get(CONF_GAS_COST_PER_THERM, 0.0)
        elif fuel_type in ("heat_pump", "electric"):
            attrs["heater_kw"]        = options.get(CONF_ELECTRIC_HEATER_KW, 0)
            attrs["cost_per_kwh"]     = options.get(CONF_ELECTRIC_COST_PER_KWH, 0.0)
            attrs["total_kwh"]        = round((runtime_min / 60.0) * 
                                        options.get(CONF_ELECTRIC_HEATER_KW, 0), 2)

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

    @property
    def device_info(self):
        """Links this entity to the Pool device."""

        adapter_config = self.coordinator.adapter_config
        prefix = adapter_config[POOL_PREFIX]
        identifiers = {(DOMAIN, adapter_config[POOL_ID])}
        name = adapter_config[POOL_NAME]

        device_info = DeviceInfo(
            identifiers=identifiers,
            name=name,
            manufacturer="Strebor Tech",
            model="Pentair ScreenLogic"
            ##via_device=(SCREENLOGIC_DOMAIN, config[CONFIG_SCREENLOGIC_ID]),
        )

        return device_info

    def add_interval_cost(self, duration_minutes: float):
        """Called when a heating interval completes."""
        heating_type = self._coordinator.get_option(CONF_HEATER_FUEL_TYPE, 0.0)
        if heating_type == "gas":
            cost_per_unit = self._coordinator.get_option(CONF_GAS_COST_PER_THERM, 0.0)
            btu_per_unit = self._coordinator.get_option(CONF_GAS_HEATER_BTU, 0.0)
            if cost_per_unit > 0 and btu_per_unit > 0:
                cost = (duration_minutes / 60.0) * (btu_per_unit / 100000) * cost_per_unit
                self._total_cost += cost
                self.async_write_ha_state()
                _LOG.debug(f"HeaterCostSensor({self._attr_unique_id}) added [${cost:.2f}] for [{duration_minutes:.1f} min] total[${self._total_cost:.2f}]")
        elif heating_type in ("heat_pump", "electric"):
            cost_per_hour = self._coordinator.get_option(CONF_ELECTRIC_COST_PER_KWH, 0.0)
            heater_kw = self._coordinator.get_option(CONF_ELECTRIC_HEATER_KW, 0.0)
            if cost_per_hour > 0 and heater_kw > 0:
                cost = (duration_minutes / 60.0) * heater_kw * cost_per_hour
                self._total_cost += cost
                self.async_write_ha_state()
                _LOG.debug(f"HeaterCostSensor({self._attr_unique_id}) added [${cost:.2f}] for [{duration_minutes:.1f} min] total[${self._total_cost:.2f}]")

# end class HeaterCostSensor

###
### ----- HeaterRuntimeSensor: Exposes cumulative heater runtime as a SensorEntity. One per body type. -----
###

class HeaterRuntimeSensor(CoordinatorEntity, SensorEntity):
    """Accumulates heater runtime in minutes."""

    _attr_device_class               = SensorDeviceClass.DURATION
    _attr_state_class                = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon                       = "mdi:timer-outline"

    def __init__(self, coordinator:ESPCoordinator, body_type:str):

        super().__init__(coordinator)

        self._total_minutes  = 0.0

        self._coordinator    = coordinator
        self._body_type      = body_type
        prefix               = coordinator.adapter_config.get(POOL_PREFIX)

        self._attr_unique_id = f"{prefix}_{body_type}_heater_runtime"
        self._attr_name      = f"{body_type.capitalize()} Heater Runtime"

    def __str__(self) -> str:
        return f"HeaterRuntimeSensor({self._attr_unique_id}) total_minutes[{self._total_minutes:.1f}]"

    @property
    def native_value(self) -> float:
        return round(self._coordinator.get_persistence(self._body_type).total_runtime_minutes, 1)

    @property
    def device_info(self):
        """Links this entity to the Pool device."""

        adapter_config = self.coordinator.adapter_config
        prefix = adapter_config[POOL_PREFIX]
        identifiers = {(DOMAIN, adapter_config[POOL_ID])}
        name = adapter_config[POOL_NAME]

        device_info = DeviceInfo(
            identifiers=identifiers,
            name=name,
            manufacturer="Strebor Tech",
            model="Pentair ScreenLogic"
            ##via_device=(SCREENLOGIC_DOMAIN, config[CONFIG_SCREENLOGIC_ID]),
        )

        return device_info

    def add_interval_runtime(self, duration_minutes:float):
        """Called when a heating interval completes."""
        self._total_minutes += duration_minutes
        self.async_write_ha_state()
        _LOG.debug(f"HeaterRuntimeSensor({self._attr_unique_id}) added [{duration_minutes:.1f} min] total[{self._total_minutes:.1f} min]")

# end class HeaterRuntimeSensor

###
### ----- ESPSensor: Exposes ESP state and attributes as a SensorEntity. One per body type. -----
###

class ESPSensor(CoordinatorEntity, SensorEntity):
    
    def __init__(self, coordinator:ESPCoordinator, body_type:str):

        from custom_components.pool_esp.bricks import SM_START, STATE_OFF, STATE_ENABLED, STATE_SENSING, STATE_HEATING, STATE_READY, STATE_MAINTAINING, STATE_STANDBY, STATE_DISABLED

        super().__init__(coordinator)

        self._coordinator  = coordinator
        self._body_type    = body_type
        prefix    = coordinator.adapter_config.get(POOL_PREFIX)
        
        self._attr_unique_id = f"{prefix}_{body_type}_esp"
        self._attr_name = f"{body_type.capitalize()} ESP"
        self._attr_icon = "mdi:pool-thermometer"
        self._attr_device_class = SensorDeviceClass.ENUM
        self._attr_options = [
            SM_START,
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
        return f"ESPSensor[{self._attr_unique_id}] state[{self.native_value}]"

    @property
    def native_value(self):
        """The State Machine state string."""
        context:Context = self.coordinator.contexts.get(self._body_type)
        if context is None:
            _LOG.error(f"ESPSensor.native_value: [{self._body_type}]Context is None")
            return None
        
        return context.machine_state

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

    @property
    def device_info(self):
        """Links this entity to the Pentair device."""

        adapter_config = self.coordinator.adapter_config
        prefix = adapter_config[POOL_PREFIX]
        identifiers = {(DOMAIN, adapter_config[POOL_ID])}
        name = adapter_config[POOL_NAME]

        device_info = DeviceInfo(
            identifiers=identifiers,
            name=name,
            manufacturer="Strebor Tech",
            model="Pentair ScreenLogic"
            ##via_device=(SCREENLOGIC_DOMAIN, config[CONFIG_SCREENLOGIC_ID]),
        )

        return device_info

# end class ESPSensor

