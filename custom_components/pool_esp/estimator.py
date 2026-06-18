import logging
import traceback
import bisect
import json
import time
from datetime import datetime, timedelta, timezone

from .sensor import HeaterCostSensor, HeaterRuntimeSensor

from .const import STATUS_LEARNING

from .persistence import Persistence
from .coordinator import *

_LOG = logging.getLogger(__name__)

###
### ----- Class LiveHistoryAdapter --------------------------------------------------
###

class LiveHistoryAdapter(HistoryAdapter):
    def __init__(self, hass, body_type, coordinator:ESPCoordinator, config_entities):
        self._hass = hass
        self._body_type = body_type
        self._coordinator = coordinator
        self._config_entities = config_entities
        self._starttime = None
        self._endtime = None
        self._context = coordinator.get_context(body_type)
        _LOG.debug(f"LiveHistoryAdapter: initialized")

    async def get_history(self):
        self._history, self._starttime, self._endtime = await self._fetch_all_history()
        return self._history, self._starttime, self._endtime

    def get_current_value(self, body_config, screenlogic_entity:str):
        """
        Get the current value of a specific attribute from the context.
        """
        return self._context.get(screenlogic_entity.lower())
    
    @property
    def now(self) -> float:
        return time.time()
    
    @property
    def starttime(self) -> str:
        """
        Get the history starttime.
        """
        return self._starttime
    
    @property
    def endtime(self) -> str:
        """
        Get the history endtime.
        """
        return self._endtime
    
    @property
    def body_type(self) -> str:
        """
        Get the body type.
        """
        return self._body_type


    async def _fetch_all_history(self):
        from homeassistant.components.recorder import get_instance

        end: datetime   = datetime.now(timezone.utc)
        start: datetime = end - timedelta(days=HISTORY_DAYS)
        instance = get_instance(self._hass)

        history = await instance.async_add_executor_job(
            self._fetch_history, self._hass, list(self._config_entities), start, end
        )

        return history, start, end
    

    @staticmethod
    def _fetch_history(hass, entity_ids, start_dt, end_dt) -> dict[str, list]:
        """Single recorder call for all entity_ids."""
        import homeassistant.components.recorder.history as rec_history

        _LOG.debug(f"fetch_history entities[{entity_ids}] start[{start_dt}] end[{end_dt}]")

        result = rec_history.get_significant_states(
            hass,
            start_dt,
            end_dt,
            entity_ids,
            None,   # filters
            True,   # include_start_time_state
            False,  # significant_changes_only
            False,  # minimal_response
            False,  # no_attributes
        )

        return result if result else {s: [] for s in entity_ids}

###
### ----- Class ESPEstimator --------------------------------------------------
###

