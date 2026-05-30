from datetime import datetime, timezone
import importlib
import logging
from string import Template
from abc import ABC, abstractmethod
from zoneinfo import ZoneInfo
from homeassistant.core import HomeAssistant

from .const import RESULT_ACTIVE, RESULT_OFF, RESULT_STANDBY, SM_START
from .const import *
from .timer import Timer

_LOG = logging.getLogger(__name__)


def local_time(ts:float, hass:HomeAssistant=None) -> str:
    """
    Convert a UTC timestamp to local time and return the local date and time as strings.
    """
    if hass:
        tz = ZoneInfo(hass.config.time_zone)
        return datetime.fromtimestamp(ts, tz=tz).strftime('%Y-%m-%d %H:%M:%S %Z')
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')

def parse_entity_change(changes: set) -> tuple[str, str, str]:
    """
    Parse Entity Change and return components:
    BodyType, Attribute and Value
    """
    BODY_TYPE = 0
    ATTR = 1
    VALUE = 2

    if changes:
        parts = changes.split(":")
        if len(parts) == 3:
            return parts[BODY_TYPE], parts[ATTR], parts[VALUE]

    return None, None, None



###
### ----- Class EntityCombo ---------------------------------------------------
###

class EntityCombo:
    def __init__(self, entity_combo:str):
        self._entity_combo = entity_combo
        self._parse()
    
    def __str__(self):
        return self._entity_combo

    @property
    def datatype(self) -> str:
        return self._datatype

    @property
    def id(self) -> str:
        return self._id

    @property
    def attribute(self) -> str | None:
        return self._attribute

    @property
    def watch(self):
        return self._watch

    def _parse(self):
        """
        Parse out the Entity Datatype, Id and (optional) Attribute from the EntityCombo:
            "<datatype>:<id>[/<attr>]:<watch>"
                where "watch" = { WATCH | IGNORE }
            Ex: "float:climate.{prefix}_{body_type}_heat/current_temperature:WATCH"
        Return: datatype, id, attribute, watch
        """
        DATATYPE = 0
        ENTITY = 1
        WATCH = 2
        ENTITY_ID = 0
        ENTITY_ATTR = 1

        try:
            parts = self._entity_combo.split(":")
            self._datatype = parts[DATATYPE]
            entity_parts = parts[ENTITY].split("/")
            self._id = entity_parts[ENTITY_ID]
            self._attribute = entity_parts[ENTITY_ATTR] if (len(entity_parts) == 2) else None
            self._watch = True if parts[WATCH] == "WATCH" else False
        except Exception as e:
            _LOG.error(f"parse_entity_combo: Failed to parse [{self._entity_combo}]; {e}")
            raise ESPException("ERROR", f"parse_entity_combo: Failed to parse [{self._entity_combo}]") from e

    def parse_entity_change(changes: set):
        """
        Parse Entity Change and return components:
        BodyType, Attribute and Value
        """
        BODY_TYPE = 0
        ATTR = 1
        VALUE = 2

        if changes:
            parts = changes.split(":")
            if len(parts) == 3:
                return parts[BODY_TYPE], parts[ATTR], parts[VALUE]

        return None, None, None


###
### ----- Class PoolAdapter ---------------------------------------------------
###
class PoolAdapter(ABC):
    import importlib

    def __init__(self, name:str, debug_mode=False):
        self._name = name
        self._debug_mode = debug_mode

    @property
    def name(self):
        return self._name

    @property
    def watch_entities(self) -> dict:
        raise NotImplementedError("Subclasses must implement this method.")
    
    @property
    def all_watch_entities(self) -> set[str]:
        raise NotImplementedError("Subclasses must implement this method.")

    @property
    def config(self):
        raise NotImplementedError("Subclasses must implement this method.")
    
    @property
    def debug_mode(self):
        return self._debug_mode

    @abstractmethod
    def discover():
        """ Discover the pool devices """
        raise NotImplementedError("Subclasses must implement this method.")

    @abstractmethod
    def getBodyTypes() -> list[str]:
        """ Get the list of body types """
        raise NotImplementedError("Subclasses must implement this method.")
    
    ###
    ### Returns a list of EntityCombo supported by this Body Type
    @abstractmethod
    def getBodyConfig(self, body_type: str) -> dict[str,EntityCombo]:
        """ Get the pool configuration """
        raise NotImplementedError("Subclasses must implement this method.")
    
    @classmethod
    async def create(cls, hass:homeassistant, name: str, *args, **kwargs) -> PoolAdapter:
        """
        Create an adapter instance. The name should be defined in the configuration.yaml:
            screenlogic_esp:
                adapter: screenlogic_adapter
        """
        try:
            # 1. Dynamically import the file (e.g., 'adapters.json_adapter')
            # If util.py is in the root, use absolute path format
            module_path = f".{name}"

            # Run blocking import off the event loop
            module = await hass.async_add_executor_job(
                importlib.import_module,
                module_path,
                __package__,
            )

             # 2. Convert string name to class format (e.g., "json_adapter" -> "JsonAdapter")
            # This logic maps your file name to your exact class name inside it
            class_name = "".join([part.capitalize() for part in name.split("_")])

            # 3. Extract the class object from the imported module
            sub_cls = getattr(module, class_name)
         
            return sub_cls(hass, *args, **kwargs)

        except ModuleNotFoundError:
            raise
        
        except Exception as e:
            raise ESPException(f"Failed to create Pool Adapter[{name}]") from e
    

