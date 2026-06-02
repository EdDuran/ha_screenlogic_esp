from abc import ABC

from custom_components.pool_esp.util import start_debugger

from .const import DOMAIN, CONF_ELECTRIC_COST_PER_KWH, CONF_ELECTRIC_HEATER_KW, CONF_GAS_COST_PER_THERM, CONF_GAS_HEATER_BTU, CONF_HEATER_FUEL_TYPE, CONF_SHOW_PANEL, DEFAULT_POOL_ADAPTER, POOL_ADAPTER_CONFIG, POOL_ID, POOL_UNIQUE_ID
from .util import PoolAdapter
import voluptuous as vol
import logging

from dataclasses_json import config
from homeassistant.config_entries import ConfigEntryNotReady, ConfigFlow, OptionsFlow
from homeassistant.helpers import issue_registry as ir
from homeassistant.core import callback

from .panel import register_panel, unregister_panel


_LOG = logging.getLogger(__name__)

###
### ----- class ESPFlowMixin --------------------------------------------------
###

class ESPFlowMixin(ABC):
    """
    Shared form logic for both ConfigFlow and OptionsFlow.
    Subclasses implement _finish_flow() to handle the final step differently.
    """

    def _get_current_options(self):
        """Override in subclasses to provide current values for pre-filling."""
        return {}
    
    def _finish_flow(self, combined_data):
        """Override in subclasses — called when all options are collected."""
        raise NotImplementedError


    async def async_step_fuel_type(self, user_input=None):
        """Shared step — choose heater type and panel preference."""
        current = self._get_current_options()

        if user_input is not None:
            self._init_data = user_input
            fuel_type = user_input.get(CONF_HEATER_FUEL_TYPE, "gas")
            if fuel_type == "gas":
                return await self.async_step_gas()
            else:
                return await self.async_step_electric()

        return self.async_show_form(
            step_id     = "fuel_type",
            data_schema = vol.Schema({
                vol.Optional(
                    CONF_SHOW_PANEL,
                    default=current.get(CONF_SHOW_PANEL, False)
                ): bool,
                vol.Optional(
                    CONF_HEATER_FUEL_TYPE,
                    default=current.get(CONF_HEATER_FUEL_TYPE, "gas")
                ): vol.In(["gas", "heat_pump", "electric"]),
            }),
        )

    async def async_step_gas(self, user_input=None):
        """Shared step — gas heater settings."""
        current = self._get_current_options()

        if user_input is not None:
            combined = {**self._init_data, **user_input}
            return self._finish_flow(combined)

        return self.async_show_form(
            step_id     = "gas",
            data_schema = vol.Schema({
                vol.Optional(
                    CONF_GAS_HEATER_BTU,
                    default=current.get(CONF_GAS_HEATER_BTU, 400000)
                ): vol.Coerce(int),
                vol.Optional(
                    CONF_GAS_COST_PER_THERM,
                    default=current.get(CONF_GAS_COST_PER_THERM, 1.50)
                ): vol.Coerce(float),
            }),
        )

    async def async_step_electric(self, user_input=None):
        """Shared step — electric/heat pump settings."""
        current = self._get_current_options()

        if user_input is not None:
            combined = {**self._init_data, **user_input}
            return self._finish_flow(combined)

        return self.async_show_form(
            step_id     = "electric",
            data_schema = vol.Schema({
                vol.Optional(
                    CONF_ELECTRIC_HEATER_KW,
                    default=current.get(CONF_ELECTRIC_HEATER_KW, 5.25)
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_ELECTRIC_COST_PER_KWH,
                    default=current.get(CONF_ELECTRIC_COST_PER_KWH, 0.12)
                ): vol.Coerce(float),
            }),
        )





###
### ----- class ESPOptionsFlow ------------------------------------------------
###

class ESPOptionsFlow(ESPFlowMixin, OptionsFlow):

    def __init__(self, config_entry):
        self._config_entry = config_entry
        self._init_data    = {}

    def _get_current_options(self):
        """Pre-fill forms with existing values."""
        return self._config_entry.options

    async def async_step_init(self, user_input=None):
        """OptionsFlow entry point — go straight to shared fuel type step."""
        return await self.async_step_fuel_type(user_input)

    def _finish_flow(self, combined_data):
        """Save updated options."""
        if combined_data.get(CONF_SHOW_PANEL):
            register_panel(self.hass)
        else:
            unregister_panel(self.hass)

        return self.async_create_entry(title="", data=combined_data)



###
### ----- class ESPConfigFlow -------------------------------------------------
###

class ESPConfigFlow(ESPFlowMixin, ConfigFlow, domain=DOMAIN):

    VERSION = 1

    def __init__(self):
        self._device_id   = None
        self._device_name = None
        self._unique_id   = None
        self._init_data   = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ESPOptionsFlow(config_entry)


    async def async_step_user(self, user_input=None):
        """Called when user adds integration from UI."""
        #start_debugger()
        #breakpoint()
        
        _LOG.debug(f"async_setup_user user_input[{user_input}]")

        yaml_config = self.hass.data.get(DOMAIN, {}).get("yaml_config")
        
        #if yaml_config is not None:
        #    adapter_name = yaml_config.get(POOL_ADAPTER_CONFIG, {})

        adapter_name = (yaml_config.get(POOL_ADAPTER_CONFIG) if yaml_config else DEFAULT_POOL_ADAPTER) or DEFAULT_POOL_ADAPTER

        try:
            await self._discover_pool_adapter(adapter_name)
        except ConfigEntryNotReady:
            return self.async_abort(reason="no_pool_adapter")
        
        return await self.async_step_fuel_type()  # ← start shared options flow immediately after discovery



    def _finish_flow(self, combined_data):
        """Create the config entry with discovered device + collected options."""
        if combined_data.get(CONF_SHOW_PANEL):
            register_panel(self.hass)
        else:
            unregister_panel(self.hass)

        return self.async_create_entry(
            title   = self._device_name,
            data    = {
                "device_id": self._device_id,
                "unique_id": self._unique_id,
            },
            options = combined_data,
        )    


    async def _discover_pool_adapter(self, adapter_name):
        ## Create PoolAdapter and Discover it's Configuration
        try:
            pool_adapter:PoolAdapter = await PoolAdapter.create(self.hass, adapter_name)
            _LOG.debug(f"Discovered Pool Adapter [{adapter_name}] config: {pool_adapter}")
        except Exception as e:
            _LOG.error(f"Failed to discover Pool Adapter; {e}")

            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "missing_pool_adapter",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="missing_pool_adapter",
            )
            # Don't fail setup entirely — just raise ConfigEntryNotReady
            # HA will retry setup automatically
            raise ConfigEntryNotReady(f"Failed to configure Pool ESP integration") from e
    

        if pool_adapter is None:
            _LOG.warning(f"...No Pool Adapter Device Found")
            return self.async_abort(reason="no_screenlogic_device")

        #
        # Has this Device already been configured?
        #
        self._unique_id = pool_adapter.config.get(POOL_UNIQUE_ID)

        if self._unique_id is not None:
            _LOG.debug(f"...Unique ID from config: {self._unique_id}")
            # Assign a unique ID to the flow and abort the flow
            # if another flow with the same unique ID is in progress
            await self.async_set_unique_id(self._unique_id)

            # Abort the flow if a config entry with the same unique ID exists
            self._abort_if_unique_id_configured()

            self._device_id = pool_adapter.config.get(POOL_ID)
            self._device_name = f"{pool_adapter.name} ESP"

        else:
            raise ConfigEntryNotReady(f"Pool Adapter [{adapter_name}] did not provide a unique ID")
        


