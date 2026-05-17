from homeassistant.const import Platform

DOMAIN = "screenlogic_esp"
SCREENLOGIC_DOMAIN = "screenlogic"

# List of Entity Platforms which the ESP supports
PLATFORMS: list[Platform] = [
    Platform.SENSOR
]

ADDONS_COORDINATOR = "hassio_addons_coordinator"

# Heater/circuit on-state values
HEATER_MODE_HEAT_VALUE      = "heat"
HEATER_MODE_HEAT_OFF_VALUE  = "off"
HEATER_STATUS_HEATING_VALUE = "heating"
HEATER_STATUS_IDLE_VALUE    = "idle"
HEATER_STATUS_OFF_VALUE     = "off"
CIRCUIT_ON_VALUE     = "on"

# ESP calculation parameters
AIR_TEMP_BIN_WIDTH   = 5       # degrees F per bin
MIN_SAMPLES          = 3       # minimum samples per bin before trusting estimate
HISTORY_DAYS         = 31      # days of recorder history to analyse
MIN_INTERVAL_MINUTES = 2       # ignore heater-on intervals shorter than this
MIN_DEGREES_GAINED   = 0.25    # ignore intervals with negligible temp rise
SINGLE_SAMPLE_SCORE  = 0.25    # score for a single sample
MAX_SAMPLES_PER_BIN  = 20
MAX_RATE_AGE_DAYS    = 365     # keep a full year of learning
STORAGE_VERSION      = 1

# Sensor settle time when entering Sensing state (mirrors SensingBrick timer)
TIMER_SENSING  = 120
TIMER_FIVE_MINUTES = 5 * 60     # Five Minutes in Seconds

### ScreenLogic Climate Entity Attributes
ATTR_STATE                  = "state"
ATTR_CURRENT_TEMP           = "current_temperature" # Water Temp
ATTR_TEMP                   = "temperature"         # Target Temp
ATTR_HVAC_ACTION            = "hvac_action"         # idle, heating
ATTR_HVAC_MODE              = "hvac_mode"           # off, heat

WATER_TEMP                  = "WATER_TEMP"
CLIMATE_MODE                = "CLIMATE_MODE"
CLIMATE_STATUS              = "CLIMATE_STATUS"
TARGET_TEMP                 = "TARGET_TEMP"
AIR_TEMP                    = "AIR_TEMP"
CIRCUIT                     = "CIRCUIT"

CONTEXT_BODY_TYPE           = "body_type"       # Pool or Spa
CONTEXT_CONFIG              = "config"          # Body Config
CONTEXT_MACHINE_STATE       = "machine_state"   # State Machine state
CONTEXT_ESP                 = "esp"             # ESP Object
CONTEXT_STATUS              = "status"          # State Machine Status
CONTEXT_TIMESTAMP           = "timestamp"       # Current data Timestamp
CONTEXT_WATER_TEMP          = "water_temp"      # Body Water Temp
CONTEXT_TARGET_TEMP         = "target_temp"     # Body Target Water Temp
CONTEXT_AIR_TEMP            = "air_temp"        # Air Temperature
CONTEXT_CHANGES             = "changes"         # Latest Changes
CONTEXT_CLIMATE_STATUS      = "climate_status"  # Heating Status (idle, heating)
CONTEXT_CLIMATE_MODE        = "climate_mode"    # Heating Mode (off, heat)
CONTEXT_CIRCUIT             = "circuit"         # Circuit (on, off)
CONTEXT_TIMER               = "timer"           # Timer (Sensing, Countdown)
CONTEXT_COORDINATOR         = "coordinator"     # Coordinator
CONTEXT_HISTORY_ADAPTER     = "history_adapter" # History Adapter
CONTEXT_HASS                = "hass"            # Home Assistant instance
CONTEXT_TESTING             = "testing"         # Testing mode
CONTEXT_EXPORT              = "export"          # Export data

CONF_SHOW_PANEL = "show_sidebar_panel"

POOL_ADAPTER_CONFIG = "adapter"
DEFAULT_POOL_ADAPTER = "screenlogic_adapter"
POOL_PREFIX   = "pool_prefix"
POOL_MODEL    = "pool_model"
POOL_NAME     = "pool_name"
POOL_ID       = "pool_id"

BODY_TYPE_POOL = "pool"
BODY_TYPE_SPA = "spa"
BODY_TYPES = [ BODY_TYPE_POOL, BODY_TYPE_SPA ]

###
### <datatype>:<sensor>[/attribute]:watch
###
BODY_CONFIG_TEMPLATES = {
    WATER_TEMP      : "float:climate.{prefix}_{body_type}_heat/current_temperature:WATCH",
    CLIMATE_MODE    : "str:climate.{prefix}_{body_type}_heat:WATCH",
    CLIMATE_STATUS  : "str:climate.{prefix}_{body_type}_heat/hvac_action:WATCH",
    TARGET_TEMP     : "float:climate.{prefix}_{body_type}_heat/temperature:WATCH",
    AIR_TEMP        : "float:sensor.{prefix}_air_temperature:IGNORE",
    CIRCUIT         : "str:switch.{prefix}_{body_type}:WATCH"
}

###
### Special case State Machine values used in the Transition Table
###
SM_START = "$START"     # Special case for First Brick to execute
SM_EXIT  = "$EXIT"      # Special case for Exiting the State Machine
SM_BRICK = "$BRICK"     # Special case for defining Brick Function
SM_NAME  = "$NAME"      # The Name of the State

###
### State Machine States
###
STATE_OFF         = "off"
STATE_ENABLED     = "enabled"
STATE_SENSING     = "sensing"
STATE_STANDBY     = "standby"
STATE_HEATING     = "heating"
STATE_READY       = "ready"
STATE_MAINTAINING = "maintaining"
STATE_DISABLED    = "disabled"

###
### Results returned by Bricks
###
RESULT_OFF          = "OFF"
RESULT_ACTIVE       = "ACTIVE"
RESULT_STANDBY      = "STANDBY"
RESULT_TARGETCHANGE = "TARGETCHANGE"
RESULT_WILDCARD     = "*"

###
### Status values displayed in Home Assistant
###
STATUS_INITIALIZING     = "Initializing"
STATUS_ENABLED          = "Enabled"         # Circuit/Heat is On
STATUS_SENSING          = "Sensing"         # Waiting for Temperature Sensor to settle
STATUS_LEARNING         = "Learning"        # Insufficent Heat/Air/Water data
STATUS_HEATING          = "Heating"         # Circuit/Heat is On and Heating
STATUS_READY            = "Ready"           # Reached Set Point; only occurs once
STATUS_DISABLED         = "Disabled"        # Heater Disabled

STATUS_ERROR            = "Error"           # An error has occurred 

ICON_OFF = "🚫"
ICON_HEATING = "🔥"
ICON_BELL = "🛎️"
ICON_SLEEPING = "😴"
ICON_LEARNING = "🎓"

### 
### ScreenLogic entity values
###
CIRCUIT_ON                   = "on"
CLIMATE_MODE_HEAT            = "heat"
CLIMATE_MODE_SOLAR           = "solar"
CLIMATE_MODE_SOLAR_PREFERRED = "solar_preferred"
CLIMATE_STATUS_HEATING       = "heating"
CLIMATE_STATUS_IDLE          = "idle"
