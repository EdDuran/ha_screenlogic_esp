# diagnostics.py

import logging
from homeassistant.components.diagnostics import async_redact_data
from .const import *

_LOGGER = logging.getLogger(__name__)

_DEBUG = True
_TRACE = True
_WARNING = True

def _warning(msg: str):
    if (_WARNING):
        _LOGGER.warning(msg)

def _debug(msg: str):
    if (_DEBUG):
        _LOGGER.debug(msg)

def _trace(msg: str):
    if (_TRACE):
        _LOGGER.info(msg)

REDACT_FIELDS = set()  # nothing sensitive to redact in ESP

async def async_get_config_entry_diagnostics(hass, entry):
    """Return diagnostics for the ESP config entry."""
    _trace(f"async_get_config_entry_diagnostics")
    _debug(f"...hass.data: {hass.data}")

    coordinator: ESPCoordinator = hass.data[DOMAIN][entry.entry_id]
    
    diag = {
        "integration": {
            "version":     entry.version,
            "domain":      DOMAIN,
            "device_name": coordinator.device.name,
        },
        "configuration": {
            "history_days":          HISTORY_DAYS,
            "air_temp_bin_width":    AIR_TEMP_BIN_WIDTH,
            "min_samples":           MIN_SAMPLES,
            "min_interval_minutes":  MIN_INTERVAL_MINUTES,
            "min_degrees_gained":    MIN_DEGREES_GAINED,
            "min_rate_deg_per_hour": MIN_RATE_DEG_PER_HOUR,
            "sensing_settle_secs":   SENSING_SETTLE_SECS,
        },
        "bodies": {}
    }

    for body_type in BODY_TYPES:
        ctx   = coordinator.get_context(body_type)
        table = coordinator.get_rate_table(body_type)

        # Format rate table
        formatted_table = {}
        if table:
            for bin_key in sorted(table.keys()):
                samples  = table[bin_key]
                avg      = sum(samples) / len(samples)
                sorted_s = sorted(samples)
                n        = len(sorted_s)
                mid      = n // 2
                median   = (sorted_s[mid] if n % 2 
                            else (sorted_s[mid-1] + sorted_s[mid]) / 2.0)
                sd       = (sum([(x - avg) ** 2 for x in samples]) / n) ** 0.5
                formatted_table[f"{bin_key}-{bin_key+AIR_TEMP_BIN_WIDTH}F"] = {
                    "median_min_per_deg": round(median, 2),
                    "mean_min_per_deg":   round(avg, 2),
                    "sd":                 round(sd, 2),
                    "n_samples":          n,
                    "raw_samples":        [round(s, 3) for s in sorted_s],
                }

        diag["bodies"][body_type] = {
            "state":        ctx.machine_state if ctx else None,
            "status":       ctx.status if ctx else None,
            "water_temp":   ctx.water_temp if ctx else None,
            "target_temp":  ctx.target_temp if ctx else None,
            "air_temp":     ctx.air_temp if ctx else None,
            "rate_table":   formatted_table,
            "total_samples": sum(len(v) for v in table.values()) if table else 0,
        }

    return async_redact_data(diag, REDACT_FIELDS)
