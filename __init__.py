from __future__ import annotations
from homeassistant.config_entries import ConfigEntry, ConfigEntryNotReady, callback
from homeassistant.components.homeassistant import IssueSeverity
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import device_registry as dr

from .coordinator import ESPCoordinator
from .const import (
    DOMAIN,
    PLATFORMS,
    ADDONS_COORDINATOR,
    POOL_ADAPTER_CONFIG,
    DEFAULT_POOL_ADAPTER
)
from .auto_discovery import auto_discovery
from .util import *
from .test_runner import *
import logging
import debugpy
import os

_LOG = logging.getLogger(__name__)


def _start_debugger():

    if not os.getenv("HA_DEBUG"):
        _LOG.warning("HA_DEBUG is not set...Debugging will not be available")
        return

    try:
        debugpy.listen(("0.0.0.0",5678))
        _LOG.debug("Debugger listening on 5678")
    except RuntimeError:
        _LOG.warning("Debugger already active")

    if not debugpy.is_client_connected():
        _LOG.warning("Waiting for debugger attach...")
        debugpy.wait_for_client()

        debugpy.breakpoint()

    _LOG.debug("Debugger attached")

###
### ----- Reload Integration ---------------------------------------------------
###
async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry."""
    _LOG.info(f"__init__.async_reload_entry")
    await hass.config_entries.async_reload(entry.entry_id)

###
### ----- Unload Integration ---------------------------------------------------
###
async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle unload — must be implemented for reload to work."""
    _LOG.info(f"__init__.async_unload_entry")

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Pop add-on data
    hass.data.pop(ADDONS_COORDINATOR, None)

    return unload_ok
    


###
### ----- Setup ----------------------------------------------------------------
###
def setup(hass: HomeAssistant, config: ConfigType) -> bool:

    _LOG.info(f"__init__.setup")

    ## Save ESP configuration
    hass.data.setdefault(DOMAIN, {})
    if DOMAIN in config:
        hass.data[DOMAIN]["yaml_config"] = config[DOMAIN]

    # Return boolean to indicate that initialization was successful.
    return True


###
### ----- Setup Integration ----------------------------------------------------
###
async def async_setup_entry(hass:HomeAssistant, config_entry:ConfigEntry) -> bool:


    ### ----- Run Service Test
    async def handle_run_test(call:ServiceCall):
        try:
            scenario_file = call.data["scenario"]
            data_file = call.data["data"]

            await run_scenario(hass, scenario_file, data_file)
            
        except ServiceValidationError:
                raise  # let HA surface this cleanly to the caller

        except Exception as e:
            _LOG.error(traceback.format_exc())
            raise HomeAssistantError(f"Test scenario failed: {e}") from e
    ### ----- Run Service Test


    _LOG.info(f"__init__.async_setup_entry")

    await hass.async_add_executor_job(_start_debugger)

    # Register reload handler
    config_entry.async_on_unload(
        config_entry.add_update_listener(async_reload_entry)
    )
    
    ## Get our Integration's yaml_config, look up the (required) 'adapter_name'
    ## and Create the PoolAdapter
    ##

    yaml_config = hass.data.get(DOMAIN, {}).get("yaml_config")
    if yaml_config is not None:
        adapter_name = yaml_config.get(POOL_ADAPTER_CONFIG, {})
    
    if yaml_config is None or adapter_name is None:
        adapter_name = DEFAULT_POOL_ADAPTER
        _LOG.warning(f"configuration.yaml: pool_esp.adapter missing. Defaulting to [{adapter_name}]")

    ## Create PoolAdapter and Discover it's Configuration
    try:
        pool_adapter:PoolAdapter = await PoolAdapter.create(hass, adapter_name)
    except Exception as e:
        _LOG.error(f"Failed to create Pool Adapter[{adapter_name}]: {e}")

        ir.async_get_or_create(
            hass,
            DOMAIN,
            "missing_pool_adapter",
            is_fixable=False,
            severity=IssueSeverity.ERROR,
            translation_key="missing_pool_adapter",
        )
        # Don't fail setup entirely — just raise ConfigEntryNotReady
        # HA will retry setup automatically
        raise ConfigEntryNotReady(f"Failed to initialize integration") from e
    
    ###
    ### Create ESP Coordinator with the Pool Adapter's Configuration
    ###
    coordinator = ESPCoordinator(hass, config_entry, pool_adapter)
    await coordinator.async_setup()  # your custom init
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][config_entry.entry_id] = {
        ADDONS_COORDINATOR: coordinator
    }

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    ###
    ### Register the test scenario service
    ###
    _LOG.debug(f"...async_setup_entry: Register [run_test_scenario] -> [run_scenario]")
    hass.services.async_register(
        "screenlogic_esp",
        "run_test_scenario",
        handle_run_test
    )

    _LOG.debug(f"..async_setup_entry: Done")

    return True


###
### ----- Remove Config Entry Device
###
async def async_remove_config_entry_device(hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry) -> bool:#
    """Remove a config entry from a device."""
    _LOG.info(f"__init__.async_remove_config_entry_device; ConfigEntry: {config_entry}, DeviceEntry: {device_entry}")
    _LOG.info(f"__init__.async_remove_config_entry_device; DeviceEntry: {device_entry}")

    return True # Tell HA it's ok to delete this device

