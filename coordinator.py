import logging
import time
import traceback

from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
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

    def __init__(self, hass:HomeAssistant, config_entry:ConfigEntry, config: dict) -> None:
        _LOG.info(f"ESPCoordinator.__init__")
        _LOG.debug(f"ConfigEntry: {type(config_entry)}:{config_entry}")
        _LOG.debug(f"Config: {type(config)} {config}")

        super().__init__(
            hass,
            _LOG,
            name=DOMAIN
        )
        self._config = config         # ScreenLogic Device Config
        self._contexts = {}           # {body_type: Context}
        self._watch_entities = {}     # ScreenLogic Entities we're watching
        self._unsub = []              # state change listeners

    ###
    ### ----- HA Required Functions --------------------------------------------
    ###
    async def async_setup(self) -> None:
        """Called once after integration loads."""
        _LOG.info(f"ESPCoordinator.async_setup")
        _LOG.debug(f"Config: {self._config}")

        # Initialize Contexts and Config for Body Types
        #
        await self._build_config() ######## add CONTEXT_CONFIG

        for body_type in BODY_TYPES:
            config = self._config[body_type]
            #self._config[body_type] = config

            context = Context(body_type)
            context.config = config     # Context has a reference to Config
            context.coordinator = self  # Context has a reference to this Coordinator
            self._contexts[body_type] = context

        _LOG.debug(f"...Config: {self._config}")
        _LOG.debug(f"...WatchEntities: {self._watch_entities}")

        # Register state change listeners
        # Calls "_handle_state_change" when any Watch Entity changes
        #
        # Create a set of all BodyType watch entities
        watch_entities = set()
        for body_type in BODY_TYPES:
            body_entities = self._watch_entities.get(body_type)
            _LOG.debug(f"...{body_type} -> {type(body_entities)} : {body_entities}")
            watch_entities.update(body_entities)

        _LOG.debug(f"...AllWatchEntities: {watch_entities}")

        self._unsub.append(
            async_track_state_change_event(
                self.hass,
                watch_entities,
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
        return self._config.get(body_type, None)

    def _get_body_type_by_watch_entity(self, entity_id) -> str:
        """ Get the Body Type for the specified watch entity """
        for body_type in BODY_TYPES:
            entities = self._watch_entities.get(body_type)
            if entity_id in entities:
                return body_type
        
        return None

    def _get_watch_entities(self) -> dict:
        """
        Build map of body_type -> List of ScreenLogic Entities to watch
        """

        #_LOG.info("_get_watch_entities:")
        
        # Building Dictionary of BodyType to Set of Entities to Watch
        watch_entities = {}

        if (self._config is not None):
            for body_type in BODY_TYPES:
                #_LOG.debug(f"...BodyType[{body_type}]")
                body_type_entities = set()
                # Map of Keyword to EntityCombo
                body_config: dict[str, str] | None = self.get_config(body_type)
                for metadata, entity_combo in body_config.items():
                    #_LOG.debug(f"...{metadata} : {entity_combo}")
                    entity_type, entity_id, entity_attr, watch = parse_entity_combo(entity_combo)
                    if watch:
                        body_type_entities.add(entity_id)
                # end for each body_config.item
                #_LOG.debug(f"...BodyTypeEntities: {body_type_entities}")
                watch_entities[body_type] = body_type_entities
            # end for each body_type
        else:
            _LOG.error(f"_get_watch_entities: Failed, config is None")

        return watch_entities


    def get_config_entities(self, body_type = None) -> set(str):
        """
        Get ALL the Unique 'body_type' Config Entities.
        Typically used to get the unique set of Entities
        prior to fetching from the HA Recorder.
        Return: Set of HA Entity Ids
        """

        if body_type is None:
            raise ESPException("ERROR", "body_type is None")

        entities = set()

        #_LOG.info("get_config_entities:")

        # Map of Keyword to EntityCombo
        body_config = self._config.get(body_type)
        for metadata, entity_combo in body_config.items():
            #_LOG.debug(f"  {metadata} : {entity_combo}")
            if (not metadata.startswith("HELPER")):
                entity_type, entity_id, entity_attr, watch = parse_entity_combo(entity_combo)
                entities.add(entity_id)

        return entities

    async def _build_config(self):
        prefix = self._config[CONFIG_SCREENLOGIC_PREFIX]
        #
        # Build map of ScreenLogic Entities and add to "_config"
        #
        config_entities  = {
            body_type: {
                name: entity_combo.format(prefix=prefix, body_type=body_type)
                for name, entity_combo in BODY_CONFIG_TEMPLATES.items()
            }
            for body_type in BODY_TYPES
        }
        self._config.update(config_entities)
        #
        # Get map of Entities which will be watched by this integration
        #
        self._watch_entities = self._get_watch_entities()

    def _what_changed(self, body_type, entity_id, old_state, new_state) -> set():
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

    def _get_current_value(self, entity_combo):
        """
        Read current state value from HA entity, respecting optional /attribute suffix.
        """
        try:
            #_LOG.debug(f"GetCurrentValue: EntityCombo[{entity_combo}]")
            datatype, entity_id, attr, watch = parse_entity_combo(entity_combo)
            #_LOG.debug(f"...EntityId[{entity_id}]")

            entity_state = self.hass.states.get(entity_id)
            #_LOG.debug(f"...EntityState:[{entity_state}]")
            value = entity_state.attributes.get(attr) if (attr) else entity_state.state

            if datatype == "float":
                if value not in (None, "unavailable", "unknown"):
                    value = float(value)
                else:
                    value = -1  # Unavailable or Unknown

            #_LOG.debug(f"...Value[{value}]")

            return value
        except Exception as e:
            _LOG.error(f"Failed to _get_current_value({entity_combo}): {e}")

    async def _execute_with_test_data(self, context: Context, cause: str = "Unknown"):
        """
        Execute the State Machine with Current ScreenLogic data
        """
        try:
            body_type = context.body_type
            _LOG.info(f"_execute_with_test_data: Body[{body_type}] Cause[{cause}]")
            config = self._config.get(body_type)

            _LOG.debug(f"...Context: {context}")
            _LOG.debug(f"...Config : {config}")

            context.timestamp       = time.time()
            context.air_temp        = 78 
            context.water_temp      = 80 
            context.target_temp     = 85 
            context.climate_mode    = CLIMATE_MODE_HEAT
            context.climate_status  = CLIMATE_STATUS_HEATING
            context.circuit         = "on" # self._get_current_value(config[CIRCUIT])
            context.sm_state        = cause

            #
            # Execute State Machine - Bricks save STATUS & ESP in the Context
            #
            await esp_state_machine(context, cause)

            esp = context.esp
            status = esp.display_label
            _LOG.debug(f"...[{body_type}] Cause[{cause}] Status[{status}] ESP[{esp.seconds}] Confidence[{esp.confidence_label}]")

        except Exception as e:
            _LOG.error(f"_execute_with_test_data: Error [{e}] Context[{context}]  ")
            raise ESPException("ERROR", f"_execute_with_test_data: {e}") from e



    async def _execute_with_current_data(self, context: Context, cause: str = "Unknown"):
        """
        Execute the State Machine with Current ScreenLogic data
        """
        try:
            #_LOG.info(f"execute_with_current_data: Cause[{cause}]")

            body_type = context.body_type
            #_LOG.debug(f"...BodyType[{body_type}]")
            config = context.config # self._config.get(body_type)

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

        body_type = self._get_body_type_by_watch_entity(entity_id)
        if not body_type:
            _LOG.warning(f"ESPCoordinator._handle_state_change: Failed to find BodyType for Entity [{entity_id}]")
            return

        # Filter — only process entities we care about
        if not self._config or not self._watch_entities or not self.contexts:
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
                ###await self._execute_with_test_data(context, changes")
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
    def config(self) -> dict:
        """
        Get the Coordinator Configuration data
        """
        return self._config

    @config.setter
    def config(self, value):
        """
        Set the Coordinator Configuration data
        """
        self._config = value

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
