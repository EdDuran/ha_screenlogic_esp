import logging
import time
import traceback
from weakref import WeakSet

from custom_components.pool_esp.persistence import Persistence

from homeassistant.config_entries import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STARTED, ConfigEntry
from homeassistant.helpers.event import async_track_state_change_event

from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    HomeAssistant,
    Event
)
from reactivex import start
from .const import *
from .util import *
from .state_machine import StateMachine
from .heater_watchdog import HeaterWatchdog

_LOG = logging.getLogger(__name__)

###
### ----- Class ESPCoordinator -------------------------------------------------
###

class ESPCoordinator(DataUpdateCoordinator):
# your state machine and ETA logic lives here:

    def __init__(self, hass:HomeAssistant, config_entry:ConfigEntry, pool_adapter:PoolAdapter) -> None:
        
        super().__init__(hass, _LOG, name=DOMAIN)

        _LOG.debug(f"ESPCoordinator.__init__")
        _LOG.debug(f"...ConfigEntry: {type(config_entry)}:{config_entry}")
        _LOG.debug(f"...PoolAdapter: {type(pool_adapter)} {pool_adapter}")

        self._config_entry = config_entry   
        self._pool_adapter = pool_adapter
        self._config = pool_adapter.config                      # PoolAdapter Config
        self._watch_entities = pool_adapter.watch_entities      # Pool Entities we're watching
        self._contexts = {}                                     # {body_type: Context}
        self._unsub = []                                        # state change listeners
        self._sensors:dict[str, set] = {}                       # {body_type: set of HA Sensor Entities to update when ESP changes}
        self._persistence:dict[str, Persistence] = {}           # {body_type: Persistence} Persistence for each body type

    @property
    def pool_adapter(self):
        return self._pool_adapter
    
    def get_watchdog(self, body_type) -> HeaterWatchdog:
        return self._watchdogs.get(body_type, None)
    
    def add_sensor(self, body_type, sensor):
        """
        Add a Sensor Entity to the Coordinator's set of sensors to update when ESP changes
        """
        self._sensors.setdefault(body_type, set()).add(sensor)
    
    def update_sensor(self, body_type):
        """
        Tell the Sensor Entity for the specified body type to update its state in Home Assistant
        """
        for sensor in self._sensors.get(body_type, set()):
            sensor.async_write_ha_state()
    
    def get_sensor(self, body_type:str, sensor_type):
        """
        Get the specified sensor for the specified body type
        """
        for sensor in self._sensors.get(body_type, set()):
            if isinstance(sensor, sensor_type) and sensor._body_type == body_type:
                return sensor
        
        raise ESPException("ERROR", f"Sensor[{body_type}][{sensor_type}] was not found in Coordinator sensors: {self._sensors.get(body_type, set())}")
    
    def get_sensors(self):
        """
        Get ALL sensors for ALL body types
        """
        sensors = set()
        for body_type, sensor_set in self._sensors.items():
            sensors.update(sensor_set)
        
        return sensors
        
    ###
    ### ----- HA Required Functions --------------------------------------------
    ###
    async def async_setup(self) -> None:
        """Called once after integration loads."""

        from homeassistant.helpers.start import async_at_started

        _LOG.debug(f"ESPCoordinator.async_setup")
        _LOG.debug(f"...PoolAdapter: {self._pool_adapter}")

        # Initialize Contexts and Config for Body Types
        for body_type in BODY_TYPES:
            config = self.get_config(body_type)
            _LOG.debug(f"...Config [{body_type}] : {config}")

            context:Context = Context(body_type)
            context.config = config     # Context has a reference to Config
            context.coordinator = self  # Context has a reference to this Coordinator
            context.hass = self.hass    # Context has a reference to the Home Assistant instance
            self._contexts[body_type] = context

            p = Persistence(self.hass, body_type, self._pool_adapter.name)
            await p.async_load()
            self._persistence[body_type] = p
            
        _LOG.debug(f"...WatchEntities: {self._watch_entities}")

        ###
        ### One watchdog per body type
        ### Watchdog monitors heating progress and flags potential heater issues
        ###
        self._watchdogs = {
            body_type: HeaterWatchdog(self, body_type)
            for body_type in BODY_TYPES
        }

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

        # Defer first calculation until ALL platforms are initialized
        async_at_started(self.hass, self._first_estimator_calculations)
        _LOG.debug("ESPCoordinator: deferred first calculation until platforms ready")
    
    # end async_setup

    async def _first_estimator_calculations(self, hass) -> None:
        """Called when HA has fully started — all platforms initialized."""
        _LOG.debug("ESPCoordinator: Platform is ready, running first calculation")
        for body_type in BODY_TYPES:
            context = self.get_context(body_type)
            await self._execute_with_current_data(context, "Initalization")

    def get_persistence(self, body_type: str) -> Persistence:
        """Return the persistence instance for a body type."""
        return self._persistence[body_type]

    def get_option(self, key: str, default=None):
        """Get a value from the integration options."""
        return self._config_entry.options.get(key, default)
    
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
        from .bricks import STATE_TRANSITIONS

        try:
            body_type = context.body_type
            config = context.config

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
            await StateMachine(STATE_TRANSITIONS,context, cause).execute()

            esp = context.esp
            if esp is not None:
                context.status = esp.status
                context.confidence_pct = esp.confidence_pct
                context.seconds = esp.seconds
                _LOG.debug(f"...Status[{context.status}] seconds[{context.seconds}] Confidence[{context.confidence_pct}%]")
                self.update_sensor(body_type)
            else:
                _LOG.error(f"...No ESP data available")
                
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
        entity_id = event.data.get("entity_id")

        body_type = self._get_body_type_by_entity(entity_id)
        if not body_type:
            _LOG.warning(f"_handle_state_change: Failed to find BodyType for Entity [{entity_id}]")
            return

        # Filter — only process entities we care about
        if not self._pool_adapter or not self._watch_entities or not self.contexts:
            return   # setup hasn't completed yet

        watch_entities = self._watch_entities.get(body_type)
        
        if entity_id in watch_entities:
            try:
                old_state = event.data.get("old_state")
                new_state = event.data.get("new_state")

                context:Context = self.get_context(body_type)
                context.changes = self._what_changed(body_type, entity_id, old_state, new_state)

                _LOG.debug(f"_handle_state_change: [{body_type}] [{entity_id}->{context.changes}]")

                await self._execute_with_current_data(context, context.changes)
            except Exception as e:
                details = (f"ESPCoordinator._handle_state_change: Failed [{body_type}] Entity[{entity_id}]; {e}")
                _LOG.error(details)
                _LOG.error(traceback.format_exc())
                raise ESPException("ERROR", details) from e
        # end if

    async def _async_update_data(self):
        _LOG.debug(f"_async_update_data")
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
