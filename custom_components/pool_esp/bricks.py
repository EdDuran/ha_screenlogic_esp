#
# ----- Brick Initialize
#
# Initialize Context
#
# Return: RESULT_OFF
#
import logging
from unittest import result

from custom_components.pool_esp.coordinator import ESPCoordinator
from custom_components.pool_esp.sensor import ESPSensor
from opentelemetry import context

from .const import STATUS_UNKNOWN, TIMER_SENSING, STATUS_INITIALIZING, STATUS_ENABLED, STATUS_READY, STATUS_DISABLED, RESULT_ACTIVE, RESULT_OFF, RESULT_STANDBY, RESULT_TARGETCHANGE
from .estimator import ESPEstimator
from .heater_watchdog import HeaterWatchdog
from .state_machine import SM_RESULT_WILDCARD, SM_START, SM_BRICK, SM_EXIT, Brick
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

BRICK_INITIALIZE = "initialize"
BRICK_OFF        = "off"
BRICK_ENABLED    = "enabled"
BRICK_SENSING    = "sensing"
BRICK_STANDBY    = "standby"
BRICK_HEATING    = "heating"
BRICK_READY      = "ready"
BRICK_MAINTAINING = "maintaining"
BRICK_DISABLED   = "disabled"





_LOG = logging.getLogger(__name__)

###############################################################################
###
### ----- Initialize Brick ----------------------------------------------------
###
###############################################################################

@Brick.register(BRICK_INITIALIZE)
class InitializeBrick(Brick):

    def __init__(self, name:str, context:Context):
        super().__init__(name, context)

    async def execute(self) -> str:
        self.context.esp = ESP(0, 0, STATUS_INITIALIZING)
        return self._get_result()


###############################################################################
###
### ----- Off Brick -----------------------------------------------------------
###
###############################################################################
#
# Body Circuit and Heater are Off
#   Calculate ETA just in case anyone wants to
#
# Return: ESP Result
#
@Brick.register("off")
class OffBrick(Brick):
    
    def __init__(self, name:str, context:Context):
        super().__init__(name, context)

    async def execute(self) -> str:
        from .estimator import ESPEstimator, ESP

        result = self._get_result()

        if result in (RESULT_OFF, RESULT_TARGETCHANGE):
            if self.context.circuit == "off" and self.context.is_at_setpoint():
                self.context.esp = ESP(0,0, STATUS_UNKNOWN)
            else:
                estimator = ESPEstimator(self.context.coordinator, self.context.body_type)
                self.context.esp = await estimator.calculate_wrapper(self.context)

            result = RESULT_OFF

        return result

###############################################################################
###
### ----- Enabled Brick -------------------------------------------------------
###
###############################################################################
#
# Body and Heater are On
#
# Return: ESP Result
#
@Brick.register("enabled")
class EnabledBrick(Brick):
    def __init__(self, name:str, context:Context):
        super().__init__(name, context)
    
    async def execute(self) -> str:
        result = self._get_result()

        self.context.prior_target_temp = None    # Reset prior target temp when heater enabled
        self.context.esp = ESP(0,0, STATUS_ENABLED)

        return result


###############################################################################
###
### ----- Sensing Brick -------------------------------------------------------
###
###############################################################################

@Brick.register("sensing")
class SensingBrick(Brick):
    def __init__(self, name:str, context:Context):
        super().__init__(name, context) 

    async def execute(self) -> str:
        """Sensing state — suppress STANDBY until settle timer expires"""
        import time

        result = self._get_result()

        if self.context.testing:
            self._log_debug(f"..._brick_sensing: Testing mode is enabled, skipping sensing")
            return result

        try:
            timer = self.context.timer

            # ACTIVE or STANDBY - run the timer
            #
            if result in [RESULT_ACTIVE, RESULT_STANDBY]:
                if timer is None: # Create Timer
                    #
                    # Start Timer - return STANDBY
                    #
                    timer = Timer(
                        name = f"Sensing-{self.context.body_type}",
                        hass = self.context.hass,
                        context = self.context,
                        callback = SensingCallback(),
                        duration = TIMER_SENSING,
                        interval = 5
                    )

                    self.context.timer = timer
                    self.context.esp = ESP(TIMER_SENSING, 0, ESP.format_ms(TIMER_SENSING))

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

            self.context.timer = timer

        except Exception as e:
            raise ESPException(f"Failed to execute Brick Sensing") from e

        #
        # STANDBY if waiting for Sensing Timer to complete
        # ACTIVE, OFF if Timer has completed
        return result


