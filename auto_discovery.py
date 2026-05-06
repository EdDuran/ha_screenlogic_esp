from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from .const import *
import logging

_LOGGER = logging.getLogger(__name__)

def auto_discovery(hass) -> dict:
    """
    Discovery Home Assistant ScreenLogic's Pool and Spa Entities.
    Returns a Map of BodyTypes -> a Map of EntityTypes -> Entity Ids
    """

    #_LOGGER.info(f"auto_discovery: Start")
    dev_reg = dr.async_get(hass)

    sl_device = None
    for device in dev_reg.devices.values():
        if not sl_device:
            # Match on manufacturer since identifiers is empty
            manufacturer = device.manufacturer
            config_entries = device.config_entries

            if manufacturer and manufacturer.lower() == "pentair":
                # Look up the config entries to find the integration domain
                for entry_id in device.config_entries:
                    entry = hass.config_entries.async_get_entry(entry_id)
                    if entry:
                        domain = entry.domain
                        if (domain and domain.lower() == "screenlogic"):
                            sl_device = device
                            #log.info("config_entry %s: domain=%s title=%s",
                            #    entry_id, entry.domain, entry.title)
                            break
                    # end if entry
                # end for entry_id
            # end if "pentair"
        else:
            break
    # end for device

    if sl_device is None:
        _LOGGER.warning(f"AutoDiscovery: No ScreenLogic Device found")
        raise Exception("No ScreenLogic device found")

    #_LOGGER.debug(f"...Device: {sl_device}")

    name = sl_device.name
    prefix = name.lower().replace(":", "").replace("-", "_").replace(" ", "_").replace("__", "_")
    id = sl_device.id
    model = sl_device.model

    config = { }

    config[CONFIG_SCREENLOGIC_NAME] = name
    config[CONFIG_SCREENLOGIC_MODEL] = model
    config[CONFIG_SCREENLOGIC_PREFIX] = prefix
    config[CONFIG_SCREENLOGIC_ID] = id

    _LOGGER.info(f"AutoDiscovery: Discovered ScreenLogic Device: {prefix}")

    return config