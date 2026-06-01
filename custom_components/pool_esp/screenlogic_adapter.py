###
### ----- Class ScreenlogicAdapter ------------------------------------------------
###
import logging
from dataclasses_json import config
import homeassistant

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import *
from .util import ESPException, EntityCombo, PoolAdapter

_LOG = logging.getLogger(__name__)

BODY_TYPE_POOL = "pool"
BODY_TYPE_SPA = "spa"

###
### EntityCombo<datatype>:<entity_id>[/attribute]:[WATCH|IGNORE]
###
BODY_CONFIG_TEMPLATES = {
    WATER_TEMP      : "float:climate.{prefix}_{body_type}_heat/current_temperature:WATCH",
    CLIMATE_MODE    : "str:climate.{prefix}_{body_type}_heat:WATCH",
    CLIMATE_STATUS  : "str:climate.{prefix}_{body_type}_heat/hvac_action:WATCH",
    TARGET_TEMP     : "float:climate.{prefix}_{body_type}_heat/temperature:WATCH",
    AIR_TEMP        : "float:sensor.{prefix}_air_temperature:IGNORE",
    CIRCUIT         : "str:switch.{prefix}_{body_type}:WATCH"
}

class ScreenlogicAdapter(PoolAdapter):

    ### ----- init
    def __init__(self, hass:homeassistant):
        self._hass = hass
        self._name = "Screenlogic"
        self._body_types = [ BODY_TYPE_POOL, BODY_TYPE_SPA ]
        self._adapter_config = {}
        self._body_config = {} # Map of BodyTypes to Map<KEYWORD> -> EntityCombo
        self._watch_entities = {} # Map of BodyTypes to List[EntityId]

        self.discover()
    
    def __str__(self):
        return f"ScreenLogicAdapter: name[{self._name}] body_types[{self._body_types}] adapter_config[{self._adapter_config}] body_config[{self._body_config}] watch_entities[{self._watch_entities}]"   

    ### ----- name
    @property
    def name(self):
        return self._name

    @property
    def config(self):
        return self._adapter_config
    
    @property
    def watch_entities(self) -> dict:
        return self._watch_entities
    
    @property
    def all_watch_entities(self) -> list[str]:
        all_watch_entities = set()
        for body_type in BODY_TYPES:
            all_watch_entities.update(self._watch_entities.get(body_type))

        return all_watch_entities

    def getBodyTypes(self) -> list[str]:
        """ Get the list of body types """
        return self._body_types
    
    def getBodyConfig(self, body_type:str) -> dict[str, EntityCombo]:
        """ Get the ConfigBody by BodyType """
        if not self._body_config:
            raise ValueError(f"Body config for [{body_type}] not found")
        return self._body_config.get(body_type, {})
    
    ###
    ### ----- discover --------------------------------------------------------
    ###
    def discover(self) -> dict:
        """
        Discovery Home Assistant ScreenLogic's Pool and Spa Entities.
        Returns a Map of BodyTypes -> a Map of EntityTypes -> Entity Ids
        """

        dev_reg = dr.async_get(self._hass)

        sl_device = None
        for device in dev_reg.devices.values():
            if not sl_device:
                # Match on manufacturer since identifiers is empty
                manufacturer = device.manufacturer
                config_entries = device.config_entries

                if manufacturer and manufacturer.lower() == "pentair":
                    # Look up the config entries to find the integration domain
                    for entry_id in device.config_entries:
                        entry = self._hass.config_entries.async_get_entry(entry_id)
                        if entry:
                            domain = entry.domain
                            if (domain and domain.lower() == "screenlogic"):
                                sl_device = device
                                break
                        # end if entry
                    # end for entry_id
                # end if "pentair"
            else:
                break
        # end for device

        if sl_device is None:
            _LOG.warning(f"No ScreenLogic Device found")
            raise ESPException("No ScreenLogic device found")
        
        connections = sl_device.connections
        if connections:
            for e in connections:
                key = e[0]
                if (key == "mac"):
                    unique_id = e[1]
                    break
                # endif key is 'mac'
            # end for each connection

        name = sl_device.name
        prefix = name.lower().replace(":", "").replace("-", "_").replace(" ", "_").replace("__", "_")
        id = sl_device.id
        model = sl_device.model

        self._adapter_config = { }

        self._adapter_config[POOL_NAME] = name
        self._adapter_config[POOL_MODEL] = model
        self._adapter_config[POOL_PREFIX] = prefix
        self._adapter_config[POOL_ID] = id
        self._adapter_config[POOL_UNIQUE_ID] = unique_id

        ## Add Body Configurations and Watch Entities
        for body_type in self._body_types:
            self._body_config[body_type] = self._get_config_by_body_type(body_type)
            self._watch_entities[body_type] = self._get_watch_entities_by_body_type(body_type)
        
        _LOG.info(f"Found ScreenLogic Device: {prefix}")

        return self._adapter_config
    


    def _get_config_by_body_type(self, body_type:str):
        prefix = self._adapter_config[POOL_PREFIX]
        #
        # Build map of ScreenLogic Entities from the BODY_CONFIG_TEMPLATES
        # <KEYWORD> -> EntityCombo
        #
        body_config  = {
            name: EntityCombo(entity_combo.format(prefix=prefix, body_type=body_type))
            for name, entity_combo in BODY_CONFIG_TEMPLATES.items()
        }

        return body_config

    def _get_watch_entities_by_body_type(self, body_type: str) -> set[str]:
        """
        Build map of body_type -> List of ScreenLogic Entities to watch
        """

        watch_entities = set()

        if (self._body_config is not None):
            # Map of Keyword to EntityComboOk
            for metadata, entity_combo in self._body_config[body_type].items():
                #_LOG.debug(f"...{metadata} : {entity_combo}")
                if entity_combo.watch:
                    watch_entities.add(entity_combo.id)
            # end for each body_config.item
        else:
            _LOG.error(f"_get_watch_entities: Failed, config is None")
            raise ESPException("ERROR", "Failed to get watch entities")

        return watch_entities


