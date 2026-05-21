
import logging
import time
from datetime import datetime, timedelta, timezone
from homeassistant.helpers.event import async_track_point_in_time
from .const import (
    DOMAIN,
    WATCHDOG_MAX_MINUTES,
    WATCHDOG_MIN_MINUTES,
    WATER_TEMP,
    CLIMATE_STATUS,
    HEATER_STATUS_HEATING_VALUE,
    WATCHDOG_THRESHOLD
)

_LOG = logging.getLogger(__name__)

class HeaterWatchdog:
    """
    Monitors heating progress against expected rate.
    Flags heater issues if temperature isn't rising as expected.
    """

    def __init__(self, coordinator, body_type):
        self._coordinator   = coordinator
        self._body_type     = body_type
        self._unsub         = None      # cancel timer
        self._baseline_ts   = None    # when we started watching
        self._baseline_temp = None    # water temp when we started
        self._expected_rate = None    # min/deg from ESP calculation

    def start(self, current_temp: float, rate: float, degrees_remaining: float):
        """
        Calculate check interval based on available rate data.
        """
        self.cancel()

        self._baseline_ts   = time.time()
        self._baseline_temp = current_temp
        self._expected_rate = rate

        if rate and degrees_remaining:
            # We have rate data — check at 25% of estimated total time
            # so we catch problems early but allow for startup delays
            total_estimated_min = rate * degrees_remaining
            check_min = max(
                WATCHDOG_MIN_MINUTES,           # never less than this
                min(
                    total_estimated_min * 0.25, # 25% of total estimate
                    WATCHDOG_MAX_MINUTES        # never more than this
                )
            )
        else:
            # No rate data yet — skip watchdog entirely
            _LOG.debug(f"Watchdog.start: [{self._body_type}] no rate data, skipping")
            return

        check_in = timedelta(minutes=check_min)
        self._unsub = async_track_point_in_time(
            self._coordinator.hass,
            self._check,
            datetime.now(timezone.utc) + check_in
        )

        _LOG.debug(
            f"Watchdog.start: [{self._body_type}] "
            f"baseline={current_temp:.1f}F rate={rate:.2f} "
            f"degrees_remaining={degrees_remaining:.1f} "
            f"check_in={check_min:.1f}min"
        )

    def cancel(self):
        """Cancel the current watchdog timer."""
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def _check(self, now):
        """Called when the watchdog timer fires."""
        self._unsub = None

        body_config   = self._coordinator.get_config(self._body_type)
        current_temp  = self._coordinator._get_current_value(body_config[WATER_TEMP])
        heater_is_on  = (
            self._coordinator._get_current_value(body_config[CLIMATE_STATUS]).lower() == HEATER_STATUS_HEATING_VALUE.lower()
        )

        # If heater turned off naturally, cancel watchdog
        if not heater_is_on:
            _LOG.debug(f"Watchdog._check: [{self._body_type}] heater off, cancelling")
            return

        elapsed_min   = (time.time() - self._baseline_ts) / 60.0
        expected_rise = elapsed_min / self._expected_rate  # degrees we should have gained
        actual_rise   = current_temp - self._baseline_temp

        _LOG.debug(
            f"Watchdog._check: [{self._body_type}] "
            f"elapsed={elapsed_min:.1f}min "
            f"expected_rise={expected_rise:.2f}F "
            f"actual_rise={actual_rise:.2f}F"
        )

        if actual_rise < (expected_rise * WATCHDOG_THRESHOLD):
            # Temperature not rising as expected — flag it
            await self._flag_heater_issue(current_temp, expected_rise, actual_rise)
        else:
            # Performing OK — reschedule for next check
            self.start(current_temp, self._expected_rate)

    async def _flag_heater_issue(self, current_temp, expected_rise, actual_rise):
        """Raise a persistent HA issue for the heater problem."""
        from homeassistant.helpers import issue_registry as ir

        _LOG.warning(
            f"Watchdog: [{self._body_type}] HEATER ISSUE DETECTED — "
            f"expected +{expected_rise:.1f}F got +{actual_rise:.1f}F"
        )

        ir.async_create_issue(
            self._coordinator.hass,
            DOMAIN,
            f"heater_performance_{self._body_type}",
            is_fixable    = True,
            severity      = ir.IssueSeverity.WARNING,
            translation_key = "heater_performance",
            translation_placeholders = {
                "body_type":     self._body_type,
                "expected_rise": f"{expected_rise:.1f}",
                "actual_rise":   f"{actual_rise:.1f}",
                "current_temp":  f"{current_temp:.1f}",
            }
        )