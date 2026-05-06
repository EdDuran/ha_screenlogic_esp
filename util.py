import logging
from string import Template
from .const import *
from .timer import Timer

_LOGGER = logging.getLogger(__name__)

_DEBUG = True
_TRACE = True
_WARNING = True

def _warning(msg: str):
    if (_WARNING):
        _LOGGER.warning(msg)

def _debug(msg: str):
    if (_DEBUG):
        _LOGGER.debug(msg)

def _trace(msg: str):
    if (_TRACE):
        _LOGGER.info(msg)

def parse_entity_combo(entity_combo: str):
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
        if entity_combo is not None:
            parts = entity_combo.split(":")
            datatype = parts[DATATYPE]
            entity_parts = parts[ENTITY].split("/")
            entity = entity_parts[ENTITY_ID]
            attr: str | None = entity_parts[ENTITY_ATTR] if (len(entity_parts) == 2) else None
            watch = True if parts[WATCH] == "WATCH" else False
    except Exception as e:
        _LOGGER.error(f"parse_entity_combo: Failed to parse [{entity_combo}]; {e}")
        raise ESPException("ERROR", f"parse_entity_combo: Failed to parse [{entity_combo}]") from e

    return datatype, entity, attr, watch

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
### ----- Class ESP ------------------------------------------------------------
###

class ESP:
    """
    The Results of calculating the ESP
    """

    _DHM = Template("$d-$h:$m")
    _HM  = Template("$h:$m")

    CONFIDENCE_NA       = "n/a"
    CONFIDENCE_LOW      = "low"
    CONFIDENCE_MEDIUM   = "medium"
    CONFIDENCE_HIGH     = "high"

    def __init__(self, seconds:int, confidence: float, status:str = None):
        self._seconds = seconds
        self._confidence = confidence
        self._status = status
        self._days = 0
        self._hours = 0
        self._minutes = 0
        self._display_label = status
        self._confidence_label = self.CONFIDENCE_NA
        self._format_esp()
    
    def __str__(self) -> str:
        return f"ESP: Seconds[{self.seconds}] {self.display_label}, Status[{self.status}]"

    def _format_esp(self) -> str:
        """
        Get the ESP (in seconds) as the number of
        Days, Hours, Minutes and Formatted ESP: [DDD-]HH:MM
        """
        if self._display_label is None: # Formatted strings already?
            total_minutes = int(round(self._seconds / 60))    
            self._days, remaining_minutes = divmod(total_minutes, 1440)  # 1440 min/day
            self._hours, self._minutes    = divmod(remaining_minutes, 60)
            self._display_label = self._DHM.substitute(d=self._days, h=f"{self._hours:02d}", m=f"{self._minutes:02d}") if self._days > 0 else self._HM.substitute(h=self._hours, m=f"{self._minutes:02d}")

            if self._confidence >= 0.7:
                self._confidence_label = self.CONFIDENCE_HIGH
            elif self._confidence >= 0.4:
                self._confidence_label = self.CONFIDENCE_MEDIUM
            elif self._confidence > 0:
                self._confidence_label = self.CONFIDENCE_LOW
            else:
                self._confidence_label = self.CONFIDENCE_NA

    @property
    def is_confidence(self, value: str):
        return self._confidence_label == value

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
    def confidence(self) -> int:
        return self._confidence

    @property
    def confidence_label(self) -> str:
        return self._confidence_label
    
    @property
    def display_label(self) -> str:
        return self._display_label
    
    @property
    def status(self) -> str:
        return self._status



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
    def __init__(self, body_type):
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
        """Is the Target Temperature changed value? """
        _trace(f"_is_target_change[{self.changes}]")
        if self.changes:
            for change in self.changes:
                _debug(f"...Change:[{change}]")
                body_type, attr, value = parse_entity_change(change)
                _debug(f"...{body_type} : {attr} : {value}")
                return body_type == self.body_type and attr == ATTR_TEMP
        else:
            return False

    def get_esp_result(self) -> str:
        """
        Get the State Machine signal from current sensor values.
            OFF     Circuit Off or Heater Disabled
            ACTIVE  Circuit On and Heater On
            STANDBY Circuit On and Heater Enabled
        """
        result = RESULT_STANDBY

        if not self.is_circuit_on() or not self.is_heat_enabled():
            result = RESULT_OFF
        if self.is_heating():
            result = RESULT_ACTIVE

        return result
# end class Context

def get_formatted_esp(seconds):
    """
    Get the ESP (from seconds) as the number of
    Days, Hours, Minutes and Formatted ESP
    """
    total_minutes = int(round(seconds / 60))    
    days,    remaining_minutes = divmod(total_minutes, 1440)  # 1440 min/day
    hours,   mins              = divmod(remaining_minutes, 60)

    if days > 0:
        return days, hours, mins, f"{days}-{hours:02d}:{mins:02d}"
    else:
        return days, hours, mins, f"{hours}:{mins:02d}"



