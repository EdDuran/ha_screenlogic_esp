import logging
import traceback

from custom_components.pool_esp.sensor import ESPSensor

from .coordinator import *
from .const import *
from .util import *
from .timer import Timer, TimerCallback
from .estimator import *

_LOG = logging.getLogger(__name__)

        
###
### ----- State Machine --------------------------------------------------------
###

def is_user_state(state: str) -> bool:
    return not state.startswith("$")

async def esp_state_machine(context:Context, cause:str = "Unknown"):
    """
    Core state machine — evaluates on every state change, filtered to
    only the ScreenLogic entities discovered at startup.
    """
    import time

    max_iterations = 10
    iteration = 0
    current_transitions = None

    try:
        body_type = context.body_type
        # Get Current State Machine State
        current_state = context.machine_state
        _LOG.info(f"StateMachine: Cause[{cause}] State[{current_state}]")

        #
        # Execute State Machine - Until:
        #  o SM_EXIT is returned
        #  o We're going in a loop (detect by breadcrumbs)
        #
        breadcrumbs:list = []
        result = None
        while iteration < max_iterations and current_state is not None and current_state != SM_EXIT:

            ###
            ### If we've executed this state before, we can exit to prevent infinite loops

            if (breadcrumbs.__contains__(current_state)):
                _LOG.warning(f"...Have been here before; Done. Breadcrumbs: {breadcrumbs}")
                break
            else:
                breadcrumbs.append(current_state)

            _LOG.debug(f"...Breadcrumbs:{breadcrumbs}")

            iteration += 1

            current_transitions = STATE_TRANSITIONS.get(current_state, None)

            if (current_transitions is not None):
                try:
                    name = current_transitions.get(SM_NAME, None)
                    brick = current_transitions.get(SM_BRICK, None)
                    if brick is not None:
                        # Execute Brick, get Result
                        _LOG.debug(f"...Execute [{current_state}] Brick[{brick.__name__}({name})]")
                        context.machine_state = current_state
                        result = await brick(context)
                    else:
                        _LOG.error(f"StateMachine: [{body_type}] State[{current_state}] No Brick found. Transitions: {current_transitions}")
                        raise ESPException("ERROR", "StateMachine: [{body_type}] State[{current_state}] No Brick found. Transitions: {current_transitions}")

                    # Look up the Result in the Current State Transisition table
                    #   If that fails, lookup Wildcard in the table
                    next_state = current_transitions.get(result)
                    if next_state is None:
                        next_state = current_transitions.get(RESULT_WILDCARD)
                        if next_state is None:
                            raise ESPException("ERROR", f"StateMachine: Failed to find TransitionState: State[{current_transitions}] Result[{result}]")
                    
                    _LOG.debug(f"   == [{result}] --> NextState[{next_state}]")
                
                except Exception as e:
                    _LOG.error(f"StateMachine: Failed to execute State [{current_state}]; {e}")
                    raise ESPException("ERROR", f"StateMachine: Failed to execute State [{current_state}]") from e

                current_state = next_state

                # Only advance State if it's not $EXIT
                #   context.machine_state is the last state we executed
                #   So we'll pick up on this state next time around
                #
                if next_state is not None and next_state != SM_EXIT:
                    context.machine_state = current_state
            else:
                _LOG.error(f"State Machine: [{body_type}] No Transitions found for State:{current_state}")
                raise ESPException("ERROR", f"State Machine: [{body_type}] No Transitions found for State:{current_state}")

        # End While Executing State Machine

        if iteration >= max_iterations:
            _LOG.error(f"StateMachine: [{body_type}] Infinte Loop Detected; aborting State Machine")
            raise ESPException("ERROR", "State Machine: Infinite loop detected")

        return 

    except Exception as e:
        _LOG.error(f"StateMachine: Failed to execute State[{current_state}]: {e}")
        raise ESPException("ERROR", f"StateMachine: Failed to execute State[{current_state}]") from e

#
# ----- Brick Initialize
#
# Initialize Context
#
# Return: RESULT_OFF
#
async def _brick_initialize(context: Context) -> str:
    context.status = STATUS_INITIALIZING
    context.esp = ESP(0, 0)
    return context.get_esp_result()

#
# ----- Brick Off
#
# Body Circuit and Heater are Off
#   Calculate ETA just in case anyone wants to
#
# Return: ESP Result
#
async def _brick_off(context: Context) -> str:
    from .estimator_refactor import ESPEstimator, ESP

    result = context.get_esp_result()

    try:
        if result == RESULT_OFF:
            if context.is_at_setpoint():
                context.status = f"{STATUS_READY}"
                context.esp = ESP(0,0)
            else:
                estimator = ESPEstimator(context.coordinator, context.body_type)
                esp: ESP = await estimator.calculate_wrapper(context)
                _LOG.debug(f"ESP: [{esp.seconds} {esp.display_label}], confidence[{esp.confidence} is '{esp.confidence_label}']")

                context.esp = esp
                context.status = esp.display_label

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

    context.status = STATUS_ENABLED
    context.esp = ESP(0,0)

    return result


