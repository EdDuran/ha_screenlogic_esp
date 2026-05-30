#
# ----- Brick Initialize
#
# Initialize Context
#
# Return: RESULT_OFF
#
import logging

from .const import TIMER_SENSING, STATUS_INITIALIZING, STATUS_ENABLED, STATUS_READY, STATUS_DISABLED, RESULT_ACTIVE, RESULT_OFF, RESULT_STANDBY, RESULT_TARGETCHANGE
from .estimator import ESPEstimator
from .heater_watchdog import HeaterWatchdog
from .state_machine import SM_NAME, SM_RESULT_WILDCARD, SM_START, SM_BRICK, SM_EXIT
from .timer import Timer, TimerCallback
from .util import ESP, Context, ESPException

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




_LOG = logging.getLogger(__name__)

async def _brick_initialize(context: Context) -> str:
    context.esp = ESP(0, 0, STATUS_INITIALIZING)
    return context.get_esp_result()

#
# ----- Brick Off
#
# Body Circuit and Heater are Off
#   Calculate ETA just in case anyone wants to
#
# Return: ESP Result
#
async def _brick_off(context:Context) -> str:
    from .estimator import ESPEstimator, ESP

    result = context.get_esp_result()

    try:
        if result == RESULT_OFF:
            if context.is_at_setpoint():
                context.esp = ESP(0,0, STATUS_READY)
            else:
                estimator = ESPEstimator(context.coordinator, context.body_type)
                context.esp = await estimator.calculate_wrapper(context)
    except Exception as e:
        _LOG.error(f"_brick_off: Failed to execute: {e}")
        raise ESPException("ERROR", f"_brick_off: Failed to execute: {e}") from e

    return result

#
# ----- Brick Enabled
#
# Body and Heater are On
#
# Return: ESP Result
#
async def _brick_enabled(context:Context) -> str:
    result = context.get_esp_result()

    context.esp = ESP(0,0, STATUS_ENABLED)

    return result


###############################################################################
###
### ----- Brick Heating
###
###############################################################################
#
# Body and Heater are On. Activly Heating the Water
# Calculate ESP
#
# Return: ESP Result
#
async def _brick_heating(context:Context) -> str:
    from .estimator import ESPEstimator, ESP

    result = context.get_esp_result()

    if result == RESULT_ACTIVE:         # Activly Heating?
        if context.is_last_degree():    # Last Degree? (Water@Target and Heating)
            # Y: Return STANDBY
            #    The water is ready, though the heater may continue to run for a while
            context.esp = ESP(0,0, STATUS_READY)
            return RESULT_STANDBY
        else:
            # N: Set Status and ESP
            #    Start Duration Timer to Decrement ESP
            estimator = ESPEstimator(context.coordinator, context.body_type)
            esp:ESP = await estimator.calculate_wrapper(context)
            _LOG.debug(f"HEATING ... ESP: [{context.seconds}] confidence[{context.confidence_pct}%] {context.status}")
            #
            # Start Heating Countdown Timer - return ACTIVE
            #
            timer = context.timer
            if timer is not None:
                await timer.stop()

            timer_interval = 5 * 60   # every 5 minutes
            timer = Timer(
                name = f"HeatingCountdown-{context.body_type}",
                hass = context.hass,
                context = context,
                callback = HeatingCallback(),
                duration = context.seconds,
                interval = timer_interval
            )
            context.timer = timer
            context.esp = esp
            timer.start()

            ###
            ### Start/Reset HeaterWatchdog based on current heater status and calculated rate
            ###
            if esp.rate is not None and esp.degrees_remaining is not None:
                watchdog:HeaterWatchdog = context.coordinator.get_watchdog(context.body_type)
                watchdog.start(context.water_temp, esp.rate, esp.degrees_remaining)
            else:
                _LOG.warning(f"HEATING: [{context.body_type}] no rate or degrees remaining data, skipping watchdog")

    else: # result in [RESULT_STANDBY, RESULT_OFF]
        timer = context.timer
        if (timer is not None):
            await timer.stop()
            context.timer = None
        
        context.coordinator._watchdogs[context.body_type].cancel()

        context.esp = ESP(0,0, STATUS_READY)
    
    return result

#
# ----- Brick Ready
#
# Body and Heater are On. SetPoint reached, Water is Ready
#
# Return: ESP Result
#
async def _brick_ready(context: Context) -> str:
    result = context.get_esp_result()

    context.esp = ESP(0, 0, STATUS_READY)

    return result

#
# ----- Brick Standby
#
# Body and Heater are On. Is Water at SetPoint?
#
# Return: ESP Result
#
async def _brick_standby(context: Context) -> str:
    result = context.get_esp_result()

    if (result == RESULT_STANDBY):
        context.esp = ESP(0, 0, STATUS_READY)

    return result

