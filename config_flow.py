from dataclasses_json import config
from homeassistant import config_entries
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from .const import DOMAIN
import logging

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

class ESPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

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