def _countdown_timer_cycle(context:Context, cycle):
    """
    Countdown Timer (callback)
    Called every TIMER_FIVE_MINUTES to decrement the ESP value
    """
    if context is not None:
        body_type = context.body_type
        esp = context.esp
        machine_state = context.machine_state

        _LOG.info(f"_countdown_timer_cycle: [{body_type}] context[{context}]")

        if esp is not None:
            if machine_state is not None and machine_state != STATE_OFF:
                # Decrement ESP 5 minutes - save new esp to the context
                new_esp = esp - (5 * 60)
                if new_esp >= 0:
                    context.esp = ESP(new_esp, 0)
                    days, hours, mins, fmt_esp = get_formatted_esp(new_esp)
                    context.status = f"🔥 {fmt_esp}"
                    _LOG.info(f"..Five Minute Timer, esp [{esp} -> {new_esp}]")
                # if new_esp > 0
            else:
                # State off - turn off Timer
                context.timer.stop()
            # end if machine_state not None
        else:
            # esp is None
            context.esp = ESP(0,0)
        # end if esp is not None
    # end if context not None

#
# ----- Brick Heating
#
# Body and Heater are On. Activly Heating the Water
# Calculate ESP
#
# Return: ESP Result
#
async def _brick_heating(context:Context) -> str:
    from .estimator_refactor import ESPEstimator, ESP

    result = context.get_esp_result()

    if result == RESULT_ACTIVE:         # Activly Heating?
        if context.is_last_degree():    # Last Degree? (Water@Target and Heating)
            # Y: Return STANDBY
            #    The water is ready, though the heater may continue to run for a while
            context.status = STATUS_HEATING
            context.esp = ESP(0,0)
            result = RESULT_STANDBY
        else:
            # N: Set Status and ESP
            #    Start Duration Timer to Decrement ESP
            estimator = ESPEstimator(context.coordinator, context.body_type)
            esp: ESP = await estimator.calculate_wrapper(context)
            _LOG.debug(f"ESP: [{esp.seconds}] confidence[{esp.confidence}] {esp.display_label}")

            context.esp = esp
            context.status = esp.display_label

###            start_duration_timer(context, 0, TIMER_FIVE_MINUTES, _countdown_timer_cycle, None)
    else: # result in [RESULT_STANDBY, RESULT_OFF]
        context.timer = None 
        context.esp = ESP(0,0)
###        stop_duration_timer(context)
    
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

    context.status = STATUS_READY
    context.esp = ESP(0, 0)

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
        context.status = f"{STATUS_READY}"
        context.esp = ESP(0, 0)

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

        context.status = f"{STATUS_READY}"
        context.esp = ESP(0, 0)
        
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

    # Ensure Duration Timer is Stopped
###    stop_duration_timer(context)

    context.status = f"{STATUS_DISABLED}"
    context.esp = ESP(0, 0)

    return result


#
# ----- Brick Sensing
#
async def _brick_sensing(context:Context) -> str:
    """Sensing state — suppress STANDBY until settle timer expires"""
    import time

    result = context.get_esp_result()
    _LOG.debug(f"..._brick_sensing: ESP Result is [{result}]")

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
                    name = f"Sensing [{context.body_type}]",
                    hass = context.hass,
                    context = context,
                    callback = SensingCallback(),
                    cycles = TIMER_SENSING / 5,
                    interval = 5
                )

                context.timer = timer
                context.status = f"{STATUS_SENSING}" 
                context.esp = ESP(0, 0)

                timer.start()
                result = RESULT_STANDBY # Waiting for Timer to complete
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
    ### ----- on_timer_cycle
    ###
    async def on_timer_cycle(self, timer:Timer, elapsed:int, remaining:int) -> None:
        """
        Sensing Timer has completed one Cycle
        """
        # Check if still Counting Down
        context:Context = timer.context
        if context.timer and remaining >= 0:
            body_type = context.body_type
            seconds_remaining = remaining * 5
            _LOG.debug(f"...[{body_type}] Cycle[{seconds_remaining}]")
            context.esp = ESP(seconds_remaining, 0)

            _LOG.info(f"SensingCallback.on_timer_cycle: {timer}, Seconds remaining[{seconds_remaining}]")

            coordinator:ESPCoordinator = context.coordinator
            coordinator.update_sensor(body_type)

    ###
    ### ----- on_timer_complete
    ###
    async def on_timer_complete(self, timer: Timer) -> None:
        """
        Sensing Timer is Done (has completed all Cycles)
        Scheduled by _brick_sensing when the settle timer starts.
        Re-evaluates the state machine.
        """
        _LOG.info(f"SensingCallback.on_timer_complete: {timer}")

        context:Context = timer.context
        body_type = context.body_type
        current_state = context.machine_state

        if current_state != STATE_SENSING:
            _LOG.warning(f"...State[{current_state}], not [Sensing] - exiting")
            return

        _LOG.debug(f"...Sensing [{body_type}] complete")

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
        _LOG.info(f"SensingCallback.on_timer_cancelled: {timer}")

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
        RESULT_WILDCARD:      STATE_OFF
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
        RESULT_WILDCARD:      STATE_OFF
    },
}