class ESPEstimator:
    """
    Estimates time-to-setpoint (ESP) for a pool/spa body of water.

    Fetches historical sensor data from Home Assistant's recorder,
    builds a heating-rate table keyed by air-temperature bin, and
    projects how long it will take the water to reach the target temp.
    """

    def __init__(self, coordinator: ESPCoordinator, body_type: str):
        self._coordinator: ESPCoordinator = coordinator
        self._body_type: str = body_type

        self._body_config:Config       = self._coordinator.get_config(body_type)
        self._config_entities:set[str] = self._coordinator.get_config_entities(body_type)
        self._hass:HomeAssistant       = self._coordinator.hass

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def calculate_wrapper(self, context) -> ESP:
        """
        Async wrapper around calculate_esp.
        Returns: (days, hours, minutes, esp_seconds, formatted_esp)
        """
        try:
            if context.history_adapter is None:
                context.history_adapter = LiveHistoryAdapter(self._hass, context.body_type, self._coordinator, self._config_entities)

            esp:ESP = await self.calculate(context.export,context.history_adapter)

            context.status = esp.status
            context.confidence_pct = esp.confidence_pct
            context.seconds = esp.seconds

            return esp

        except Exception as e:
            _LOG.error(traceback.format_exc())
            _LOG.error(f"calculate_wrapper: Failed to Calculate Pool ESP; {e}")
            raise e

    async def calculate(self, export:bool, history_adapter:HistoryAdapter) -> ESP:
        """
        Fetch history, build a min/deg rate table keyed by air-temp bin,
        and estimate seconds until the water reaches the target temperature.

        Works when the heater is OFF (hypothetical estimate).

        Returns:
            esp (int | None): total seconds to setpoint, or None if unavailable.
        """
        from homeassistant.components.recorder import get_instance

        body_config = self._coordinator.get_config(self._body_type)
        cost_per_hour = self._coordinator.get_option(CONF_GAS_COST_PER_THERM, 0.0) if self._coordinator.get_option(CONF_HEATER_FUEL_TYPE, "gas") == "gas" else self._coordinator.get_option(CONF_ELECTRIC_COST_PER_KWH, 0.0)
        _LOG.debug(f"calculate: [{self._body_type}]")

        instance  = get_instance(self._hass)
        execution_starttime = time.time()

        # --- Fetch history ---------------------------------------------------
        try:
            history, start, end = await history_adapter.get_history()
            now_ts= history_adapter.now
            _LOG.debug(f"...HistoryAdapter; Now: [{now_ts} / {local_time(now_ts)}]")

            ###
            ### Export history data
            ###
            if export:    # Context Export Flag set?
                await self._export_history_data(history, body_config, start, end)

            water_history  = self._get_history_by_entity(WATER_TEMP,     self._body_type, history, body_config)
            air_history    = self._get_history_by_entity(AIR_TEMP,       self._body_type, history, body_config)
            heat_history   = self._get_history_by_entity(CLIMATE_STATUS, self._body_type, history, body_config)
            target_history = self._get_history_by_entity(TARGET_TEMP,    self._body_type, history, body_config)

            heat_states = self._parse_state_values(heat_history, CLIMATE_STATUS, body_config)
            heating_intervals   = self._extract_heater_on_intervals(heat_states, now_ts)

            _LOG.debug(f"...History: Air[{len(air_history)}] Water[{len(water_history)}] Target[{len(target_history)}] HeatStatus[{len(heat_history)}] HeatingIntervals[{len(heating_intervals)}]")
        except Exception as e:
            _LOG.error(f"calculate: Failed to retrieve History; {e}")
            _LOG.error(traceback.format_exc())
            raise ESPException("calculate: Failed to retrieve History") from e

        if not water_history or not air_history or not heat_history:
            _LOG.warning(f"...RETURN No Data: Air[{len(air_history)}] Water[{len(water_history)}] HeatStatus[{len(heat_history)}]")
            return ESP(0, 0, STATUS_LEARNING) # No ESP, No Confidence

        water_temps = self._parse_state_values(water_history, WATER_TEMP, body_config)
        air_temps   = self._parse_state_values(air_history, AIR_TEMP, body_config)

        MIN_RATE_DEG_PER_HOUR = 0.5  # must be rising at least 0.1°F/hour to count

        ###
        ### ----- Build rate table --------------------------------------------
        ###

        try:
            result = await instance.async_add_executor_job(
                self._build_rate_table,
                heating_intervals, water_temps, air_temps,
                AIR_TEMP_BIN_WIDTH,
                MIN_INTERVAL_MINUTES,
                MIN_DEGREES_GAINED,
                MIN_RATE_DEG_PER_HOUR,
            )

            table = result["table"]
            skipped_short    = result["skipped_short"]
            skipped_no_rise  = result["skipped_no_rise"]
            skipped_slow     = result["skipped_slow"]
            skipped_no_water = result["skipped_no_water"]
            skipped_no_air   = result["skipped_no_air"]

            _LOG.debug(
                f"...calculate: rate table built [{self._body_type}] — skipped(short={skipped_short}, no_rise={skipped_no_rise} slow={skipped_slow} no_water={skipped_no_water} no_air={skipped_no_air})"
            )

            if not table:
                _LOG.debug(f"[{self._body_type}] Recorder has No usable heating intervals")

            ###
            ### ----- Merge new rate table with persisted historical data -----
            ###       and save back to disk
            ###
            used  = result["used"]
            persistence:Persistence = self._coordinator.get_persistence()
            if export: ## export means running live data, so merge results
                await persistence.merge_and_save(self._body_type, table, heating_intervals, used, cost_per_hour)

            ###
            ###  After merging, reload the full rate
            ###
            table = persistence.get_rate_table(self._body_type)

        except Exception as e:
            _LOG.error(traceback.format_exc())
            raise ESPException("calculate: Failed to build rate table") from e
        
        # --- Log rate table --------------------------------------------------
        for bin_key in sorted(table.keys()):
            samples = table[bin_key]
            avg     = sum(samples) / len(samples)
            sd      = (sum((x - avg) ** 2 for x in samples) / len(samples)) ** 0.5
            sorted_s = sorted(samples)
            n        = len(sorted_s)
            mid      = n // 2
            median   = sorted_s[mid] if n % 2 else (sorted_s[mid - 1] + sorted_s[mid]) / 2.0
            _LOG.debug(
                f"Rate Table:[{self._body_type}] [{bin_key:3d}F -{(bin_key + AIR_TEMP_BIN_WIDTH):3d}F] -> median[{median:5.2f}] avg[{avg:5.2f} min/deg] n[{len(samples):2d}] sd[{sd:.2f}]"
            )

        # --- Read current sensor values --------------------------------------
        try:
            if body_config:
                current_water  = history_adapter.get_current_value(body_config, WATER_TEMP)
                current_air    = history_adapter.get_current_value(body_config, AIR_TEMP)
                current_target = history_adapter.get_current_value(body_config, TARGET_TEMP)
                heater_status  = history_adapter.get_current_value(body_config, CLIMATE_STATUS)
                if (current_water is None or current_air is None or current_target is None or heater_status is None):
                    _LOG.error(f"calculate: [{self._body_type}] Failed to get all current sensor values")
                    raise ESPException(f"Failed to get [{self._body_type}] current sensor values")
                
                heater_is_on   = heater_status.lower() == HEATER_STATUS_HEATING_VALUE.lower()
            else:
                _LOG.error(f"calculate: Failed to get BodyConfig[{self._body_type}]")
        except (ValueError, TypeError) as e:
            detail = f"calculate: {self._body_type} cannot read current sensors; {e}"
            _LOG.error(f"calculate: {self._body_type} {detail}")
            raise ESPException(detail) from e

        _LOG.debug(
            f"...calculate: [{self._body_type}] "
            f"WaterTemp[{current_water:.1f}F] TargetTemp[{current_target:.1f}F] "
            f"AirTemp=[{current_air:.1f}F] IsHeaterActive[{heater_is_on}]"
        )

        # --- Weighted rate lookup --------------------------------------------
        try:
            target_bin = int(current_air // AIR_TEMP_BIN_WIDTH) * AIR_TEMP_BIN_WIDTH
            rate, confidence = await instance.async_add_executor_job(
                self._weighted_rate, table, target_bin, AIR_TEMP_BIN_WIDTH,
            )
        except Exception:
            raise ESPException("Failed to calculate weighted rate and confidence")

        if rate is None:
            _LOG.warning(f"calculate: [{self._body_type}] rate is None")
            return ESP(0, 0, STATUS_LEARNING) # No ESP, No Confidence

        # --- ESP calculation -------------------------------------------------
        UNCERTAINTY_GAIN  = 0.5
        degrees_remaining = current_target - current_water
        if degrees_remaining <= 0:
            _LOG.debug(f"calculate: Water is already at or above target")
            return ESP(0, 0, STATUS_READY) # Water is already at or above target
        
        if degrees_remaining == 0 and heater_is_on:
            degrees_remaining = 1

        base_esp          = rate * degrees_remaining
        uncertainty_factor = 1.0 + (1.0 - confidence) * UNCERTAINTY_GAIN
        base_esp = base_esp * uncertainty_factor
        base_esp = round(base_esp / 5) * 5
        seconds = base_esp * 60  # convert to seconds

        _LOG.debug(
            f"...calculate: [{self._body_type}] esp[{seconds:.1f}] "
            f"Target[{current_target}] - Water[{current_water}] = Delta[{degrees_remaining}]"
        )

        heater_note = "ON" if heater_is_on else "OFF. Estimate if started now"

        esp = ESP(seconds, confidence, ESP.format_dhm(seconds))
        esp.rate = rate
        esp.degrees_remaining = degrees_remaining

        msg = (
            f"{esp.display_label} to {int(current_target)}F"
            f" water={round(current_water, 1)}F"
            f" air={round(current_air)}F"
            f" [heater {heater_note}]"
        )

        execution_endtime = time.time()
        execution_duration = execution_endtime - execution_starttime

        _LOG.debug(f"...calculate: ESP using weighted model: rate[{rate:.2f} min/deg] confidence[{confidence:.2f}]")
        _LOG.debug(f"...calculate: [{self._body_type}] Complete: {msg}")
        _LOG.debug(f"...calculate: [{self._body_type}] ESP Calculation took [{execution_duration:.1f}s]")

        return esp


    def _get_history_by_entity(self, metadata:str, body_type:str, all_history, body_config: dict[str, EntityCombo]):
        """Return the raw state list for a single entity identified by metadata key."""
        entity_combo = body_config.get(metadata)
        return all_history.get(entity_combo.id)

    async def _export_history_data(self, history, body_config, start, end):
        """Serialize and write history to a JSON debug file (non-blocking)."""
        now   = datetime.now(timezone.utc)
        hours = HISTORY_DAYS * 24

        def serialize_states(states_list):
            serialized = []
            for s in states_list:
                try:
                    serialized.append({
                        "state":        s.state,
                        "attributes":   dict(s.attributes),
                        "last_updated": s.last_updated.isoformat(),
                        "entity_id":    s.entity_id,
                    })
                except Exception as e:
                    # Use logger directly — _LOG.warning() does not accept extra args
                    _LOG.warning(f"esp_export_history: Failed to serialize State: {e}")
            return serialized

        export_data = {
            "metadata": {
                "exported_at": now.isoformat(),
                "body_type":   self._body_type,
                "start":       start,
                "end":         end,
                "hours":       hours,
                "config":      body_config,
            },
            "history": {
                entity_id: serialize_states(states_list)
                for entity_id, states_list in history.items()
            },
        }

        def write_json(path, data):
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)
            return path

        try:
            output_file = f"/config/esp_test_{self._body_type}_{now.isoformat()}.json"
            written = await self._hass.async_add_executor_job(write_json, output_file, export_data)
            _LOG.debug(f"Exported History to [{written}]")
        except Exception:
            _LOG.error(traceback.format_exc())

    # -------------------------------------------------------------------------
    # State parsing
    # -------------------------------------------------------------------------

    def _parse_state_value(self, raw_state, entity_combo:EntityCombo):
        """Parse a single state object into (timestamp, float|str)."""
        ts    = None
        value = None

        if raw_state:
            try:
                value = raw_state.attributes.get(entity_combo.attribute) if entity_combo.attribute else raw_state.state
                if value is not None and value not in ("unavailable", "unknown"):
                    value = float(value) if entity_combo.datatype == "float" else value
                    ts    = raw_state.last_updated.timestamp()
            except (ValueError, TypeError, AttributeError) as e:
                _LOG.error(f"_parse_state_value({entity_combo}): {e}")

        return ts, value

    def _parse_state_values(self, raw_states: list, metadata: str, body_config: dict[str, EntityCombo]) -> list[tuple]:
        """Parse a list of state objects into [(timestamp, float|str), ...]."""
        results     = []
        entity_combo = body_config.get(metadata)

        if raw_states:
            for state in raw_states:
                try:
                    value = state.attributes.get(entity_combo.attribute) if entity_combo.attribute else state.state
                    if value is not None and value not in ("unavailable", "unknown"):
                        ts    = state.last_updated.timestamp()
                        value = float(value) if entity_combo.datatype == "float" else value
                        results.append((ts, value))
                except (ValueError, TypeError, AttributeError) as e:
                    #_LOG.error(f"_parse_state_values: State: {state}; {e}")
                    pass  # silently ignore unparseable states

        return results

    # -------------------------------------------------------------------------
    # Interval extraction
    # -------------------------------------------------------------------------

    def _extract_heater_on_intervals(self, heat_states, now_ts=None):
        """
        Scan heat_states for heating ON/OFF transitions.
        Returns a list of (on_start_ts, off_ts) tuples.
        """
        _LOG.debug(f"_extract_heater_on_intervals: Contains [{len(heat_states)}] Climate Status records")

        intervals = []
        on_start  = None

        for ts, heat_state in heat_states:
            if heat_state is None or heat_state in ("unavailable", "unknown"):
                _LOG.debug(f"...Skipping None/unavailable state at [{ts}]")
                continue

            is_on = heat_state.lower() == HEATER_STATUS_HEATING_VALUE.lower()

            if is_on and on_start is None:              ### Heater turned ON
                on_start = ts
            elif not is_on and on_start is not None:    ### Heater turned OFF
                self._heating_costs(on_start, ts)
                intervals.append((on_start, ts, False))  # ← 3-tuple, closed
                on_start = None


        # Close open interval if heater still on
        if on_start is not None and now_ts is not None:
            intervals.append((on_start, now_ts, True))   # ← 3-tuple, open
            _LOG.debug("...Heater still ON, closing interval at now")

        for start_ts, end_ts, is_open in intervals:
            duration_min = (end_ts - start_ts) / 60.0
            dt = datetime.fromtimestamp(start_ts)
            time_str = dt.strftime('%Y-%m-%d %H:%M:%S')
            _LOG.debug(f"...start[{start_ts:.0f} {time_str}] duration[{duration_min:4.1f}m] is_open[{is_open}]")

        return intervals


    def _heating_costs(self, on_start:float, ts:float):
        duration_min = (ts - on_start) / 60.0
        self._coordinator.get_sensor(self._body_type, HeaterCostSensor).add_interval_cost(duration_min)
        self._coordinator.get_sensor(self._body_type, HeaterRuntimeSensor).add_interval_runtime(duration_min)   


    # -------------------------------------------------------------------------
    # Rate table construction
    # -------------------------------------------------------------------------

    @staticmethod
    def _interpolate(series, at_ts):
        """Linear interpolation over a (timestamp, value) series."""
        if not series:
            return None
        if at_ts <= series[0][0]:
            return series[0][1]
        if at_ts >= series[-1][0]:
            return series[-1][1]
        for i in range(1, len(series)):
            t0, v0 = series[i - 1]
            t1, v1 = series[i]
            if t0 <= at_ts <= t1:
                frac = (at_ts - t0) / (t1 - t0) if t1 != t0 else 0.0
                return v0 + frac * (v1 - v0)
        return None

    @staticmethod
    def _build_rate_table(
        intervals, water_temps, air_temps,
        air_temp_bin_width, min_interval_minutes,
        min_degrees_gained, min_rate_deg_per_hour,
    ):
        """Build a {air_temp_bin: [min_per_deg, ...]} table from heater-on intervals."""

        def interpolate(series, at_ts):
            if not series:
                return None
            if at_ts <= series[0][0]:
                return series[0][1]
            if at_ts >= series[-1][0]:
                return series[-1][1]
            lo = bisect.bisect_right([t for t, v in series], at_ts) - 1
            lo = max(0, lo)
            if lo >= len(series) - 1:
                return series[-1][1]
            t0, v0 = series[lo]
            t1, v1 = series[lo + 1]
            frac = (at_ts - t0) / (t1 - t0) if t1 != t0 else 0.0
            return v0 + frac * (v1 - v0)

        def interpolate_ts_for_temp(temps, target_temp, start_ts, end_ts):
            relevant = [(ts, v) for ts, v in temps if start_ts <= ts <= end_ts]
            for i in range(1, len(relevant)):
                t0, v0 = relevant[i - 1]
                t1, v1 = relevant[i]
                if v0 <= target_temp <= v1 or v1 <= target_temp <= v0:
                    if v1 == v0:
                        return t0
                    frac = (target_temp - v0) / (v1 - v0)
                    return t0 + frac * (t1 - t0)
            return None

        def air_bin(temp):
            return int(temp // air_temp_bin_width) * air_temp_bin_width

        table            = {}
        skipped_short    = 0
        skipped_no_rise  = 0
        skipped_slow     = 0
        skipped_no_water = 0
        skipped_no_air   = 0
        total_chunks     = 0

        for start_ts, end_ts, is_open in intervals:
            duration_min = (end_ts - start_ts) / 60.0
            if duration_min < min_interval_minutes:
                skipped_short += 1
                _LOG.debug(f"...Skipping short interval: {duration_min:.1f} min")
                continue

            water_at_start = interpolate(water_temps, start_ts)
            water_at_end   = interpolate(water_temps, end_ts)
            if water_at_start is None or water_at_end is None:
                skipped_no_water += 1
                _LOG.debug(f"...Skipping Interval[{local_time(start_ts)} / {local_time(end_ts)}] No Water data at Start{water_at_start}] or End[{water_at_end}]: {duration_min:.1f} min")
                continue

            degrees_gained = water_at_end - water_at_start
            if degrees_gained < min_degrees_gained:
                skipped_no_rise += 1
                _LOG.debug(f"...Skipping [{local_time(start_ts)} / {local_time(end_ts)}] WaterTemp No Rise [{water_at_start} to {water_at_end}] gained[({degrees_gained:.2f}°]) in {duration_min:.1f} min")
                continue

            rate_per_hour = degrees_gained / (duration_min / 60.0)
            if rate_per_hour < min_rate_deg_per_hour:
                skipped_slow += 1
                _LOG.debug(f"...Skipping [{local_time(start_ts)} / {local_time(end_ts)}] Slow interval: RatePerHour[{rate_per_hour:.2f}°/hr] gained[({degrees_gained:.2f}°]) in {duration_min:.1f} min")
                continue

            start_degree = int(water_at_start) + 1
            end_degree   = int(water_at_end)

            if start_degree > end_degree:
                mid_ts  = (start_ts + end_ts) / 2.0
                avg_air = interpolate(air_temps, mid_ts)
                if avg_air is not None:
                    table.setdefault(air_bin(avg_air), []).append(duration_min / degrees_gained)
                    total_chunks += 1
                    _LOG.debug(f"...Interval: {duration_min:.1f} min, {degrees_gained:.2f}° gained, air={avg_air:.1f}F")
                continue

            degree_timestamps = [(water_at_start, start_ts)]
            for deg in range(start_degree, end_degree + 1):
                ts = interpolate_ts_for_temp(water_temps, float(deg), start_ts, end_ts)
                if ts is not None:
                    degree_timestamps.append((float(deg), ts))
            degree_timestamps.append((water_at_end, end_ts))

            for i in range(1, len(degree_timestamps)):
                chunk_start_temp, chunk_start_ts = degree_timestamps[i - 1]
                chunk_end_temp,   chunk_end_ts   = degree_timestamps[i]

                chunk_deg = chunk_end_temp - chunk_start_temp
                if chunk_deg < 0.1:
                    continue

                chunk_min = (chunk_end_ts - chunk_start_ts) / 60.0
                if chunk_min < 0.5:
                    continue

                chunk_rate_per_hour = chunk_deg / (chunk_min / 60.0)
                if chunk_rate_per_hour < min_rate_deg_per_hour:
                    skipped_slow += 1
                    continue

                mid_ts  = (chunk_start_ts + chunk_end_ts) / 2.0
                avg_air = interpolate(air_temps, mid_ts)
                if avg_air is None:
                    skipped_no_air += 1
                    continue

                table.setdefault(air_bin(avg_air), []).append([chunk_min / chunk_deg, chunk_end_ts])
                total_chunks += 1

        return {
            "table":            table,
            "skipped_short":    skipped_short,
            "skipped_no_rise":  skipped_no_rise,
            "skipped_slow":     skipped_slow,
            "skipped_no_water": skipped_no_water,
            "skipped_no_air":   skipped_no_air,
            "used":             total_chunks,
        }

    # -------------------------------------------------------------------------
    # Statistics helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _median(samples: list) -> float:
        s   = sorted(samples)
        n   = len(s)
        mid = n // 2
        return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0

    @staticmethod
    def _trim_samples(samples: list) -> list:
        """Drop the bottom 10% and top 20% of samples to reduce outlier influence."""
        if len(samples) < 5:
            return samples      # Not enough samples to trim
        
        s     = sorted(samples)
        n     = len(s)
        lower = int(n * 0.1)
        upper = int(n * 0.8)
        return s[lower:upper]

    @staticmethod
    def score_bin(samples: list) -> float:
        n = len(samples)
        if n == 0:
            return 0.0
        avg = sum(samples) / n
        if avg == 0:
            return 0.0
        variance        = sum((x - avg) ** 2 for x in samples) / n
        sd              = variance ** 0.5
        cv              = sd / avg
        size_score      = min(n / 20.0, 1.0)
        consistency_score = max(0.0, 1.0 - cv)
        return size_score * consistency_score

    def _weighted_rate(self, table:dict, target_bin:int, bin_width:int):
        """
        Compute a confidence-weighted median rate (min/deg) from the rate table,
        blending bins by proximity to target_bin.
        Returns (rate, confidence).
        """
        _LOG.debug(f"weighted_rate: target_bin[{target_bin}]")
        total_weight = 0.0
        weighted_sum = 0.0

        for bin_key, samples in table.items():
            if not samples:
                _LOG.debug(f"...[{bin_key:3d}] not any samples")
                continue

            clean = self._trim_samples(samples)
            if len(clean) < 3:
                clean = samples

            distance        = abs(bin_key - target_bin) / bin_width
            distance_weight = 1.0 / (1.0 + distance)
            #
            # Single sample — use directly with low confidence
            if len(clean) == 1:
                med    = clean[0]
                weight = SINGLE_SAMPLE_SCORE * distance_weight
                _LOG.debug(f"...[{bin_key:3d}F] single sample med={med:.2f} weight={weight:.2f} (low confidence)"
                )
                weighted_sum += med * weight
                total_weight += weight
                continue
            #
            if len(clean) < 2:  # absolute minimum
                _LOG.debug(f"...[{bin_key:3d}] only {len(samples)} raw samples (need 2+), skipping")
                continue

            score = self.score_bin(clean)
            if score == 0:
                _LOG.debug(f"...[{bin_key:3d}] score_bin == 0, skipping")
                continue

            avg = sum(clean) / len(clean)
            sd  = (sum((x - avg) ** 2 for x in clean) / len(clean)) ** 0.5

            if avg > 0 and (sd / avg) > 0.5:
                _LOG.debug(f"...[{bin_key:3d}] high variance (sd/avg={sd/avg:.2f}), skipping")
                continue

            med             = self._median(clean)
            weight          = score * distance_weight

            _LOG.debug(f"...[{bin_key:3d}] med={med:.2f} score={score:.2f} distance={distance:.1f} weight={weight:.2f}")

            weighted_sum += med * weight
            total_weight += weight
        # end for each table.item

        if total_weight == 0:
            rate       = 0.0
            confidence = 0.0
        else:
            rate       = weighted_sum / total_weight
            confidence = round(min(total_weight / 2.0, 1.0), 2)

        _LOG.debug(f"...Weighted Rate[{rate:.2f}] total_weight[{total_weight:.2f}] confidence[{confidence}]")

        return rate, confidence