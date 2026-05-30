import logging

from .const import SM_NAME, SM_RESULT_WILDCARD, SM_START, SM_BRICK, SM_EXIT
from .util import Context, ESPException

_LOG = logging.getLogger(__name__)


        
###
### ----- State Machine --------------------------------------------------------
###

class StateMachine:
    """
    Executes the State Machine Bricks and Transitions based on the current State and Brick Results.
    The main execution loop is in esp_state_machine, which executes until there are no more transitions to execute.
    Each Brick is responsible for setting the ESP Result in the Context, which is used to determine the next State Transition.
    """

    def __init__(self, state_transitions: dict, context:Context, cause:str = "Unknown"):
        self._state_transitions:dict = state_transitions
        self._context:Context = context
        self._cause:str = cause

    def is_user_state(self, state: str) -> bool:
        return not state.startswith("$")
    
    async def execute(self):

        """
        Core state machine — evaluates on every state change, filtered to
        only the ScreenLogic entities discovered at startup.
        """
        import time

        max_iterations = 10
        iteration = 0
        current_transitions = None

        try:
            body_type = self._context.body_type
            # Get Current State Machine State
            current_state = self._context.machine_state
            _LOG.debug(f"Execute: [{self._context.body_type}] Starting-At[{current_state}] Because-of[{self._cause}] ")

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
                ###
                if (breadcrumbs.__contains__(current_state)):
                    break
                else:
                    breadcrumbs.append(current_state)

                iteration += 1

                current_transitions = self._state_transitions.get(current_state, None)

                if (current_transitions is not None):
                    try:
                        name = current_transitions.get(SM_NAME, None)
                        brick = current_transitions.get(SM_BRICK, None)
                        if brick is not None:
                            # Execute Brick, get Result
                            self._context.machine_state = current_state
                            result = await brick(self._context)
                        else:
                            _LOG.error(f"StateMachine: [{body_type}] State[{current_state}] No Brick found. Transitions: {current_transitions}")
                            raise ESPException("ERROR", "StateMachine: [{body_type}] State[{current_state}] No Brick found. Transitions: {current_transitions}")

                        # Look up the Result in the Current State Transisition table
                        #   If that fails, lookup Wildcard in the table
                        next_state = current_transitions.get(result)
                        if next_state is None:
                            next_state = current_transitions.get(SM_RESULT_WILDCARD)
                            if next_state is None:
                                raise ESPException("ERROR", f"StateMachine: Failed to find TransitionState: State[{current_transitions}] Result[{result}]")
                        
                        _LOG.debug(f"...[{current_state}] [{brick.__name__}({name})] --> [{result}] --> NextState[{next_state}]")
                    
                    except Exception as e:
                        _LOG.error(f"StateMachine: Failed to execute State [{current_state}]; {e}")
                        raise ESPException("ERROR", f"StateMachine: Failed to execute State [{current_state}]") from e

                    current_state = next_state

                    # Only advance State if it's not $EXIT
                    #   context.machine_state is the last state we executed
                    #   So we'll pick up on this state next time around
                    #
                    if next_state is not None and next_state != SM_EXIT:
                        self._context.machine_state = current_state
                else:
                    _LOG.error(f"State Machine: [{body_type}] No Transitions found for State:{current_state}")
                    raise ESPException("ERROR", f"State Machine: [{body_type}] No Transitions found for State:{current_state}")
            
            # End While Executing State Machine

            _LOG.debug(f"...Breadcrumbs:{breadcrumbs}")

            if iteration >= max_iterations:
                _LOG.error(f"StateMachine: [{body_type}] Infinte Loop Detected; aborting State Machine")
                raise ESPException("ERROR", "State Machine: Infinite loop detected")

            return 

        except Exception as e:
            _LOG.error(f"StateMachine: Failed to execute State[{current_state}]: {e}")
            raise ESPException("ERROR", f"StateMachine: Failed to execute State[{current_state}]") from e

