import logging
import traceback
import bisect
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from .util import *
from .coordinator import *

_LOG = logging.getLogger(__name__)


class ESPEstimator:
    """
    Estimates time-to-setpoint (ESP) for a pool/spa body of water.

    Fetches historical sensor data from Home Assistant's recorder,
    builds a heating-rate table keyed by air-temperature bin, and
    projects how long it will take the water to reach the target temp.
    """

    def __init__(self, coordinator: ESPCoordinator, body_type: str):
        self.coordinator: ESPCoordinator = coordinator
        self.body_type: str   = body_type

        self.body_config = self.coordinator.get_config(body_type)
        self.hass        = self.coordinator.hass

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def calculate_wrapper(self, context) -> ESP:
        """
        Async wrapper around calculate_esp.
        Returns: (days, hours, minutes, esp_seconds, formatted_esp)
        """
        try:
            start = time.time()

            esp: ESP = await self.calculate()

            context.esp = esp
            return esp

        except Exception as e:
            _LOG.error(traceback.format_exc())
            _LOG.error(f"calculate_wrapper: Failed to Calculate ESP; {e}")
            raise e

    async def calculate(self) -> ESP:
        """
        Fetch history, build a min/deg rate table keyed by air-temp bin,
        and estimate seconds until the water reaches the target temperature.

        Works when the heater is OFF (hypothetical estimate).

        Returns:
            esp (int | None): total seconds to setpoint, or None if unavailable.
        """
        from homeassistant.components.recorder import get_instance

        body_config = self.coordinator.get_config(self.body_type)
        _LOG.info(f"Estimator.calculate: [{self.body_type}]")
        #_LOG.debug(f"...body_config[{body_config}]")

        config_entities = self.coordinator.get_config_entities(self.body_type)
        #_LOG.debug(f"...ConfigEntities[{config_entities}]")

        instance  = get_instance(self.hass)
        starttime = time.time()
        now       = datetime.now(timezone.utc)

        # --- Fetch history ---------------------------------------------------
        try:
            history, start, end = await self._fetch_all_history(self.hass, config_entities)
            await self._export_history_data(history, body_config, start, end)

            water_history  = self._get_history_by_entity(WATER_TEMP,     self.body_type, history, body_config)
            air_history    = self._get_history_by_entity(AIR_TEMP,       self.body_type, history, body_config)
            heat_history   = self._get_history_by_entity(CLIMATE_STATUS, self.body_type, history, body_config)
            target_history = self._get_history_by_entity(TARGET_TEMP,    self.body_type, history, body_config)

            heat_states = self._parse_state_values(heat_history, CLIMATE_STATUS, body_config)
            now_ts      = datetime.now(timezone.utc).timestamp()
            intervals   = self._extract_heater_on_intervals(heat_states, now_ts)

            detail = (
                f"History Records WaterTemp={len(water_history)}, "
                f"AirTemp={len(air_history)}, HeatStatus={len(heat_history)}, "
                f"TargetTemp={len(target_history)}, Heat-On={len(intervals)}"
            )
            _LOG.debug(f"...calculate: [{self.body_type}] {detail}")

        except Exception as e:
            _LOG.error(f"calculate: Failed to retrieve History; {e}")
            _LOG.error(traceback.format_exc())
            raise ESPException("ERROR", "calculate: Failed to retrieve History") from e

        if not water_history or not air_history or not heat_history:
            _LOG.debug(
                f"...No Data: Water[{len(water_history)}] "
                f"Air[{len(air_history)}] Heat[{len(heat_history)}]"
            )
            return ESP(0, 0, STATUS_LEARNING) # No ESP, No Confidence

        water_temps = self._parse_state_values(water_history, WATER_TEMP, body_config)
        air_temps   = self._parse_state_values(air_history, AIR_TEMP, body_config)
        _LOG.debug(f"...HeatStates : {len(heat_states)} records")
        _LOG.debug(f"...WaterTemps : {len(water_temps)} records")
        _LOG.debug(f"...AirTemps   : {len(air_temps)} records")

        MIN_RATE_DEG_PER_HOUR = 0.5  # must be rising at least 0.1°F/hour to count

        # --- Build rate table ------------------------------------------------
        try:
            result = await instance.async_add_executor_job(
                self._build_rate_table,
                intervals, water_temps, air_temps,
                AIR_TEMP_BIN_WIDTH, MIN_INTERVAL_MINUTES,
                MIN_DEGREES_GAINED, MIN_RATE_DEG_PER_HOUR,
            )

            table            = result["table"]
            used             = result["used"]
            skipped_short    = result["skipped_short"]
            skipped_no_rise  = result["skipped_no_rise"]
            skipped_slow     = result["skipped_slow"]
            skipped_no_water = result["skipped_no_water"]
            skipped_no_air   = result["skipped_no_air"]

        except Exception:
            _LOG.error(traceback.format_exc())
            raise ESPException("ERROR", "calculate: Failed to build rate table") from e

        _LOG.debug(
            f"...calculate: rate table built [{self.body_type}] — "
            f"used={used} skipped(short={skipped_short}, no_rise={skipped_no_rise} "
            f"slow={skipped_slow} no_water={skipped_no_water} no_air={skipped_no_air})"
        )

        if not table:
            detail = f"calculate: [{self.body_type}] No usable heating intervals yet — need more history"
            _LOG.warning(f"calculate: [{self.body_type}] {detail}")
            return ESP(0, 0, STATUS_LEARNING) # No ESP, No Confidence

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
                f"Rate Table: [{self.body_type}] Air {bin_key:3d}F-{(bin_key + AIR_TEMP_BIN_WIDTH):3d}F "
                f"-> median={median:.2f} avg={avg:.2f} min/deg n={len(samples)} sd={sd:.2f}"
            )

        # --- Read current sensor values --------------------------------------
        try:
            if body_config:
                current_water  = self.coordinator._get_current_value(body_config[WATER_TEMP])
                current_air    = self.coordinator._get_current_value(body_config[AIR_TEMP])
                current_target = self.coordinator._get_current_value(body_config[TARGET_TEMP])
                heater_is_on   = (
                    self.coordinator._get_current_value(body_config[CLIMATE_STATUS]).lower() == HEATER_STATUS_HEATING_VALUE.lower()
                )
            else:
                _LOG.error(f"calculate: Failed to get BodyConfig[{self.body_type}]")
        except (ValueError, TypeError) as e:
            detail = f"calculate: {self.body_type} cannot read current sensors; {e}"
            _LOG.error(f"calculate: {self.body_type} {detail}")
            raise ESPException("ERROR", detail)

        _LOG.debug(
            f"...calculate: [{self.body_type}] "
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
            raise ESPException("ERROR", "calculate: Failed to calculate weighted rate and confidence")

        if rate is None:
            _LOG.warning(f"calculate: [{self.body_type}] rate is None")
            return ESP(0, 0, STATUS_LEARNING) # No ESP, No Confidence

        # --- ESP calculation -------------------------------------------------
        UNCERTAINTY_GAIN  = 0.5
        degrees_remaining = current_target - current_water
        if degrees_remaining == 0 and heater_is_on:
            degrees_remaining = 1

        base_esp          = rate * degrees_remaining
        uncertainty_factor = 1.0 + (1.0 - confidence) * UNCERTAINTY_GAIN
        base_esp = base_esp * uncertainty_factor
        base_esp = round(base_esp / 5) * 5
        seconds = base_esp * 60  # convert to seconds

        _LOG.debug(
            f"...calculate: [{self.body_type}] esp[{seconds:.1f}] "
            f"Target[{current_target}] - Water[{current_water}] = Delta[{degrees_remaining}]"
        )

        heater_note = "ON" if heater_is_on else "OFF. Estimate if started now"

        esp = ESP(seconds, confidence)

        msg = (
            f"{esp.display_label} to {int(current_target)}F"
            f" water={round(current_water, 1)}F"
            f" air={round(current_air)}F"
            f" [heater {heater_note}]"
        )

        _LOG.debug(f"...ESP using weighted model: rate[{rate:.2f} min/deg] confidence[{confidence:.2f} {esp.confidence_label}]")
        _LOG.debug(f"...calculate: [{self.body_type}] Complete: {msg}")
        _LOG.debug(f"...calculate: [{self.body_type}] ESP Calculation took [{time.time() - starttime:.1f}s]")

        return esp

    # -------------------------------------------------------------------------
    # History helpers
    # -------------------------------------------------------------------------

    async def _fetch_all_history(self, hass, config_entities):
        from homeassistant.components.recorder import get_instance

        end: datetime   = datetime.now(timezone.utc)
        start: datetime = end - timedelta(days=HISTORY_DAYS)
        instance = get_instance(hass)

        history = await instance.async_add_executor_job(
            self._fetch_history, hass, list(config_entities), start, end
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

    @staticmethod
    def _get_history_by_entity(metadata: str, body_type: str, all_history, body_config):
        """Return the raw state list for a single entity identified by metadata key."""
        entity_combo = body_config.get(metadata)
        _, entity_id, _, _ = parse_entity_combo(entity_combo)
        return all_history.get(entity_id)

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
                "body_type":   self.body_type,
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
            output_file = f"/config/esp_test_{self.body_type}_{now.isoformat()}.json"
            written = await self.hass.async_add_executor_job(write_json, output_file, export_data)
            _LOG.debug(f"Exported History to [{written}]")
        except Exception:
            _LOG.error(traceback.format_exc())

    # -------------------------------------------------------------------------
    # State parsing
    # -------------------------------------------------------------------------

    @staticmethod
    def _parse_state_value(raw_state, entity_combo: str):
        """Parse a single state object into (timestamp, float|str)."""
        ts    = None
        value = None
        entity_type, entity_id, entity_attr, _ = parse_entity_combo(entity_combo)

        if raw_state:
            try:
                value = raw_state.attributes.get(entity_attr) if entity_attr else raw_state.state
                if value is not None and value not in ("unavailable", "unknown"):
                    value = float(value) if entity_type == "float" else value
                    ts    = raw_state.last_updated.timestamp()
            except (ValueError, TypeError, AttributeError) as e:
                _LOG.error(f"_parse_state_value({entity_combo}): {e}")

        return ts, value

    @staticmethod
    def _parse_state_values(raw_states: list, metadata: str, body_config) -> list[tuple]:
        """Parse a list of state objects into [(timestamp, float|str), ...]."""
        results     = []
        entity_combo = body_config.get(metadata)
        entity_type, _, entity_attr, _ = parse_entity_combo(entity_combo)

        if raw_states:
            for state in raw_states:
                try:
                    value = state.attributes.get(entity_attr) if entity_attr else state.state
                    if value is not None and value not in ("unavailable", "unknown"):
                        ts    = state.last_updated.timestamp()
                        value = float(value) if entity_type == "float" else value
                        results.append((ts, value))
                except (ValueError, TypeError, AttributeError):
                    pass  # silently ignore unparseable states

        return results

    # -------------------------------------------------------------------------
    # Interval extraction
    # -------------------------------------------------------------------------

    @staticmethod
    def _extract_heater_on_intervals(heat_states, now_ts=None):
        """
        Scan heat_states for heating ON/OFF transitions.
        Returns a list of (on_start_ts, off_ts) tuples.
        """
        _LOG.debug(f"_extract_heater_on_intervals: Contains [{len(heat_states)}] Climate Status records")

        intervals = []
        on_start  = None

        for ts, heat_state in heat_states:
            is_on = heat_state == HEATER_STATUS_HEATING_VALUE
            if is_on and on_start is None:
                on_start = ts
            elif not is_on and on_start is not None:
                intervals.append((on_start, ts))
                on_start = None

        if on_start is not None and now_ts is not None:
            intervals.append((on_start, now_ts))
            _LOG.debug("...ExtractHeaterOnIntervals: heater still ON, closing interval at now")

        for start_ts, end_ts in intervals:
            duration_min = (end_ts - start_ts) / 60.0
            dt = datetime.fromtimestamp(start_ts)
            time = dt.strftime('%Y-%m-%d %H:%M:%S')
            _LOG.debug(f"   start={start_ts:.0f}/{time} duration={duration_min:.1f} min")

        return intervals

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

        for start_ts, end_ts in intervals:
            duration_min = (end_ts - start_ts) / 60.0
            if duration_min < min_interval_minutes:
                skipped_short += 1
                continue

            w_start = interpolate(water_temps, start_ts)
            w_end   = interpolate(water_temps, end_ts)
            if w_start is None or w_end is None:
                skipped_no_water += 1
                continue

            degrees_gained = w_end - w_start
            if degrees_gained < min_degrees_gained:
                skipped_no_rise += 1
                continue

            rate_per_hour = degrees_gained / (duration_min / 60.0)
            if rate_per_hour < min_rate_deg_per_hour:
                skipped_slow += 1
                continue

            start_degree = int(w_start) + 1
            end_degree   = int(w_end)

            if start_degree > end_degree:
                mid_ts  = (start_ts + end_ts) / 2.0
                avg_air = interpolate(air_temps, mid_ts)
                if avg_air is not None:
                    table.setdefault(air_bin(avg_air), []).append(duration_min / degrees_gained)
                    total_chunks += 1
                continue

            degree_timestamps = [(w_start, start_ts)]
            for deg in range(start_degree, end_degree + 1):
                ts = interpolate_ts_for_temp(water_temps, float(deg), start_ts, end_ts)
                if ts is not None:
                    degree_timestamps.append((float(deg), ts))
            degree_timestamps.append((w_end, end_ts))

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

                table.setdefault(air_bin(avg_air), []).append(chunk_min / chunk_deg)
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
            return samples
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

    def _weighted_rate(self, table: dict, target_bin: int, bin_width: int):
        """
        Compute a confidence-weighted median rate (min/deg) from the rate table,
        blending bins by proximity to target_bin.
        Returns (rate, confidence).
        """
        _LOG.debug("weighted_rate")
        total_weight = 0.0
        weighted_sum = 0.0

        for bin_key, samples in table.items():
            if not samples:
                _LOG.debug(f"...not enough samples at [{bin_key}]")
                continue

            clean = self._trim_samples(samples)
            if len(clean) < 3:
                _LOG.debug(f"...len(clean) < 3 at [{bin_key}]")
                continue
            if len(samples) == 1:
                _LOG.debug(f"...len(samples) == 1 at [{bin_key}]")
                continue

            score = self.score_bin(clean)
            if score == 0:
                _LOG.debug(f"...score_bin == 0 at [{bin_key}]")
                continue

            avg = sum(clean) / len(clean)
            sd  = (sum((x - avg) ** 2 for x in clean) / len(clean)) ** 0.5

            if avg > 0 and (sd / avg) > 0.5:
                _LOG.debug(f"...avg > 0 and (sd / avg) > 0.5 at [{bin_key}]")
                continue

            med             = self._median(clean)
            distance        = abs(bin_key - target_bin) / bin_width
            distance_weight = 1.0 / (1.0 + distance)
            weight          = score * distance_weight

            weighted_sum += med * weight
            total_weight += weight

        if total_weight == 0:
            return None, 0.0

        rate       = weighted_sum / total_weight
        confidence = round(min(total_weight / 2.0, 1.0), 2)

        _LOG.debug(f"...Weighted Rate[{rate}] total_weight[{total_weight}] confidence[{confidence}]")
        return rate, confidence