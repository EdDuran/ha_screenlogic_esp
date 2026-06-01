from .const import DOMAIN, CONF_ELECTRIC_COST_PER_KWH, CONF_ELECTRIC_HEATER_KW, CONF_GAS_COST_PER_THERM, CONF_GAS_HEATER_BTU, CONF_HEATER_FUEL_TYPE, CONF_SHOW_PANEL, DEFAULT_POOL_ADAPTER, POOL_ADAPTER_CONFIG, POOL_ID, POOL_UNIQUE_ID
from .util import PoolAdapter
import voluptuous as vol
import logging

from dataclasses_json import config
from homeassistant.config_entries import ConfigEntryNotReady, ConfigFlow, OptionsFlow
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import device_registry as dr
from homeassistant.core import callback

from .panel import register_panel, unregister_panel


_LOG = logging.getLogger(__name__)

async def _find_screenlogic_device(hass):
    """
    Find the Pentair ScreenLogic Device
    """
    dev_reg = dr.async_get(hass)
    for device in dev_reg.devices.values():
        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry and entry.domain == "screenlogic":
                return device
    return None

class ESPOptionsFlow(OptionsFlow):

    def __init__(self):
        self._init_data = {}

    async def async_step_init(self, user_input=None):
        try:
            if user_input is not None:
                # Save init options first
                self._init_data = user_input  # ← store for later

                fuel_type = user_input.get(CONF_HEATER_FUEL_TYPE)
                if fuel_type == "gas":
                    return await self.async_step_gas()       # ← no user_input = show form
                elif fuel_type in ("heat_pump", "electric"):
                    return await self.async_step_electric()
                        
                if user_input.get(CONF_SHOW_PANEL):
                    register_panel(self.hass)
                else:
                    unregister_panel(self.hass)
                    
                return self.async_create_entry(title="", data=user_input)
            # end if user_input is not None

            return self.async_show_form(
                step_id   = "init",
                data_schema = vol.Schema({
                    vol.Optional(
                        CONF_SHOW_PANEL,
                        default = self.config_entry.options.get(CONF_SHOW_PANEL, False)
                    ): bool,
                    
                    vol.Optional(
                        CONF_HEATER_FUEL_TYPE,
                        default=self.config_entry.options.get(CONF_HEATER_FUEL_TYPE, "gas")
                    ): vol.In(["gas", "heat_pump", "electric"])
                }),
                description_placeholders = {
                    "info": "Enable to show ESP Rate Table viewer in the HA sidebar"
                }
            )
        except Exception as e:
            _LOG.error(f"Error in async_step_init: {e}")
            raise ConfigEntryNotReady("Failed to save options") from e  
    
    async def async_step_gas(self, user_input=None):
        if user_input is not None:
            # Merge init data with gas-specific data
            combined = {**self._init_data, **user_input}

            if combined.get(CONF_SHOW_PANEL):
                register_panel(self.hass)
            else:
                unregister_panel(self.hass)

            return self.async_create_entry(title="", data=combined)

        return self.async_show_form(
            step_id="gas",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_GAS_HEATER_BTU,
                    default=self.config_entry.options.get(CONF_GAS_HEATER_BTU, 400000)
                ): vol.Coerce(int),
                vol.Optional(
                    CONF_GAS_COST_PER_THERM,
                    default=self.config_entry.options.get(CONF_GAS_COST_PER_THERM, 1.50)
                ): vol.Coerce(float)
            })
        )

    async def async_step_electric(self, user_input=None):
        if user_input is not None:
            combined = {**self._init_data, **user_input}

            if combined.get(CONF_SHOW_PANEL):
                register_panel(self.hass)
            else:
                unregister_panel(self.hass)

            return self.async_create_entry(title="", data=combined)

        return self.async_show_form(
            step_id="electric",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_ELECTRIC_COST_PER_KWH,
                    default=self.config_entry.options.get(CONF_ELECTRIC_COST_PER_KWH, 0.12)
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_ELECTRIC_HEATER_KW,
                    default=self.config_entry.options.get(CONF_ELECTRIC_HEATER_KW, 5)
                ): vol.Coerce(float)
            })
        )

class ESPConfigFlow(ConfigFlow, domain=DOMAIN):

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ESPOptionsFlow()

    async def async_step_user(self, info=None):
        """Called when user adds integration from UI."""
        _LOG.debug(f"async_setup_user Info:[{info}]")
        _LOG.debug(f"async_setup_user config:[{type(config)} / {config}]")

        yaml_config = self.hass.data.get(DOMAIN, {}).get("yaml_config")
        if yaml_config is not None:
            adapter_name = yaml_config.get(POOL_ADAPTER_CONFIG, {})
        
        if yaml_config is None or adapter_name is None:
            adapter_name = DEFAULT_POOL_ADAPTER
            _LOG.warning(f"configuration.yaml: pool_esp.adapter missing. Defaulting to [{adapter_name}]")

        ## Create PoolAdapter and Discover it's Configuration
        try:
            pool_adapter:PoolAdapter = await PoolAdapter.create(self.hass, adapter_name)
            _LOG.debug(f"Pool Adapter [{adapter_name}] created successfully with config: {pool_adapter}")
        except Exception as e:
            _LOG.error(f"Failed to create Pool Adapter[{adapter_name}]: {e}")

            ir.async_get_or_create(
                self.hass,
                DOMAIN,
                "missing_pool_adapter",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="missing_pool_adapter",
            )
            # Don't fail setup entirely — just raise ConfigEntryNotReady
            # HA will retry setup automatically
            raise ConfigEntryNotReady(f"Failed to initialize integration") from e
    

        if pool_adapter is None:
            _LOG.warning(f"...No ScreenLogic Device")
            return self.async_abort(reason="no_screenlogic_device")

        #
        # Has this Device MAC already been configured?
        #
        unique_id = pool_adapter.config.get(POOL_UNIQUE_ID)

        if unique_id is not None:
            _LOG.debug(f"...Unique ID from config: {unique_id}")
            # Assign a unique ID to the flow and abort the flow
            # if another flow with the same unique ID is in progress
            await self.async_set_unique_id(unique_id)

            # Abort the flow if a config entry with the same unique ID exists
            self._abort_if_unique_id_configured()

            #
            # Create Device Entry
            #
            device_id = pool_adapter.config.get(POOL_ID)
            device_name = f"{pool_adapter.name} ESP"
            _LOG.debug(f"..Creating Device: {device_name}")
            # No user input needed — fully automatic
            return self.async_create_entry(
                title=device_name,
                data={
                    "device_id": device_id,
                    "unique_id": unique_id
                }
            )
        else:
            _LOG.debug(f"Connections is empty")
            return False


