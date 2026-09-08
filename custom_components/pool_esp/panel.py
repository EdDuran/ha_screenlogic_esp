# panel.py
import logging
from homeassistant.components import frontend
from homeassistant.components.frontend import async_register_built_in_panel, async_remove_panel

_LOG = logging.getLogger(__name__)

_ESP_RATES = "esp-rates"

def register_panel(hass):
    try:
        async_register_built_in_panel(
            hass,
            component_name    = "iframe",
            sidebar_title     = "Pool ESP Rates",
            sidebar_icon      = "mdi:chart-bell-curve",
            frontend_url_path = _ESP_RATES,
            config            = {"url": "/local/pool_esp/esp_rate_viewer.html"},
            require_admin     = False
        )
        _LOG.debug("Pool ESP Rates sidebar panel registered")
    except Exception:
        _LOG.debug("ESP Rates panel already registered")

def unregister_panel(hass):
    try:
        # Check if panel exists before removing
        panels = hass.data.get(frontend.DATA_PANELS, {})
        if _ESP_RATES in panels:
            async_remove_panel(hass, _ESP_RATES)
            _LOG.debug("Pool ESP Rates sidebar panel un-registered")
    except Exception:
        pass