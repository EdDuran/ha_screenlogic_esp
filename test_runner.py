import logging
import asyncio
import json
import time
import traceback
from .state_machine import *
from .const import *

_LOGGER = logging.getLogger(__name__)


def _load_json(path):
    with open(path, "r") as f:
        return json.load(f)


async def run_scenario(hass, file):
    import json, time

    #file_path = f"/config/custom_components/test/{file}"
    file_path = hass.config.path("custom_components/screenlogic_esp/test", file)
    _LOGGER.info(f"run_scenario [{file_path}]")

    try:
        scenario = await hass.async_add_executor_job(_load_json, file_path)

        body_type = BODY_TYPE_SPA
        context = Context(body_type)

        start = time.time()

        for step in scenario["steps"]:
            for key, value in step.items():
                if key not in ("t", "expected"):
                    context.set(key, value)

            value = start + step.get("t",0)
            context.set("timestamp", value)

            _LOGGER.debug(f"...Context: {context}")
            await esp_state_machine(context, cause="test")

            _LOGGER.info(
                "t=%s temp=%s status=%s esp=%s",
                step.get("t"),
                context.get("water_temp"),
                context.get("status"),
                context.get("esp"),
            )

            if "expected" in step:
                expected = step["expected"]
                for k, v in expected.items():
                    if context.get(k) != v:
                        _LOGGER.error(
                            "FAIL: %s expected %s got %s",
                            k, v, context.get(k)
                        )
                        
    except asyncio.CancelledError:
        _LOGGER.warning("Scenario cancelled")
        raise

    except Exception as e:
        _LOGGER.error(traceback.format_exc())
        _LOGGER.error(f"Test [{file_path}] Failed; {e}")


    async def _execute_with_test_data(self, context: Context, entity_id: str, cause: str = "Unknown"):
        """
        Execute the State Machine with Current ScreenLogic data
        """
        try:
            body_type = context.body_type
            _LOGGER.info(f"_execute_with_test_data: Body[{body_type}] EntityId[{entity_id}] Cause[{cause}]")
            config = self._config.get(body_type)
            _LOGGER.debug(f"...Config[{config}]")

            _LOGGER.debug(f"...Context: {context}")
            _LOGGER.debug(f"...Config:  {config}")

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

            status = context.status
            esp = context.esp
            _LOGGER.debug(f"...[{body_type}] Cause[{cause}] Status[{status}] ESP[{esp}]")

        except Exception as e:
            _LOGGER.error(f"_execute_with_test_data: Error [{e}] Context[{context}]  ")
            raise ESPException("ERROR", f"_execute_with_test_data: {e}") from e

