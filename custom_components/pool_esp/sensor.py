from __future__ import annotations

from ctypes import cast
import logging

from custom_components.pool_esp.bricks import STATE_OFF, STATE_ENABLED, STATE_SENSING, STATE_HEATING, STATE_READY, STATE_MAINTAINING, STATE_STANDBY, STATE_DISABLED
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import DeviceInfo, HomeAssistant
from homeassistant.components.sensor import ConfigEntry, SensorEntity, SensorDeviceClass

from .util import *
from .const import *

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .coordinator import ESPCoordinator

_LOG = logging.getLogger(__name__)

async def async_setup_entry(hass:HomeAssistant, config_entry:ConfigEntry, async_add_entities):
    from .coordinator import ESPCoordinator

    coordinator:ESPCoordinator = hass.data[DOMAIN][config_entry.entry_id][ADDONS_COORDINATOR]

    sensors = [
        ESPSensor(coordinator, BODY_TYPE_POOL),
        ESPSensor(coordinator, BODY_TYPE_SPA)
    ]
    async_add_entities(sensors)

    coordinator.add_sensor(BODY_TYPE_POOL, sensors[0])
    coordinator.add_sensor(BODY_TYPE_SPA, sensors[1])

class ESPSensor(CoordinatorEntity, SensorEntity):
    
    def __init__(self, coordinator:ESPCoordinator, body_type:str):

        super().__init__(coordinator)

        config = coordinator.get_config(body_type)
        prefix = config.get(POOL_PREFIX)
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
        context:Context = self.coordinator.contexts.get(self._body_type)
        if context is None:
            _LOG.error(f"ESPSensor.native_value: Context is None for body type {self._body_type}")
            return None
        
        return context.machine_state

    @property
    def extra_state_attributes(self):
        """All the attributes."""
        context:Context = self.coordinator.get_context(self._body_type)
        if context is None:
            _LOG.error(f"ESPSensor.extra_state_attributes: Context is None for body type [{self._body_type}]")
            return {}
        
        esp = context.esp
        if esp is None:
            _LOG.error(f"ESPSensor.extra_state_attributes: ESP is None for body type [{self._body_type}]")
            return {}
        
        if type(esp) is not ESP:
            _LOG.error(f"ESPSensor.extra_state_attributes: ESP is not of type ESP for body type [{self._body_type}]")
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

