import logging
import time
import traceback
import debugpy

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.event import async_track_state_change_event

from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    HomeAssistant,
    Event
)
from .const import *
from .util import *
from .state_machine import esp_state_machine

_LOG = logging.getLogger(__name__)

###
### ----- Class ESPCoordinator -------------------------------------------------
###

class ESPCoordinator(DataUpdateCoordinator):
# your state machine and ETA logic lives here:

    def __init__(self, hass:HomeAssistant, config_entry:ConfigEntry, pool_adapter:PoolAdapter) -> None:
        
        _LOG.info(f"ESPCoordinator.__init__")
        _LOG.debug(f"ConfigEntry: {type(config_entry)}:{config_entry}")
        _LOG.debug(f"PoolAdapter: {type(pool_adapter)} {pool_adapter}")

        super().__init__(
            hass,
            _LOG,
            name=DOMAIN
        )
        self._pool_adapter = pool_adapter
        self._config = pool_adapter.config                      # PoolAdapter Config
        self._watch_entities = pool_adapter.watch_entities      # Pool Entities we're watching
        self._contexts = {}                                     # {body_type: Context}
        self._unsub = []                                        # state change listeners

    ###
    ### ----- HA Required Functions --------------------------------------------
    ###
    async def async_setup(self) -> None:
        """Called once after integration loads."""
        _LOG.info(f"ESPCoordinator.async_setup")
        _LOG.debug(f"PoolAdapter: {self._pool_adapter}")

        # Initialize Contexts and Config for Body Types
        for body_type in BODY_TYPES:
            config = self.get_config(body_type)
            _LOG.debug(f"...Config [{body_type}] : {config}")

            context = Context(body_type)
            context.config = config     # Context has a reference to Config
            context.coordinator = self  # Context has a reference to this Coordinator
            context.hass = self.hass  # Context has a reference to the Home Assistant instance
            self._contexts[body_type] = context

        _LOG.debug(f"...WatchEntities: {self._watch_entities}")

        # Register state change listeners
        # Calls "_handle_state_change" when any Watch Entity changes
        #
        # Create a set of all BodyType watch entities
        all_watch_entities = self._pool_adapter.all_watch_entities

        _LOG.debug(f"...AllWatchEntities: {all_watch_entities}")

        self._unsub.append(
            async_track_state_change_event(
                self.hass,
                all_watch_entities,
                self._handle_state_change
            )
        )

        ###
        ### Initialize all BodyType ESP Values
        ###
        for body_type in BODY_TYPES:
            context = self.get_context(body_type)
            #_LOG.debug(f"...Context: {context}")
            await self._execute_with_current_data(context, "Initalization")
    
    def get_context(self, body_type: str) -> Context:
        """ Get Context by body_type """
        return self._contexts.get(body_type, None)
    
    def get_config(self, body_type: str) -> Config:
        """ Get Config by body_type """
        return self._pool_adapter.getBodyConfig(body_type)

    def _get_body_type_by_entity(self, entity_id) -> str:
        """ Get the Body Type for the specified entity """
        for body_type in BODY_TYPES:
            entities = self._watch_entities.get(body_type)
            if entity_id in entities:
                return body_type
        
        return None

    def get_config_entities(self, body_type = None) -> set[str]:
        """
        Get ALL the Unique 'body_type' Config Entities.
        Typically used to get the unique set of Entities
        prior to fetching from the HA Recorder.
        Return: Set of HA Entity Ids
        """

        if body_type is None:
            raise ESPException("ERROR", "body_type is None")

        entities = set()

        # Map of Keyword to EntityCombo
        body_config = self._pool_adapter.getBodyConfig(body_type)
        for metadata, entity_combo in body_config.items():
            entities.add(entity_combo.id)

        return entities

    def _what_changed(self, body_type, entity_id, old_state, new_state) -> set[str]:
        """
        Return a set of what has changed between the old & new states
        <body_type>:[state|attr]:<value>
        """
        #_LOG.info(f"_what_changed: {body_type} {entity_id}, {old_state} {new_state}")
        changes = set()

        if entity_id is not None and old_state is not None and new_state is not None:
            #_LOG.debug(f"*** {type(old_state)} {old_state}")
            old_attrs = old_state.attributes
            new_attrs = new_state.attributes

            old_state = old_state.state
            new_state = new_state.state
            if old_state != new_state:
                #_LOG.debug(f"...[{entity_id}] State [{old_state} -> {new_state}]")
                changes.add(f"{body_type}:{ATTR_STATE}:{new_state}")

            for attr in [ATTR_CURRENT_TEMP, ATTR_TEMP, ATTR_HVAC_ACTION, ATTR_HVAC_MODE]:
                old = old_attrs.get(attr)
                new = new_attrs.get(attr)
                if old is not None and new is not None and old != new:
                    changes.add(f"{body_type}:{attr}:{new}")
                    #_LOG.debug(f"...Add [{entity_id}.{attr}] {old} -> {new}")
        else:
            _LOG.warning(f"ArgError; EntityId {entity_id}, OldState {old_state}, NewState {new_state}")

        return changes # will be an empty Set if nothing changed

    def _get_current_value(self, entity_combo:EntityCombo):
        """
        Read current state value from HA entity, respecting optional /attribute suffix.
        """
        try:
            entity_state = self.hass.states.get(entity_combo.id)
            if entity_state is not None:
                value = entity_state.attributes.get(entity_combo.attribute) if (entity_combo.attribute) else entity_state.state

                if entity_combo.datatype == "float":
                    if value not in (None, "unavailable", "unknown"):
                        value = float(value)
                    else:
                        value = -1  # Unavailable or Unknown

                return value
            else:
                _LOG.warning(f"get_current_value:EntityState is None for {entity_combo.id}")
        except Exception as e:
            _LOG.error(f"Failed to get_current_value for [{entity_combo}]: {e}")


    async def _execute_with_current_data(self, context: Context, cause: str = "Unknown"):
        """
        Execute the State Machine with Current ScreenLogic data
        """
        try:
            #_LOG.info(f"execute_with_current_data: Cause[{cause}]")

            body_type = context.body_type
            #_LOG.debug(f"...BodyType[{body_type}]")
            config = context.config

            #_LOG.debug(f"...Context: {context}")
            #_LOG.debug(f"...Config : {config}")

            ### Load up Context with Current Data
            context.timestamp       = time.time()
            context.air_temp        = self._get_current_value(config[AIR_TEMP])
            context.water_temp      = self._get_current_value(config[WATER_TEMP])
            context.target_temp     = self._get_current_value(config[TARGET_TEMP])
            context.climate_mode    = self._get_current_value(config[CLIMATE_MODE])
            context.climate_status  = self._get_current_value(config[CLIMATE_STATUS])
            context.circuit         = self._get_current_value(config[CIRCUIT])
            context.sm_state        = cause
            context.export          = True  # Export history data

            #
            # Execute State Machine - Bricks save STATUS & ESP in the Context
            #
            await esp_state_machine(context, cause)

            esp = context.esp
            if esp is not None:
                status = esp.display_label if (esp is not None) else "Unknown"
                seconds = esp.seconds if (esp is not None) else 0
                confidence = esp.confidence_label if (esp is not None) else "Unknown"
                _LOG.debug(f"...Status[{status}] ESP[{seconds}] Confidence[{confidence}]")
            else:
                _LOG.warning(f"...No ESP data available")
                
        except Exception as e:
            _LOG.error(f"_execute_with_current_data: Error [{e}] Context[{context}]  ")
            _LOG.error(traceback.format_exc())

    ###
    ### ---- handle_state_change
    ###
    ### Called when a Watch Entity changes
    ###
    async def _handle_state_change(self, event: Event):
        """Equivalent to your @event_trigger handler."""
        changes = None
        entity_id = event.data.get("entity_id")

        body_type = self._get_body_type_by_entity(entity_id)
        if not body_type:
            _LOG.warning(f"ESPCoordinator._handle_state_change: Failed to find BodyType for Entity [{entity_id}]")
            return

        # Filter — only process entities we care about
        if not self._pool_adapter or not self._watch_entities or not self.contexts:
            return   # setup hasn't completed yet

        watch_entities = self._watch_entities.get(body_type)
        
        if entity_id in watch_entities:
            try:
                _LOG.info(f"ESPCoordinator._handle_state_change: [{entity_id}]")

                old_state = event.data.get("old_state")
                new_state = event.data.get("new_state")

                context = self.get_context(body_type)
                context.changes = None

                changes = self._what_changed(body_type, entity_id, old_state, new_state)
                #_LOG.debug(f"...Changes: {changes}")
                if len(changes) != 0:
                    context.changes = changes
                
                await self._execute_with_current_data(context, changes)
            except Exception as e:
                details = (f"ESPCoordinator._handle_state_change: Failed [{body_type}] Entity[{entity_id}]; {e}")
                _LOG.info(details)
                _LOG.error(traceback.format_exc())
                raise ESPException("ERROR", details) from e
        # end if

        # Tell HA entities to update
        await self.async_request_refresh()

    async def _async_update_data(self):
        _LOG.info(f"ESPCoordinator._async_update_data")
        #_LOG.debug(f"...Data: {self.data}")
        return self.data

###
### ----- Coordinator Config
###     Contains configuration data
###     See also consts CONFIG literals
###
    @property
    def adapter_config(self) -> dict:
        """
        Get the Coordinator Adapter Configuration data
        """
        return self._pool_adapter.config

###
### ----- Coordinator Contexts
###     Contains Pool and Spa Context data
###     See also consts CONTEXT literals
###
    @property
    def contexts(self) -> dict:
        """
        Get the Coordinator Context data
        """
        return self._contexts

    @contexts.setter
    def contexts(self, value):
        """
        Set the Coordinator Context data
        """
        self._contexts = value