###############################################################################
###
### ----- Heating Brick ------------------------------------------------------
###
###############################################################################
#
# Body and Heater are On. Activly Heating the Water
# Calculate ESP
#
# Return: ESP Result
#
@Brick.register("heating")
class HeatingBrick(Brick):
    def __init__(self, name:str, context:Context):
        super().__init__(name, context)
    
    async def execute(self) -> str:
        from .estimator import ESPEstimator, ESP

        result = self._get_result()

        if result in (RESULT_ACTIVE, RESULT_TARGETCHANGE):         # Activly Heating?
            if self.context.is_last_degree():    # Last Degree? (Water@Target and Heating)
                # Y: Return STANDBY
                #    The water is ready, though the heater may continue to run for a while
                result = RESULT_STANDBY
            else:
                # N: Set Status and ESP
                #    Start Duration Timer to Decrement ESP
                estimator = ESPEstimator(self.context.coordinator, self.context.body_type)
                esp:ESP = await estimator.calculate_wrapper(self.context)
                #
                # Start Heating Countdown Timer - return ACTIVE
                #
                timer = self.context.timer
                if timer is not None:
                    await timer.stop()

                timer_interval = 5 * 60   # every 5 minutes
                timer = Timer(
                    name = f"HeatingCountdown-{self.context.body_type}",
                    hass = self.context.hass,
                    context = self.context,
                    callback = HeatingCallback(),
                    duration = self.context.seconds,
                    interval = timer_interval
                )
                self.context.timer = timer
                self.context.esp = esp
                timer.start()

                ###
                ### Start/Reset HeaterWatchdog based on current heater status and calculated rate
                ###
                if esp.rate is not None and esp.degrees_remaining is not None:
                    watchdog:HeaterWatchdog = self.context.coordinator.get_watchdog(self.context.body_type)
                    watchdog.start(self.context.water_temp, esp.rate, esp.degrees_remaining)
                else:
                    self._log_debug(f"[{self.context.body_type}] no rate or degrees remaining data, skipping watchdog")
        # end if ACTIVE or TARGETCHANGE

        ### STANDBY - Stop the Timer. We're "READY"
        if result == RESULT_STANDBY:
            timer = self.context.timer
            if timer is not None:
                await timer.stop()
                context.timer = None
            
            self.context.coordinator._watchdogs[self.context.body_type].cancel()

            self.context.esp = ESP(0,0, STATUS_READY)
        
        return result


###############################################################################
###
### ----- Ready Brick ---------------------------------------------------------
###
###############################################################################
#
# Body and Heater are On. SetPoint reached, Water is Ready
#
# Return: ESP Result
#
@Brick.register("ready")
class ReadyBrick(Brick):
    def __init__(self, name:str, context:Context):
        super().__init__(name, context)

    async def execute(self) -> str:
        result = self._get_result()
        self.context.esp = ESP(0, 0, STATUS_READY)
        return result

###############################################################################
###
### ----- Standby Brick -------------------------------------------------------
###
###############################################################################
#
# Body and Heater are On. Is Water at SetPoint?
#
# Return: ESP Result
#
@Brick.register("standby")
class StandbyBrick(Brick):
    def __init__(self, name:str, context:Context):
        super().__init__(name, context)
        
    async def execute(self) -> str:
        result = self._get_result()
        if (result == RESULT_STANDBY):
            self.context.esp = ESP(0, 0, STATUS_READY)
        return result


###############################################################################
###
### ----- Maintaining Brick ---------------------------------------------------
###
###############################################################################
#
# Body and Heater are On: Maintaining the Water Temperature
#
# Return: ESP Result
#
@Brick.register("maintaining")
class MaintainingBrick(Brick):
    def __init__(self, name:str, context:Context):
        super().__init__(name, context)

    async def execute(self) -> str:
        result = self._get_result()

        if result == RESULT_ACTIVE:
            estimator = ESPEstimator(self.context.coordinator, self.context.body_type)
            esp:ESP = await estimator.calculate_wrapper(self.context)

            ###
            ### Start/Reset HeaterWatchdog based on current heater status and calculated rate
            ###
            if esp.rate is not None:
                rate = esp.rate
                degrees_remaining = esp.degrees_remaining
                self.context.coordinator._watchdogs[self.context.body_type].start(
                    self.context.water_temp, rate, degrees_remaining
                )
            
            self.context.esp = ESP(0, 0, STATUS_READY)
        
        return result

###############################################################################
###
### ----- Disabled Brick ------------------------------------------------------
###
###############################################################################
#
# Body and/or Heater are Off
#
# Return: ESP Result
#
@Brick.register("disabled")
class DisabledBrick(Brick):
    def __init__(self, name:str, context:Context):
        super().__init__(name, context)

    async def execute(self) -> str:
        result = self._get_result()
        self.context.esp = ESP(0, 0, STATUS_DISABLED)
        return result



