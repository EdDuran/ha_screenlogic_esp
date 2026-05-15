# diagnostics.py

import logging
from coordinator import ESPCoordinator
from homeassistant.components.diagnostics import async_redact_data
from persistence import Persistence
from .const import *

_LOG = logging.getLogger(__name__)


REDACT_FIELDS = set()  # nothing sensitive to redact in ESP

async def async_get_config_entry_diagnostics(hass, entry):
    """Return diagnostics for the ESP config entry."""
    _LOG.info(f"async_get_config_entry_diagnostics")
    _LOG.debug(f"...hass.data: {hass.data}")

    coordinator  = hass.data[DOMAIN][entry.entry_id][ADDONS_COORDINATOR]
    diagnostics  = {}

    for body_type in BODY_TYPES:
        persistence = Persistence(hass, body_type)
        await persistence.async_load()
        diagnostics[body_type] = persistence.get_diagnostics()

    return diagnostics