#
# ----- Brick Maintaining
#
# Body and Heater are On: Maintaining the Water Temperature
#
# Return: ESP Result
#
async def _brick_maintaining(context: Context) -> str:
    result = context.get_esp_result()

    if result == RESULT_ACTIVE:
        # Target Temp Change?
        if context.is_target_change():
            result = RESULT_TARGETCHANGE
        else:
            estimator = ESPEstimator(context.coordinator, context.body_type)
            esp:ESP = await estimator.calculate_wrapper(context)
            _LOG.debug(f"MAINTAINING ... ESP: [{context.seconds}] confidence[{context.confidence_pct}%] {context.status}")

            context.esp = ESP(0, 0, STATUS_READY)
            ###
            ### Start/Reset HeaterWatchdog based on current heater status and calculated rate
            ###
            if esp.rate is not None:
                rate = esp.rate
                degrees_remaining = esp.degrees_remaining
                context.coordinator._watchdogs[context.body_type].start(
                    context.water_temp, rate, degrees_remaining
                )
        
    return result

#
# ----- Brick Disabled
#
# Body and/or Heater are Off
#
# Return: ESP Result
#
async def _brick_disabled(context: Context) -> str:
    result = context.get_esp_result()

    context.esp = ESP(0, 0, STATUS_DISABLED)

    return result


#
# ----- Brick Sensing
#
async def _brick_sensing(context:Context) -> str:
    """Sensing state — suppress STANDBY until settle timer expires"""
    import time

    result = context.get_esp_result()

    is_testing = context.testing
    if is_testing:
        _LOG.debug(f"..._brick_sensing: Testing mode is enabled, skipping sensing")
        return result

    try:
        timer = context.timer

        # ACTIVE or STANDBY - run the timer
        #
        if result in [RESULT_ACTIVE, RESULT_STANDBY]:
            if timer is None: # Create Timer
                #
                # Start Timer - return STANDBY
                #
                timer = Timer(
                    name = f"Sensing-{context.body_type}",
                    hass = context.hass,
                    context = context,
                    callback = SensingCallback(),
                    duration = TIMER_SENSING,
                    interval = 5
                )

                context.timer = timer
                context.esp = ESP(TIMER_SENSING, 0, ESP.format_ms(TIMER_SENSING))

                result = RESULT_STANDBY # Waiting for Timer to complete

                timer.start()
            else: # Timer already created
                if timer.is_running: # Timer is Running
                    result = RESULT_STANDBY # Still waiting for timer to complete
                else:
                    result = RESULT_ACTIVE # Done Sensing
                    await timer.stop()
                    timer = None

        if result in [RESULT_OFF]:  # Heater off stop timer
            if (timer is not None):
                await timer.stop()
                timer = None

        context.timer = timer

    except Exception as e:
        raise ESPException("ERROR", f"Failed to execute Brick Sensing") from e

    #
    # STANDBY if waiting for Sensing Timer to complete
    # ACTIVE, OFF if Timer has completed
    return result

###
### ----- Class SensingCallback --------------------------------------------
###

class SensingCallback(TimerCallback):
    """
    Sensing Timer Callback
    """
    ###
    ### ----- on_timer_interval
    ###
    async def on_timer_interval(self, timer:Timer, elapsed:int, remaining:int) -> None:
        """
        Sensing Timer has completed one Cycle
        """
        # Check if still Counting Down
        context:Context = timer.context
        if context.timer and remaining >= 0:

            esp:ESP = ESP(remaining, 0, ESP.format_ms(remaining))
            context.esp = esp
            context.status = esp.status
            context.seconds = esp.seconds
            context.confidence_pct = esp.confidence_pct
            context.coordinator.update_sensor(context.body_type)

            _LOG.debug(f"SensingCallback.on_timer_interval: [{timer.name}] remaining[{context.seconds}/{context.status}]")

    ###
    ### ----- on_timer_complete
    ###
    async def on_timer_complete(self, timer: Timer) -> None:
        """
        Sensing Timer is Done (has completed all Cycles)
        Scheduled by _brick_sensing when the settle timer starts.
        Re-evaluates the state machine.
        """
        _LOG.debug(f"SensingCallback.on_timer_complete: {timer}")

        context:Context = timer.context
        current_state = context.machine_state

        if current_state != STATE_SENSING:
            _LOG.warning(f"...State[{current_state}], not [Sensing] - exiting")
            return

        _LOG.debug(f"...Sensing [{context.body_type}] completed")

        ###
        ### Re-Run the State Machine. Will pick up at Sensing State
        ###
        try:
            await context.coordinator._execute_with_current_data(context, "Sensing Completed")
        except ESPException as e:
            _LOG.error(f"Failed to Execute with Current Data; {e}")
            raise e

        except Exception as e:
            _LOG.error(f"_sensing_timer_done: Failed to execute State Machine with current data. {e}")

    ###
    ### ----- on_timer_cancelled
    ###

    async def on_timer_cancelled(self, timer: Timer) -> None:
        """Called when timer is stopped externally."""
        _LOG.debug(f"on_timer_cancelled: [{timer.name}]")

###
### ----- Class HeatingCallback -----------------------------------------------
###