###############################################################################
###############################################################################
###############################################################################



###############################################################################
###
### ----- SensingCallback -----------------------------------------------------
###
###############################################################################
#
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
            coordinator:ESPCoordinator = context.coordinator
            coordinator.update_sensor(context.body_type, "esp")

            ### _LOG.debug(f"SensingCallback.on_timer_interval: [{timer.name}] remaining[{context.seconds}/{context.status}]")

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
            ### Edgecase when State moved on and Timer no longer relevant. Just exit and do nothing.
            ###_LOG.warning(f"...State[{current_state}], not [Sensing] - exiting")
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

###############################################################################
###
### ----- HeatingCallback -----------------------------------------------------
###
###############################################################################
#
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
            ### Edgecase when State moved on and Timer no longer relevant. Just exit and do nothing.
            ###_LOG.warning(f"...State[{current_state}], not [Heating] - exiting")
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


###############################################################################
###
### ----- State Machine Transition Table --------------------------------------
###
###############################################################################
#
# Result Definitions:
#    OFF          - Circuit Off
#    STANDBY      - Circuit On, Heater Enabled, Not Activly Heating
#    ACTIVE       - Circuit On, Heater Enabled, Activly Heating
#    TARGETCHANGE - Target Temperature has changed (increased)
#
STATE_TRANSITIONS = {
    SM_START: {
        SM_BRICK:             BRICK_INITIALIZE,
        SM_RESULT_WILDCARD:   STATE_OFF,
        RESULT_TARGETCHANGE:  STATE_OFF
    },
    STATE_OFF: {
        SM_BRICK:             BRICK_OFF,
        RESULT_ACTIVE:        STATE_ENABLED,
        RESULT_STANDBY:       STATE_ENABLED,
        RESULT_OFF:           SM_EXIT,         # Remain in this State
        RESULT_TARGETCHANGE:  SM_EXIT
    },
    STATE_ENABLED: {
        SM_BRICK:             BRICK_ENABLED,
        RESULT_ACTIVE:        STATE_SENSING,
        RESULT_STANDBY:       STATE_SENSING,
        RESULT_OFF:           STATE_DISABLED,
        RESULT_TARGETCHANGE:  STATE_SENSING
    },
    STATE_SENSING: {
        SM_BRICK:             BRICK_SENSING,
        RESULT_ACTIVE:        STATE_HEATING,
        RESULT_STANDBY:       SM_EXIT,          # Remain in this State
        RESULT_OFF:           STATE_DISABLED,
        RESULT_TARGETCHANGE:  SM_EXIT           # Target Change? Keep sensing
    },
    STATE_HEATING: {
        SM_BRICK:             BRICK_HEATING,
        RESULT_ACTIVE:        SM_EXIT,          # Remain in this State
        RESULT_STANDBY:       STATE_READY,
        RESULT_OFF:           STATE_DISABLED,
        RESULT_TARGETCHANGE:  SM_EXIT           # Ignore Target Temp changes while heating, will pick up on next cycle when re-evaluated
    },
    STATE_READY: {
        SM_BRICK:             BRICK_READY,
        RESULT_ACTIVE:        STATE_MAINTAINING,
        RESULT_STANDBY:       SM_EXIT,          # Remain in this State
        RESULT_OFF:           STATE_DISABLED,
        RESULT_TARGETCHANGE:  STATE_HEATING     # Target Temp changes while at setpoint should trigger heating again
    },
    STATE_STANDBY: {
        SM_BRICK:             BRICK_STANDBY,
        RESULT_ACTIVE:        STATE_MAINTAINING,
        RESULT_STANDBY:       SM_EXIT,
        RESULT_OFF:           STATE_DISABLED,
        RESULT_TARGETCHANGE:  STATE_HEATING     # Target Temp changes while at setpoint should trigger heating again
   },
    STATE_MAINTAINING: {
        SM_BRICK:             BRICK_MAINTAINING,
        RESULT_ACTIVE:        SM_EXIT,
        RESULT_STANDBY:       STATE_STANDBY,
        RESULT_OFF:           STATE_DISABLED,
        RESULT_TARGETCHANGE:  STATE_HEATING     # Target Temp changes while at setpoint should trigger heating again
    },
    STATE_DISABLED: {
        SM_BRICK:             BRICK_DISABLED,
        SM_RESULT_WILDCARD:   STATE_OFF
    },
}


