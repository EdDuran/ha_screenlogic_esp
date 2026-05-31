from __future__ import annotations
from homeassistant.config_entries import ConfigEntry, ConfigEntryNotReady, callback
from homeassistant.components.homeassistant import IssueSeverity
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import device_registry as dr
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.frontend import async_register_built_in_panel, async_remove_panel


from .panel import register_panel, unregister_panel
from .persistence import Persistence
from .coordinator import ESPCoordinator
from .const import (
    DOMAIN,
    PLATFORMS,
    ADDONS_COORDINATOR,
    POOL_ADAPTER_CONFIG,
    DEFAULT_POOL_ADAPTER
)
from .util import *
from .test_runner import *
import logging
import debugpy
import os
import shutil

_LOG = logging.getLogger(__name__)

class ESPRatesView(HomeAssistantView):
    """
    View to get/set ESP rates data. This is used by the panel to persist rates data,
    which is not stored in the Coordinator and thus not persisted across restarts by default.
    """
    url = "/api/pool_esp/rates"
    name = "api:pool_esp:rates"
    requires_auth = True

    async def get(self, request):
        hass = request.app["hass"]

        # Load from any body type — top level data is shared
        p = Persistence(hass, BODY_TYPES[0])
        await p.async_load()

        result = {
            "pool_type": p._data.get("pool_type", "Unknown"),
            "bodies":    {}
        }

        for body_type in BODY_TYPES:
            p = Persistence(hass, body_type)
            await p.async_load()
            result["bodies"][body_type] = p.body_data
        
        return self.json(result)

    async def post(self, request):
        hass = request.app["hass"]
        data = await request.json()
        bodies = data.get("bodies", {})

        for body_type, body_data in bodies.items():
            p = Persistence(hass, body_type)
            await p.async_load()
            p._data["pool_type"] = data.get("pool_type", "Unknown") # Ensure pool_type is saved at top level for easy access
            p._data[body_type] = body_data
            await p.async_save()
            
        return self.json({"status": "ok"})


def _start_debugger():

    _LOG.debug(f"__init__._start_debugger: Starting debugger...HA_DEBUG=[{os.getenv('HA_DEBUG')}]")

    if not os.getenv("HA_DEBUG"):
        _LOG.debug("HA_DEBUG is not set...Debugging will not be available")
        return

    try:
        debugpy.listen(("0.0.0.0",5678))
        _LOG.debug("__init__.Debugger listening on 5678")
    except RuntimeError:
        _LOG.warning("__init__.Debugger already active")

    if not debugpy.is_client_connected():
        _LOG.warning("Waiting for debugger attach...")
        debugpy.wait_for_client()

        debugpy.breakpoint()

    _LOG.debug("__init__.Debugger attached")

###
### ----- Reload Integration ---------------------------------------------------
###
async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry."""
    _LOG.debug(f"__init__.async_reload_entry")
    await hass.config_entries.async_reload(entry.entry_id)

###
### ----- Unload Integration ---------------------------------------------------
###
async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle unload — must be implemented for reload to work."""
    _LOG.debug(f"__init__.async_unload_entry")

    # Always clean up panel on unload
    unregister_panel(hass)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Pop add-on data
    hass.data.pop(ADDONS_COORDINATOR, None)

    return unload_ok
    


###
### ----- Setup ----------------------------------------------------------------
###
def setup(hass: HomeAssistant, config: ConfigType) -> bool:

    _LOG.debug(f"__init__.setup")

    ## Save ESP configuration
    hass.data.setdefault(DOMAIN, {})
    if DOMAIN in config:
        hass.data[DOMAIN]["yaml_config"] = config[DOMAIN]


    # Return boolean to indicate that initialization was successful.
    return True

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    _LOG.debug(f"__init__.async_setup")

    await _async_copy_www_assets(hass)

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


    _LOG.debug(f"__init__.async_setup_entry")

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
        _LOG.debug(f"configuration.yaml: pool_esp.adapter missing. Defaulting to [{adapter_name}]")

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
        "pool_esp",
        "run_test_scenario",
        handle_run_test
    )

    ###
    ### Register panel if option enabled
    ###
    if config_entry.options.get(CONF_SHOW_PANEL, False):
        register_panel(hass)

    ###
    ### Register the ESP rates view
    ###
    hass.http.register_view(ESPRatesView())

    _LOG.debug(f"..async_setup_entry: Done")

    return True

async def _async_copy_www_assets(hass:HomeAssistant):
    """Copy www files to /config/www/pool_esp on every startup."""
    
    source_dir = hass.config.path(
        "custom_components", "pool_esp", "www"
    )
    dest_dir = hass.config.path("www", "pool_esp")

    def _copy():
        try:
            _LOG.debug(f"Copying assets from {source_dir} to {dest_dir}...")

            os.makedirs(dest_dir, exist_ok=True)
            if os.path.exists(source_dir):
                for filename in os.listdir(source_dir):
                    src  = os.path.join(source_dir, filename)
                    dest = os.path.join(dest_dir, filename)
                    shutil.copy2(src, dest)
                    _LOG.debug(f"Copied {filename} to {dest}")
            else:
                _LOG.warning(f"www source not found: {source_dir}")
        except Exception as e:
            _LOG.error(f"Failed to copy www assets: {e}")

    await hass.async_add_executor_job(_copy)

###
### ----- Remove Config Entry Device
###
async def async_remove_config_entry_device(hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry) -> bool:#
    """Remove a config entry from a device."""
    _LOG.debug(f"__init__.async_remove_config_entry_device; ConfigEntry: {config_entry}, DeviceEntry: {device_entry}")
    _LOG.debug(f"__init__.async_remove_config_entry_device; DeviceEntry: {device_entry}")

    return True # Tell HA it's ok to delete this device

