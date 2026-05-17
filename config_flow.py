import voluptuous as vol
import logging

from dataclasses_json import config
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigEntryNotReady, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant import config_entries

from .panel import register_panel, unregister_panel
from .const import DOMAIN, CONF_SHOW_PANEL


_LOGGER = logging.getLogger(__name__)

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

class ESPOptionsFlow(config_entries.OptionsFlow):

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            if user_input.get(CONF_SHOW_PANEL):
                register_panel(self.hass)
            else:
                unregister_panel(self.hass)
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id   = "init",
            data_schema = vol.Schema({
                vol.Optional(
                    CONF_SHOW_PANEL,
                    default = self.config_entry.options.get(CONF_SHOW_PANEL, False)
                ): bool
            }),
            description_placeholders = {
                "info": "Enable to show ESP Rate Table viewer in the HA sidebar"
            }
        )


class ESPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):


    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ESPOptionsFlow()
    

    async def async_step_user(self, info=None):
        """Called when user adds integration from UI."""
        _LOGGER.info(f"CONFIG_FLOW: async_setup_user Info:[{info}]")

        adapter_name = config.get("adapter", "ScreenlogicAdapter")

        #
        # Auto-discover ScreenLogic device
        #   None found: Abort
        #
        device = await _find_screenlogic_device(self.hass)

        if device is None:
            _LOGGER.info(f"...No ScreenLogic Device")
            return self.async_abort(reason="no_screenlogic_device")

        #
        # Has this Device MAC already been configured?
        #
        connections = device.connections
        if connections:
            for e in connections:
                key = e[0]
                if (key == "mac"):
                    mac = e[1]
                    _LOGGER.info(f"...MAC is {mac}")
                    # Assign a unique ID to the flow and abort the flow
                    # if another flow with the same unique ID is in progress
                    await self.async_set_unique_id(mac)

                    # Abort the flow if a config entry with the same unique ID exists
                    self._abort_if_unique_id_configured()

                    #
                    # Create Device Entry
                    #
                    device_name = f"{device.name} ESP"
                    _LOGGER.info(f"..Creating Device: {device_name}")
                    # No user input needed — fully automatic
                    return self.async_create_entry(
                        title=device_name,
                        data={
                            "device_id": device.id,
                            "unique_id": mac
                        }
                    )
                    break
                # endif key is 'mac'
            # end for each connection
        else:
            _LOGGER.info(f"Connections is empty")
            return False