###
### ----- Class HistoryAdapter -------------------------------------------------
###
class HistoryAdapter(ABC):
    """
    A base class to adapt and manage historical data.
    """

    def get_history(self):
        """
        Get the historical data.
        """
        raise NotImplementedError("Subclasses must implement this method.")
    
    def get_current_value(self, screenlogic_entity:str):
        """
        Get the current value of a specific attribute from the coordinator.
        """
        raise NotImplementedError("Subclasses must implement this method.")

    @property
    def now(self) -> float:
        """
        Get the current timestamp.
        """
        raise NotImplementedError("Subclasses must implement this method.")

    @property
    def starttime(self) -> str:
        """
        Get the history starttime.
        """
        raise NotImplementedError("Subclasses must implement this method.")
    
    @property
    def endtime(self) -> str:
        """
        Get the history endtime.
        """
        raise NotImplementedError("Subclasses must implement this method.")
    
    @property
    def body_type(self) -> str:
        """
        Get the body type.
        """
        raise NotImplementedError("Subclasses must implement this method.")

###
### ----- Class ESP ------------------------------------------------------------
###

class ESP:
    """
    The Results of calculating the ESP
    """
    def __init__(self, seconds:int, confidence: float, status:str = None):
        self._seconds = seconds
        self._confidence = confidence
        self._status = status
        self._days = 0
        self._hours = 0
        self._minutes = 0
        self._display_label = status

    def __eq__(self, other, tolerance_seconds=300):  # 5 minute tolerance
        if not isinstance(other, ESP):
            return False
        return (
            abs(self._seconds - other._seconds) <= tolerance_seconds and
            abs(self._confidence - other.confidence) <= 0.05
        )
    
    def __str__(self) -> str:
        return f"ESP: Seconds[{self.seconds}] {self.display_label} Confidence[{self.confidence}%]"

    @staticmethod
    def format_dhm(seconds) -> str:
        """
        Get the ESP (in seconds) as the number of
        Days, Hours, Minutes and Formatted ESP: [DDD-]HH:MM
        """
        _DHM = Template("$d-$h:$m")
        _HM  = Template("$h:$m")
        total_minutes = int(round(seconds / 60))  
        days, remaining_minutes = divmod(total_minutes, 1440)  # 1440 min/day
        hours, minutes    = divmod(remaining_minutes, 60)
        return _DHM.substitute(d=days, h=f"{hours:02d}", m=f"{minutes:02d}") if days > 0 else _HM.substitute(h=hours, m=f"{minutes:02d}")

    @staticmethod
    def format_ms(seconds) -> str:
        """
        Get the ESP (in seconds) as the number of
        Minutes and Seconds as Formatted ESP: MM:SS
        """
        minutes, remaining_seconds = divmod(seconds, 60)  # 1440 min/day
        _MS  = Template("$m:$s")
        return _MS.substitute(m=minutes, s=f"{remaining_seconds:02d}")

    @property
    def seconds(self) -> int:
        return self._seconds

    @property
    def days(self) -> int:
        return self._days

    @property
    def hours(self) -> int:
        return self._hours

    @property
    def minutes(self) -> int:
        return self._minutes

    @property
    def confidence_pct(self) -> float:
        return self._confidence * 100
    
    @property
    def confidence(self) -> float:
        return self._confidence

    @property
    def display_label(self) -> str:
        return ESP.format_dhm(self._seconds)
    
    @property
    def status(self) -> str:
        return self._status
    
    @property
    def rate(self) -> float:
        return self._rate

    @rate.setter
    def rate(self, value):
        self._rate = value

    @property
    def degrees_remaining(self) -> float:
        return self._degrees_remaining
    
    @degrees_remaining.setter
    def degrees_remaining(self, value):
        self._degrees_remaining = value 


