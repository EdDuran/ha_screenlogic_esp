# panel.py
import logging
from homeassistant.components.frontend import async_register_built_in_panel, async_remove_panel

_LOG = logging.getLogger(__name__)

def register_panel(hass):
    try:
        async_register_built_in_panel(
            hass,
            component_name    = "iframe",
            sidebar_title     = "Pool ESP Rates",
            sidebar_icon      = "mdi:chart-bell-curve",
            frontend_url_path = "esp-rates",
            config            = {"url": "/local/ha_screenlogic_esp/esp_rate_viewer.html"},
            require_admin     = False
        )
        _LOG.debug("Pool ESP Rates sidebar panel registered")
    except Exception:
        _LOG.warning("ESP Rates panel already registered")

def unregister_panel(hass):
    try:
        async_remove_panel(hass, "esp-rates")
        _LOG.debug("Pool ESP Rates sidebar panel un-registered")
    except Exception:
        pass