from ctypes import cast
import logging

from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from .coordinator import ESPCoordinator
from .util import *
from .const import *


_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, config_entry, async_add_entities):
    coordinator = hass.data[ADDONS_COORDINATOR]
    sensors = [
        ESPSensor(coordinator, "pool"),
        ESPSensor(coordinator, "spa")
    ]
    async_add_entities(sensors)

class ESPSensor(CoordinatorEntity, SensorEntity):

    def __init__(self, coordinator:ESPCoordinator, body_type:str):
        _LOGGER.info("ESPSensor.__init__")
        _LOGGER.debug(f"...BodyType[{body_type}]")

        super().__init__(coordinator)

        config = coordinator.get_config(body_type)
        prefix = config.get(CONFIG_SCREENLOGIC_PREFIX)
        self._body_type = body_type
        self._attr_unique_id = f"{prefix}_{body_type}"
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

    @property
    def native_value(self):
        """The State Machine state string."""
        #_LOGGER.info("ESPSensor.native_value")
        context:Context = self.coordinator.contexts.get(self._body_type)
        if context is None:
            _LOGGER.error(f"ESPSensor.native_value: Context is None for body type {self._body_type}")
            return None
        
        return context.machine_state

    @property
    def extra_state_attributes(self):
        """All the attributes."""
        #_LOGGER.info("ESPSensor.extra_state_attributes")
        context:Context = self.coordinator.get_context(self._body_type)
        if context is None:
            _LOGGER.error(f"ESPSensor.extra_state_attributes: Context is None for body type {self._body_type}")
            return {}
        
        esp = context.esp
        if esp is None:
            _LOGGER.error(f"ESPSensor.extra_state_attributes: ESP is None for body type {self._body_type}")
            return {}
        
        if type(esp) is not ESP:
            _LOGGER.error(f"ESPSensor.extra_state_attributes: ESP is not of type ESP for body type {self._body_type}")
        
        return {
            "body"           : self._body_type,
            "status"         : context.status,
            "seconds"        : esp.seconds,
            "confidence_num" : esp.confidence,
            "confidence_str" : esp.confidence_label,
            "water_temp"     : context.water_temp,
            "setpoint"       : context.target_temp,
            "air_temp"       : context.air_temp,
            "climate_status" : context.climate_status,
            "climate_mode"   : context.climate_mode,
            "circuit"        : context.circuit
        }

    @property
    def device_info(self):
        """Links this entity to the Pentair device."""
        #_LOGGER.info("ESPSensor.device_info")

        config = self.coordinator.config
        prefix = config[CONFIG_SCREENLOGIC_PREFIX]
        identifiers = {(DOMAIN, config[CONFIG_SCREENLOGIC_ID])}
        name = config[CONFIG_SCREENLOGIC_NAME]
        #_LOGGER.debug(f"...Identifier[{identifiers}] Name[{name}]")

        device_info = DeviceInfo(
            identifiers=identifiers,
            name=name,
            manufacturer="Strebor Tech",
            model="Pentair ScreenLogic"
            ##via_device=(SCREENLOGIC_DOMAIN, config[CONFIG_SCREENLOGIC_ID]),
        )

        #_LOGGER.debug(f"...DeviceInfo: {device_info}")

        return device_info

