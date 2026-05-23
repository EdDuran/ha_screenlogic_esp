import logging
import asyncio
import json
import os
import time
import traceback
from datetime import datetime
import debugpy

from homeassistant.exceptions import (
    ServiceValidationError,  # bad input from caller — their fault
    HomeAssistantError      # something went wrong — your fault
)

from .state_machine import *
from .const import *
from .util import HistoryAdapter

_LOG = logging.getLogger(__name__)

    
###
### ---------------------------------------------------------------------------
###

_PATH = "test"  # /config/test

async def run_scenario(hass, scenario_file, data_file):
    import time

    entries = hass.config_entries.async_entries(DOMAIN)
    coordinator = hass.data[DOMAIN][entries[0].entry_id][ADDONS_COORDINATOR]

    try:
        test_runner = TestRunner(data_file, scenario_file, _PATH, coordinator)
        test_result = await test_runner.run(hass)

        if not test_result:
            _LOG.error(f"***** TEST FAILED: {scenario_file} *****")
        else:
            _LOG.info(f"***** TEST PASSED: {scenario_file} *****")
    
    except ServiceValidationError as e:
        _LOG.error(f"***** TestRunner VALIDATION ERROR: {scenario_file} ***** {e}")
        raise

    except Exception as e:
        _LOG.error(traceback.format_exc())
        raise HomeAssistantError(f"TestRunner [{scenario_file}] Failed") from e


###
### ----- Class TestRunner ----------------------------------------------------
###

class TestRunner:
    def __init__(self, data_file, scenario_file, path, coordinator:ESPCoordinator):
        self._data_file = data_file
        self._scenario_file = scenario_file
        self._path = path
        self._coordinator = coordinator

        _LOG.debug(f"***** ESP Test Runner *****")
        _LOG.debug(f"...Path     : [{path}]")
        _LOG.debug(f"...Scenario : [{scenario_file}]")
        _LOG.debug(f"...Data     : [{data_file}]")

    async def run(self, hass) -> bool:

        debugpy.breakpoint()

        try:
            scenario_path = hass.config.path(self._path, self._scenario_file)
            data_path = hass.config.path(self._path, self._data_file)

            scenario = await hass.async_add_executor_job(self._load_scenario, scenario_path)

            context = Context()
            context.hass = hass

            ### Use the File History Adapter to read historical data JSON
            history_adapter = FileHistoryAdapter(data_path, context)
            await history_adapter.load()
            body_type = history_adapter.body_type

            context.coordinator = self._coordinator
            context.history_adapter = history_adapter
            context.testing = True          # Testing mode disables sensing

            start = time.time()

            for step in scenario["steps"]:
                ### For each step
                ###   Copy Entity Values to Context
                for key, value in step.items():
                    if key not in ("t", "expected"):
                        context.set(key, value)
                # end for each step key

                ### Set the timestamp
                value = start + step.get("t",0)
                context.set("timestamp", value)

                ### Execute the State Machine
                await esp_state_machine(context, cause=f"TESTING: {body_type} {self._scenario_file}")

                _LOG.info(f"Step: t=[{step.get('t'):3d}], status=[{context.get('status'):8s}] {context.get('esp')}")

                if not self._check_expected_results(step, context):
                    _LOG.error(f"FAILED: Step [{step.get('t')}]")
                    return False    # Test Failed

            # end for each step

            return True     # Test Successful

        except asyncio.CancelledError as e:
            raise HomeAssistantError(f"TestRunner Scenario [{self._scenario_file}] Cancelled") from e
        

    def _load_scenario(self,scenario_path):
        if not os.path.exists(scenario_path):
            raise ServiceValidationError(f"Test Scenario file not found: {scenario_path}")   
        
        with open(scenario_path, "r") as f:
            return json.load(f)




    def _check_expected_results(self, step:Step, context:Context) -> bool:
        """
        Check if the actual values match the expected values.
        """
        expected = step.get("expected", {})
        if not expected:
            return True
        
        for k, v in expected.items():
            if (k == "esp"):
                return self._check_esp(context.get(k), v)
            else:
                if context.get(k) != v:
                    _LOG.error(f"FAIL: [{k}] expected [{v}] got [{context.get(k)}]")
                    return False
        

    def _check_esp(self, actual_esp:ESP, expected:str) -> bool:
        expected_esp = ESP(
            seconds = expected["seconds"],
            confidence = expected["confidence"]
        )

        if actual_esp != expected_esp:
            _LOG.error(f"...Test Failed")
            _LOG.error(f"...ESP Expected: Seconds[{expected_esp.seconds}], Confidence[{expected_esp.confidence}]")
            _LOG.error(f"...ESP Actual  : Seconds[{actual_esp.seconds}], Confidence[{actual_esp.confidence}]")
            return False
        return True

###
### ----- Class FileHistoryAdapter --------------------------------------------------
###
class FileHistoryAdapter(HistoryAdapter):
    def __init__(self, data_path:str, context:Context):
        _LOG.debug(f"FileHistoryAdapter:")
        self._data_path = data_path
        self._context = context
        self._data = None

        _LOG.debug(f"...Data: [{data_path}]")

    async def load(self):
        await self._load_history()

    async def _load_history(self):
        if self._data is None:
            if not os.path.exists(self._data_path):
                raise ServiceValidationError(f"Data file not found: {self._data_path}")
            
            with await asyncio.get_event_loop().run_in_executor(None, open, self._data_path, "r") as f:
                self._data = json.load(f)

            history = {
                entity_id: [StateProxy(s) for s in states]
                for entity_id, states in self._data["history"].items()
            }
    
            self._history = history
            self._metadata = self._data["metadata"]
            self._context.body_type = self.body_type

    async def get_history(self):
        await self._load_history()
        return self._history, self.starttime, self.endtime

    def get_current_value(self, body_config, screenlogic_entity:str):
        """
        Get the current value of a specific attribute from the context.
        """
        return self._context.get(screenlogic_entity.lower())
    
    @property
    def starttime(self) -> str:
        return self._metadata.get("start", 0)

    @property
    def endtime(self) -> str:
        return self._metadata.get("end", 0)
    
    @property
    def body_type(self) -> str:
        return self._metadata.get("body_type", "")

    @property
    def now(self) -> float:
        return datetime.fromisoformat(self.endtime).timestamp()

###
### ----- Class Step ----------------------------------------------------------
###

class Step:
    def __init__(self, data):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)
    

###
### ----- Class StateProxy ----------------------------------------------------
###

class StateProxy:
    """Mimics HA State object for JSON playback."""
    
    def __init__(self, d: dict):
        self.state       = d["state"]
        self.entity_id   = d["entity_id"]
        self.attributes  = d["attributes"]
        self.last_updated = datetime.fromisoformat(d["last_updated"])

    def __repr__(self):
        return f"StateProxy(entity_id={self.entity_id}, state={self.state}, last_updated={self.last_updated})"