###
### ----- Class ESPException ---------------------------------------------------
###
class ESPException(Exception):
    """
    Exception raised by ESP services.
    status  — user-facing display string for the HA helper entity
    """
    def __init__(self, status: str, detail:str = None):
        Exception.__init__(self, detail or status)   # explicit parent call
        self.status = status
        self.detail = detail

###
### ----- Class Config ---------------------------------------------------------
###

class Config():
    """
    The ESP Configuration, containing Pentair Screenlogic information
    """
    def __init__(self, body_type) -> None:
        self._config = {}

    def __str__(self) -> str:
        return f"{self._config}"

###
### ----- Class Context --------------------------------------------------------
###

class Context():
    """
    The Context of each ESP Sensor (ie, body types: pool and spa)
    """
    def __init__(self, body_type="Unknown"):
        self._context = {}
        self.body_type = body_type
    
    def __str__(self) -> str:
        return f"{self._context}"

    def __repr__(self) -> str:
        return (f"Context[{self.body_type}] "
                f"status={self.status} "
                f"machine_state={self.machine_state} "
                f"water_temp={self.water_temp} "
                f"target_temp={self.target_temp} "
                f"air_temp={self.air_temp} "
                f"climate_mode={self.climate_mode} "
                f"climate_status={self.climate_status} "
                f"circuit={self.circuit}")
    @property
    def body_type(self) -> str:
        return self._context.get(CONTEXT_BODY_TYPE, None)

    @body_type.setter
    def body_type(self, value):
        self._context[CONTEXT_BODY_TYPE] = value


    @property
    def machine_state(self) -> str:
        """
        Get the State Machine 'State"
        Returns "SM_START" if the Machine State isn't defined
        """
        return self._context.get(CONTEXT_MACHINE_STATE, SM_START)

    @machine_state.setter
    def machine_state(self, value):
        self._context[CONTEXT_MACHINE_STATE] = value


    @property
    def status(self) -> str:
        return self._context.get(CONTEXT_STATUS, None)

    @status.setter
    def status(self, value):
        self._context[CONTEXT_STATUS] = value


    @property
    def seconds(self) -> int:
        return self._context.get(CONTEXT_SECONDS, None)

    @seconds.setter
    def seconds(self, value):
        self._context[CONTEXT_SECONDS] = value


    @property
    def confidence_pct(self) -> float:
        return self._context.get(CONTEXT_CONFIDENCE_PCT, None)

    @confidence_pct.setter
    def confidence_pct(self, value):
        self._context[CONTEXT_CONFIDENCE_PCT] = value


    @property
    def esp(self) -> ESP:
        return self._context.get(CONTEXT_ESP, None)

    @esp.setter
    def esp(self, value:ESP):
        self._context[CONTEXT_ESP] = value


    @property
    def water_temp(self) -> float:
        return self._context.get(CONTEXT_WATER_TEMP, None)

    @water_temp.setter
    def water_temp(self, value):
        self._context[CONTEXT_WATER_TEMP] = value


    @property
    def target_temp(self) -> float:
        return self._context.get(CONTEXT_TARGET_TEMP, None)

    @target_temp.setter
    def target_temp(self, value):
        self._context[CONTEXT_TARGET_TEMP] = value


    @property
    def air_temp(self) -> float:
        return self._context.get(CONTEXT_AIR_TEMP, None)

    @air_temp.setter
    def air_temp(self, value):
        self._context[CONTEXT_AIR_TEMP] = value


    @property
    def changes(self) -> set:
        return self._context.get(CONTEXT_CHANGES, None)

    @changes.setter
    def changes(self, value):
        self._context[CONTEXT_CHANGES] = value


    @property
    def timestamp(self) -> any:
        return self._context.get(CONTEXT_TIMESTAMP, None)

    @timestamp.setter
    def timestamp(self, value):
        self._context[CONTEXT_TIMESTAMP] = value


    @property
    def circuit(self) -> str:
        return self._context.get(CONTEXT_CIRCUIT, None)

    @circuit.setter
    def circuit(self, value):
        self._context[CONTEXT_CIRCUIT] = value


    @property
    def climate_mode(self) -> str:
        return self._context.get(CONTEXT_CLIMATE_MODE)

    @climate_mode.setter
    def climate_mode(self, value: str):
        self._context[CONTEXT_CLIMATE_MODE] = value


    @property
    def climate_status(self) -> str:
        return self._context.get(CONTEXT_CLIMATE_STATUS)

    @climate_status.setter
    def climate_status(self, value: str):
        self._context[CONTEXT_CLIMATE_STATUS] = value


    @property
    def timer(self) -> Timer:
        return self._context.get(CONTEXT_TIMER)

    @timer.setter
    def timer(self, value: Timer):
        self._context[CONTEXT_TIMER] = value


    @property
    def config(self) -> dict:
        return self._context.get(CONTEXT_CONFIG)

    @config.setter
    def config(self, value: any):
        self._context[CONTEXT_CONFIG] = value


    @property
    def coordinator(self) -> dict:
        return self._context.get(CONTEXT_COORDINATOR)

    @coordinator.setter
    def coordinator(self, value: any):
        self._context[CONTEXT_COORDINATOR] = value


    @property
    def history_adapter(self):
        return self._context.get(CONTEXT_HISTORY_ADAPTER)

    @history_adapter.setter
    def history_adapter(self, value: any):
        self._context[CONTEXT_HISTORY_ADAPTER] = value


    @property
    def hass(self) -> homeassistant:
        return self._context.get(CONTEXT_HASS)

    @hass.setter
    def hass(self, value:homeassistant):
        self._context[CONTEXT_HASS] = value


    @property
    def testing(self) -> bool:
        return self._context.get(CONTEXT_TESTING, False)

    @testing.setter
    def testing(self, value:bool):
        self._context[CONTEXT_TESTING] = value


    @property
    def export(self) -> bool:
        return self._context.get(CONTEXT_EXPORT, False)

    @export.setter
    def export(self, value:bool):
        self._context[CONTEXT_EXPORT] = value


    ###
    ### ----- Generic Get and Set functions ------------------------------------
    ###

    def set(self, key: str, value: any):
        self._context[key] = value

    def get(self, key: str) -> any:
        return self._context.get(key, None)


    ###
    ### ----- Convience Functions ----------------------------------------------
    ###

    def is_last_degree(self):
        """
        If Water Temp is at Target and Heater still on, then this is the "Last Degree"
        """
        return (self.water_temp >= self.target_temp) and self.is_heating

    def is_circuit_on(self) -> bool:
        """
        Is the Circuit On?
        """
        return self.circuit == CIRCUIT_ON

    def is_heat_enabled(self) -> bool:
        """
        Is the Heater Enabled?
        """
        return self.climate_mode in [
            CLIMATE_MODE_HEAT,
            CLIMATE_MODE_SOLAR,
            CLIMATE_MODE_SOLAR_PREFERRED]

    def is_heating(self) -> bool:
        """
        Is the Heater On?
        """
        return self.climate_status == CLIMATE_STATUS_HEATING

    def is_at_setpoint(self) -> bool:
        """
        Is the Water at the selected Target Temperature?
        """
        if self.water_temp is None or self.target_temp is None:
            return False
        return self.water_temp >= self.target_temp
    
    def is_target_change(self) -> bool:
        """Is the Body Target Temperature changed value? """
        target_change:bool = False

        if self.changes:
            for change in self.changes:
                body_type, attr, value = parse_entity_change(change)
                _LOG.debug(f"...{body_type} : {attr} : {value}")
                target_change = body_type == self.body_type and attr == ATTR_TEMP

        _LOG.debug(f"_is_target_change({self.changes})? --> [{target_change}]")

        return target_change
    
    def get_esp_result(self) -> str:
        """
        Get the State Machine signal from current sensor values.
            OFF     Circuit Off or Heater Disabled
            ACTIVE  Circuit On and Heater On
            STANDBY Circuit On and Heater Enabled
        """
        result = RESULT_OFF
        if (self.is_circuit_on() and self.is_heat_enabled()):
            result = RESULT_ACTIVE if self.is_heating() else RESULT_STANDBY

        return result
    
# end class Context



