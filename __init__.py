from __future__ import annotations
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import device_registry as dr
from .coordinator import ESPCoordinator
from .const import (
    DOMAIN,
    PLATFORMS,
    ADDONS_COORDINATOR
)
from .auto_discovery import auto_discovery
from .util import *
from .test_runner import *
import logging
import os

_LOG = logging.getLogger(__name__)

import debugpy
try:
    if not debugpy.is_client_connected():
        debugpy.listen(("0.0.0.0", 5678))
        _LOG.warning("ESP debugpy listening on :5678")
except Exception:
    pass

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
    # Return boolean to indicate that initialization was successful.
    return True


###
### ----- Setup Integration ----------------------------------------------------
###
async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:

    async def handle_run_test(call):
        scenario = call.data["scenario"]
        await run_scenario(hass, scenario)


    _LOG.info(f"__init__.async_setup_entry")

    # Register reload handler
    config_entry.async_on_unload(
        config_entry.add_update_listener(async_reload_entry)
    )
    
    # Discover ScreenLogic Device
    config = auto_discovery(hass)
    _LOG.debug(f"...async_setup_entry; Auto Discovery[{config}]")
    
    if config is None:
        ir.async_get_or_create(
            hass,
            DOMAIN,
            "missing_screenlogic",
            is_fixable=False,
            severity=IssueSeverity.ERROR,
            translation_key="missing_screenlogic",
        )
        # Don't fail setup entirely — just raise ConfigEntryNotReady
        # HA will retry setup automatically
        raise ConfigEntryNotReady("ScreenLogic integration not found")
    
    ###
    ### Create ESP Coordinator and add it to hass
    ###
    coordinator = ESPCoordinator(hass, config_entry, config)
    await coordinator.async_setup()  # your custom init
    await coordinator.async_config_entry_first_refresh()
    hass.data[ADDONS_COORDINATOR] = coordinator

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)


    _LOG.debug(f"...async_setup_entry: Register [run_test_scenario] -> [handle_run_test]")
    hass.services.async_register(
        "screenlogic_esp",
        "run_test_scenario",
        handle_run_test
    )

    _LOG.debug(f"..async_setup_entry: Done")

    return True



### Remove Config Entry Device
async def async_remove_config_entry_device(hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry) -> bool:#
    """Remove a config entry from a device."""
    _LOG.info(f"__init__.async_remove_config_entry_device; ConfigEntry: {config_entry}, DeviceEntry: {device_entry}")
    _LOG.info(f"__init__.async_remove_config_entry_device; DeviceEntry: {device_entry}")

    return True # Tell HA it's ok to delete this device