class HeatingCallback(TimerCallback):
    """
    Heating Timer Callback
    """
    ###
    ### ----- on_timer_interval
    ###
    async def on_timer_interval(self, timer:Timer, elapsed:int, remaining:int) -> None:
        """
        Heating Countdown Timer has completed one Cycle
        """
        # Check if still Counting Down
        context:Context = timer.context
        if context is not None and remaining >= 0:
            esp:ESP = ESP(remaining, context.esp.confidence, ESP.format_dhm(remaining))
            context.esp = esp
            context.status = esp.status
            context.seconds = esp.seconds
            context.confidence_pct = esp.confidence_pct
            context.coordinator.update_sensor(context.body_type)

            _LOG.debug(f"HeatingCallback.on_timer_interval: [{timer.name}], remaining[{context.seconds}/{context.status}] confidence[{context.confidence_pct}]")


    ###
    ### ----- on_timer_complete
    ###
    async def on_timer_complete(self, timer:Timer) -> None:
        """
        Heating Countdown Timer is Done (has completed all Cycles)
        Scheduled by _brick_sensing when the settle timer starts.
        Re-evaluates the state machine.
        """
        _LOG.debug(f"HeatingCallback.on_timer_complete: [{timer.name}]")

        context:Context = timer.context
        current_state = context.machine_state

        if current_state != STATE_HEATING:
            _LOG.warning(f"...State[{current_state}], not [Heating] - exiting")
            return

        ###
        ### Re-Run the State Machine. Will pick up at Heating State
        ###
        try:
            body_type = context.body_type
            await context.coordinator._execute_with_current_data(context, "Heating Expectation not met")
        except ESPException as e:
            _LOG.error(f"Failed to Execute with Current Data; {e}")
            raise e

        except Exception as e:
            _LOG.error(f"Timer[{timer.name}] Failed to execute State Machine with current data. {e}")

    ###
    ### ----- on_timer_cancelled
    ###

    async def on_timer_cancelled(self, timer: Timer) -> None:
        _LOG.debug(f"HeatingCallback.on_timer_cancelled: [{timer.name}]")


# ---------------------------------------------------------------------------
# STATE MACHINE TRANSITION TABLE
# ---------------------------------------------------------------------------
#
# Result Definitions:
#    OFF     - Circuit Off
#    STANDBY - Circuit On, Heater Enabled, Not Activly Heating
#    ACTIVE  - Circuit On, Heater Enabled, Activly Heating
#
STATE_TRANSITIONS = {
    SM_START: {
        SM_NAME:              "initialize",
        SM_BRICK:             _brick_initialize,
        SM_RESULT_WILDCARD:   STATE_OFF
    },
    STATE_OFF: {
        SM_NAME:              "off",
        SM_BRICK:             _brick_off,
        RESULT_ACTIVE:        STATE_ENABLED,
        RESULT_STANDBY:       STATE_ENABLED,
        RESULT_OFF:           SM_EXIT          # Remain in this State
    },
    STATE_ENABLED: {
        SM_NAME:              "enabled",
        SM_BRICK:             _brick_enabled,
        RESULT_ACTIVE:        STATE_SENSING,
        RESULT_STANDBY:       STATE_SENSING,
        RESULT_OFF:           STATE_DISABLED
    },
    STATE_SENSING: {
        SM_NAME:              "sensing",
        SM_BRICK:             _brick_sensing,
        RESULT_ACTIVE:        STATE_HEATING,
        RESULT_STANDBY:       SM_EXIT,          # Remain in this State
        RESULT_OFF:           STATE_DISABLED

    },
    STATE_HEATING: {
        SM_NAME:              "heating",
        SM_BRICK:             _brick_heating,
        RESULT_ACTIVE:        SM_EXIT,          # Remain in this State
        RESULT_STANDBY:       STATE_READY,
        RESULT_OFF:           STATE_DISABLED
    },
    STATE_READY: {
        SM_NAME:              "ready",
        SM_BRICK:             _brick_ready,
        RESULT_ACTIVE:        STATE_MAINTAINING,
        RESULT_STANDBY:       SM_EXIT,          # Remain in this State
        RESULT_OFF:           STATE_DISABLED
    },
    STATE_STANDBY: {
        SM_NAME:              "standby",
        SM_BRICK:            _brick_standby,
        RESULT_ACTIVE:        STATE_MAINTAINING,
        RESULT_STANDBY:       SM_EXIT,
        RESULT_OFF:           STATE_DISABLED
    },
    STATE_MAINTAINING: {
        SM_NAME:              "maintaining",
        SM_BRICK:             _brick_maintaining,
        RESULT_ACTIVE:        SM_EXIT,
        RESULT_STANDBY:       STATE_STANDBY,
        RESULT_OFF:           STATE_DISABLED,
        RESULT_TARGETCHANGE:  STATE_HEATING
    },
    STATE_DISABLED: {
        SM_NAME:              "disabled",
        SM_BRICK:             _brick_disabled,
        SM_RESULT_WILDCARD:   STATE_OFF
    },
}
