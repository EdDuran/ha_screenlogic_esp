###
### ----- Class ScreenlogicAdapter ------------------------------------------------
###
import logging
from custom_components.pool_esp.const import POOL_DOMAIN
from dataclasses_json import config
import homeassistant

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntry


from homeassistant.config_entries import ConfigEntry, ConfigEntryState

from .const import *
from .util import ESPException, EntityCombo, PoolAdapter

_LOG = logging.getLogger(__name__)

SL_DOMAIN = "screenlogic"
SL_TECHNOLOGY = "ScreenLogic"
SL_CONTROLLER_STATE_READY = "ready"
SL_CONTROLLER_STATE_SYNC = "sync"
SL_CONTROLLER_STATE_SERVICE = "service"

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

TEST_ENTITY_TEMPLATE = "str:sensor.{prefix}_controller_state:IGNORE"

class ScreenlogicAdapter(PoolAdapter):

    ### ----- init
    def __init__(self, hass:homeassistant):

        super().__init__(self)

        self._hass:homeassistant = hass
        self._name:str = "Screenlogic"
        self._body_types:list = [ BODY_TYPE_POOL, BODY_TYPE_SPA ]
        self._body_config:dict = {} # Map of BodyTypes to Map<KEYWORD> -> EntityCombo
        self._watch_entities:dict = {} # Map of BodyTypes to List[EntityId]

        self._discover()
        self.is_ready = self._test_device()
    
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
    def _discover(self) -> dict:
        """
        Discovery Home Assistant ScreenLogic's Pool and Spa Entities.
        Returns a Map of BodyTypes -> a Map of EntityTypes -> Entity Ids
        """

        ###
        ### Get the (first) Screenlogic Integration ConfigEntry
        ###
        entries = self._hass.config_entries.async_entries(SL_DOMAIN)

        if not entries:
            # Screenlogic Integration is not installed
            raise ESPException(f"ScreenlogicAdapter: Integration [{SL_DOMAIN}] is not installed")

        if len(entries) != 1:
            raise ESPException(f"ScreenlogicAdapter: Integration [{SL_DOMAIN}] contains [{len(entries)}] entries; Pool ESP supports only one")

        entry:ConfigEntry = entries[0]
    
        # Screenlogic is not Loaded
        if entry.state != ConfigEntryState.LOADED:
            reason = entry.reason if entry.reason is not None else "Unknown"
            raise ESPException(f"ScreenlogicAdapter: Integration [{SL_DOMAIN}] is not Loaded; State[{entry.state.value}] Reason[{reason}]")
        
        prefix = entry.title.lower().replace(":", "").replace("-", "_").replace(" ", "_").replace("__", "_")
        unique_id = entry.unique_id
        _LOG.info(f"Integration [{SL_DOMAIN}] unique_id is [{entry.unique_id}]")

        ###
        ### Get the (first) Screenlogic Integration Device
        ###

        dev_reg = dr.async_get(self._hass)

        devices = dr.async_entries_for_config_entry(dev_reg, entry.entry_id)

        if not devices:
            # Screenlogic Integration is not installed
            raise ESPException(f"ScreenlogicAdapter: Integration [{SL_DOMAIN}] is not installed")

        if len(devices) != 1:
            raise ESPException(f"ScreenlogicAdapter: Integration [{SL_DOMAIN}] contains [{len(entries)}] devices; Pool ESP supports only one")

        device:DeviceEntry = devices[0]

        ###
        ### Build the Screenlogic Adapter Configuration
        ###

        self._adapter_config = { }

        self._adapter_config[POOL_NAME] = device.name
        self._adapter_config[POOL_MANUFACTURER] = device.manufacturer
        self._adapter_config[POOL_MODEL] = device.model
        self._adapter_config[POOL_PREFIX] = prefix
        self._adapter_config[POOL_UNIQUE_ID] = unique_id
        self._adapter_config[POOL_DOMAIN] = SL_DOMAIN
        self._adapter_config[POOL_TECHNOLOGY] = SL_TECHNOLOGY

        ###
        ### Add Body Configurations and Watch Entities
        ###
        for body_type in self._body_types:
            self._body_config[body_type] = self._get_config_by_body_type(body_type)
            self._watch_entities[body_type] = self._get_watch_entities_by_body_type(body_type)
        
        _LOG.debug(f"Integration [{SL_DOMAIN}] discovered Device [{device.name}]")

    def _test_device(self) -> bool:        
        ###
        ### Get data from the Screenlogic Device
        ###
        entity_combo = EntityCombo(TEST_ENTITY_TEMPLATE.format(prefix=self._adapter_config[POOL_PREFIX]))
        _LOG.debug(f"Testing Entity [{entity_combo.id}]")

        test = self._hass.states.get(entity_combo.id)
        if test is not None:
            value = test.state
            _LOG.debug(f"...Value[{value}]")

            if value is not None and value != SL_CONTROLLER_STATE_READY:
                raise ESPException(f"ScreenlogicAdapter: Integration [{SL_DOMAIN}] Controller is not Ready; value[{value}]")
        
        return True
    


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
        Build map of body_type -> List of ScreenLogic EntityId's to watch
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
            raise ESPException("Failed to get watch entities")

        return watch_entities


