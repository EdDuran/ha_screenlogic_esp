import logging
import traceback
from .util import *

_LOGGER = logging.getLogger(__name__)

_INFO = True
_DEBUG = True
_TRACE = True
_WARNING = True

def _info(msg: str):
    if (_INFO):
        _LOGGER.info(msg)

def _warning(msg: str):
    if (_WARNING):
        _LOGGER.warning(msg)

def _debug(msg: str):
    if (_DEBUG):
        _LOGGER.debug(msg)

def _trace(msg: str):
    if (_TRACE):
        _LOGGER.info(msg)

async def calculate_wrapper(context):
    """
    Async Wrapper around Calculate ETA
    Returns: Days, Hours, Minutes, ESP and Formatted ESP
    """
    import time

    try:
        startime = time.time()

        body_type = context.body_type
        body_config = context.config
        hass = context.coordinator.hass
        config_entities = context.coordinator.get_config_entities(body_type)

        esp = await calculate(hass, body_config, body_type, context.coordinator)

        if esp is not None:
            days, hours, mins, fmt_esp = get_formatted_esp(esp)
            context.esp = esp 
            return days, hours, mins, esp, fmt_esp
        else:
            return 0, 0, 0, 0, ""

    except ESPException as e:
        context.esp = None
        return 0, 0, 0, 0, STATUS_LEARNING

    except Exception as e:
        _LOGGER.error(traceback.format_exc())
        _LOGGER.error(f"calculate_wrapper: Failed to Calculate ESP; {e}")
        return 0, 0, 0, 0, ""

# ---------------------------------------------------------------------------
# ESP CALCULATION SERVICE  (async — uses recorder instance executor directly)
# ---------------------------------------------------------------------------

async def export_history_data(hass, history, body_type, body_config, start, end):
    import traceback
    from datetime import datetime, timedelta, timezone

    _trace(f"export_history_data")
    _debug(f"...body_type  : {body_type}")
    _debug(f"...history of : {list(history)}]")

    now   = datetime.now(timezone.utc)
    hours = HISTORY_DAYS * 24

    # Serialize to JSON-safe format
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
                _warning("esp_export_history: Failed to serialize State: %s", e)
        return serialized

    export_data = {
        "metadata": {
            "exported_at":  now.isoformat(),
            "body_type":    body_type,
            "start":        start,
            "end":          end,
            "hours":        hours,
            "config":       body_config
        },
        "history": {
            entity_id: serialize_states(states_list)
            for entity_id, states_list in history.items()
        }
    }

    # Write to file using executor (blocking I/O)
    def write_json(path, data):
        import json
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        return path

    try:
        output_file = f"/config/esp_test_{body_type}_{now.isoformat()}.json"
        written = await hass.async_add_executor_job(
            write_json, output_file, export_data)
        _trace(f"Exported History to [{written}]")
    except Exception as e:
        _LOGGER.error(traceback.format_exc())

def _interpolate(series, at_ts):
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



def _extract_heater_on_intervals(heat_states, now_ts = None):
    """
    Look for heat On time and Off time.
    Return Tuples of On and Off times
    """
    _trace(f"_extract_heater_on_intervals: Contains [{len(heat_states)}] Climate Status records")

    intervals = []
    on_start  = None

    for ts, heat_state in heat_states:
        is_on = heat_state == HEATER_STATUS_HEATING_VALUE
        #_debug(f"...{ts} : {heat_state} {is_on} {on_start}")
        if is_on and on_start is None:
            on_start = ts
        elif not is_on and on_start is not None:
            intervals.append((on_start, ts))
            on_start = None

    # Close any still-open interval at now
    if on_start is not None and now_ts is not None:
        intervals.append((on_start, now_ts))
        _debug(f"...ExtractHeaterOnIntervals: heater still ON, closing interval at now")

#Claude debug
    _debug(f"...Heater On Intervals:")
    for start_ts, end_ts in intervals:
        duration_min = (end_ts - start_ts) / 60.0
        _debug(f"   start={start_ts:.0f} end={end_ts:.0f} duration={duration_min:.1f} min")

    return intervals


### ----- Build Rate Table 2 from ChatGPT

def _build_rate_table_dual(
    intervals, water_temps, air_temps,
    air_temp_bin_width, min_interval_minutes,
    min_degrees_gained, min_rate_deg_per_hour):

    import bisect

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

    def interpolate_ts_for_temp(series, target_temp, start_ts, end_ts):
        relevant = [(ts, v) for ts, v in series if start_ts <= ts <= end_ts]
        for i in range(1, len(relevant)):
            t0, v0 = relevant[i-1]
            t1, v1 = relevant[i]
            if v0 <= target_temp <= v1 or v1 <= target_temp <= v0:
                if v1 == v0:
                    return t0
                frac = (target_temp - v0) / (v1 - v0)
                return t0 + frac * (t1 - t0)
        return None

    def air_bin(temp):
        return int(temp // air_temp_bin_width) * air_temp_bin_width

    # =========================
    # OUTPUT TABLES
    # =========================
    granular_table = {}
    coarse_table   = {}

    for start_ts, end_ts in intervals:

        duration_min = (end_ts - start_ts) / 60.0
        if duration_min < min_interval_minutes:
            continue

        w_start = interpolate(water_temps, start_ts)
        w_end   = interpolate(water_temps, end_ts)
        if w_start is None or w_end is None:
            continue

        if (w_end - w_start) < min_degrees_gained:
            continue

        # ============================================================
        # 🔹 1. GRANULAR MODEL (IMPROVED VERSION OF YOUR CURRENT LOGIC)
        # ============================================================

        start_degree = int(w_start) + 1
        end_degree   = int(w_end)

        degree_points = [(w_start, start_ts)]

        for deg in range(start_degree, end_degree + 1):
            ts = interpolate_ts_for_temp(water_temps, float(deg), start_ts, end_ts)
            if ts is not None:
                degree_points.append((float(deg), ts))

        degree_points.append((w_end, end_ts))

        for i in range(1, len(degree_points)):
            t0, ts0 = degree_points[i-1]
            t1, ts1 = degree_points[i]

            deg  = t1 - t0
            mins = (ts1 - ts0) / 60.0

            # 🔧 tighter filters (important)
            if deg < 0.5:
                continue
            if mins < 2.0:
                continue

            rate = deg / (mins / 60.0)
            if rate < min_rate_deg_per_hour:
                continue

            mid_ts = (ts0 + ts1) / 2.0
            air = interpolate(air_temps, mid_ts)
            if air is None:
                continue

            granular_table.setdefault(air_bin(air), []).append(mins / deg)

        # ============================================================
        # 🔹 2. COARSE MODEL (NEW — NO MICRO-CHUNKS)
        # ============================================================

        # Work directly from actual water readings (no interpolation slicing)
        segment = [(ts, v) for ts, v in water_temps if start_ts <= ts <= end_ts]

        if len(segment) < 2:
            continue

        prev_ts, prev_temp = segment[0]

        for ts, temp in segment[1:]:

            delta = temp - prev_temp

            # Only count REAL ~1 degree jumps
            if delta >= 0.8:   # tolerant for sensor rounding
                mins = (ts - prev_ts) / 60.0

                if mins < 2.0:
                    continue

                rate = delta / (mins / 60.0)
                if rate < min_rate_deg_per_hour:
                    continue

                mid_ts = (ts + prev_ts) / 2.0
                air = interpolate(air_temps, mid_ts)
                if air is None:
                    continue

                coarse_table.setdefault(air_bin(air), []).append(mins / delta)

                prev_ts, prev_temp = ts, temp

            else:
                # keep accumulating until we cross ~1°F
                if temp > prev_temp:
                    # do nothing, wait for bigger jump
                    pass
                else:
                    # reset if temp drops (noise or heater off)
                    prev_ts, prev_temp = ts, temp

    return {
        "granular": granular_table,
        "coarse":   coarse_table
    }





MIN_RATE_DEG_PER_HOUR = 0.5  # must be rising at least 0.1°F/hour to count

def _build_rate_table(
    intervals, water_temps, air_temps,
    air_temp_bin_width, min_interval_minutes,
    min_degrees_gained, min_rate_deg_per_hour):

    import bisect

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

    def interpolate_ts_for_temp(water_temps, target_temp, start_ts, end_ts):
        """
        Find the timestamp when water reached target_temp
        within [start_ts, end_ts] by linear interpolation.
        """
        # Find the two readings that straddle the target temp
        relevant = [(ts, v) for ts, v in water_temps 
                    if start_ts <= ts <= end_ts]
        if not relevant:
            return None
        for i in range(1, len(relevant)):
            t0, v0 = relevant[i-1]
            t1, v1 = relevant[i]
            if v0 <= target_temp <= v1 or v1 <= target_temp <= v0:
                if v1 == v0:
                    return t0
                frac = (target_temp - v0) / (v1 - v0)
                return t0 + frac * (t1 - t0)
        return None

    def air_bin(air_temp):
        return int(air_temp // air_temp_bin_width) * air_temp_bin_width

    table = {}
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

        # Slice interval into per-degree chunks
        # Each integer degree boundary becomes a sample point
        start_degree = int(w_start) + 1   # first whole degree above start
        end_degree   = int(w_end)         # last whole degree at or below end

        if start_degree > end_degree:
            # Less than 1 whole degree gained — use whole interval as one sample
            mid_ts  = (start_ts + end_ts) / 2.0
            avg_air = interpolate(air_temps, mid_ts)
            if avg_air is not None:
                table.setdefault(air_bin(avg_air), []).append(duration_min / degrees_gained)
                total_chunks += 1
            continue

        # Find timestamp for each degree boundary
        degree_timestamps = [(w_start, start_ts)]
        for deg in range(start_degree, end_degree + 1):
            ts = interpolate_ts_for_temp(water_temps, float(deg), start_ts, end_ts)
            if ts is not None:
                degree_timestamps.append((float(deg), ts))
        degree_timestamps.append((w_end, end_ts))

        # Each consecutive pair is one chunk
        for i in range(1, len(degree_timestamps)):
            chunk_start_temp, chunk_start_ts = degree_timestamps[i-1]
            chunk_end_temp,   chunk_end_ts   = degree_timestamps[i]

            chunk_deg = chunk_end_temp - chunk_start_temp
            if chunk_deg < 0.1:
                continue

            chunk_min = (chunk_end_ts - chunk_start_ts) / 60.0
            if chunk_min < 0.5:   # ignore sub-30-second chunks
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

            min_per_deg = chunk_min / chunk_deg
            table.setdefault(air_bin(avg_air), []).append(min_per_deg)
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

### Chat GPT


def _median(samples):
    s = sorted(samples)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid-1] + s[mid]) / 2.0


def _trim_samples(samples):
    if len(samples) < 5:
        return samples

    s = sorted(samples)
    n = len(s)

    lower = int(n * 0.1)   # drop bottom 10%
    upper = int(n * 0.8)   # drop top 20%

    return s[lower:upper]



def _weighted_rate(table, target_bin, bin_width):
    total_weight = 0.0
    weighted_sum = 0.0

    _info(f"weighted_rate")

    for bin_key, samples in table.items():
        if not samples:
            _debug(f"...not enough samples at [{bin_key}]")
            continue

        # STEP 1 — trim
        clean = _trim_samples(samples)

        # HARD GUARDS
        if len(clean) < 3:
            _debug(f"...len(clean) < 3 at [{bin_key}]")
            continue

        if len(samples) == 1:
            _debug(f"...len(samples) == 1 at [{bin_key}]")
            continue

        # STEP 2 — score
        score = score_bin(clean)
        if score == 0:
            _debug(f"...score_bin == 0 at [{bin_key}]")
            continue

        # STEP 3 — stats
        avg = sum(clean) / len(clean)
        sd  = (sum((x - avg)**2 for x in clean) / len(clean))**0.5

        if avg > 0 and (sd / avg) > 0.5:
            _debug(f"...avg > 0 and (sd / avg) > 0.5 at [{bin_key}]")
            continue

        med = _median(clean)

        # STEP 4 — distance weighting
        distance = abs(bin_key - target_bin) / bin_width
        distance_weight = 1.0 / (1.0 + distance)

        weight = score * distance_weight

        weighted_sum += med * weight
        total_weight += weight

    if total_weight == 0:
        return None, 0.0

    rate = weighted_sum / total_weight
    confidence = min(total_weight / 2.0, 1.0)

    _debug(f"...Weighted Rate[{rate}] total_weight[{total_weight}] confidence[{confidence}]")

    return rate, confidence

### End ChatGPT


def score_bin(samples):
    n = len(samples)
    if n == 0:
        return 0.0

    avg = sum(samples) / n
    if avg == 0:
        return 0.0

    variance = sum((x - avg) ** 2 for x in samples) / n
    sd = variance ** 0.5
    cv = sd / avg  # coefficient of variation

    # Normalize components
    size_score = min(n / 20.0, 1.0)        # saturates at ~20 samples
    consistency_score = max(0.0, 1.0 - cv) # penalize noisy bins

    return size_score * consistency_score

def confidence_label(self, confidence: float) -> str:
    if confidence >= 0.7:
        return "High"
    elif confidence >= 0.4:
        return "Medium"
    else:
        return "Low"

async def calculate(hass, body_config, body_type, coordinator):
    import traceback

    """
    Single async recorder call fetches all required entities (deduplicating
    shared base entity_ids). Builds a min/deg rate table keyed by air temp
    bin from historical heater-ON intervals, then estimates time to target.

    Works when heater is OFF: produces a hypothetical estimate.
    Results written to helper entities for dashboard display.
    Returns:
        esp,       int: total seconds to SetPoint
    """
    from datetime import datetime, timedelta, timezone
    from homeassistant.components.recorder import get_instance
    import time

    # Get recorder instance once — reused for history fetch AND rate table
    
    _trace(f"calculate: BodyType[{body_type}]")
    _debug(f"...body_config[{body_config}]")
    
    config_entities = coordinator.get_config_entities(body_type)
    _debug(f"...ConfigEntities[{config_entities}]")
    
    instance = get_instance(hass)

    starttime = time.time()

    # Init return values
    esp = None

    now = datetime.now(timezone.utc)

    try:
        history, start, end = await _fetch_all_history(hass, config_entities)

        await export_history_data(hass, history, body_type, body_config, start, end)

        water_history   = _get_history_by_entity(WATER_TEMP, body_type, history, body_config)
        air_history     = _get_history_by_entity(AIR_TEMP, body_type, history, body_config)
        heat_history    = _get_history_by_entity(CLIMATE_STATUS, body_type, history, body_config)
        target_history  = _get_history_by_entity(TARGET_TEMP, body_type, history, body_config)
        
        heat_states     = _parse_state_values(heat_history, body_type, CLIMATE_STATUS, body_config)
        
        now_ts = datetime.now(timezone.utc).timestamp()
        intervals = _extract_heater_on_intervals(heat_states, now_ts)

        detail = f"History Records WaterTemp={len(water_history)}, AirTemp={len(air_history)}, HeatStatus={len(heat_history)}, TargetTemp={len(target_history)}, Heat-On={len(intervals)}"
        _debug(f"...calculate: [{body_type}] {detail}")
    except Exception as e:
        _LOGGER.error(f"calculate: Failed to retrieve History; {e}")
        _LOGGER.error(traceback.format_exc())
        raise ESPException("ERROR", f"calculate: Failed to retrieve History") from e

    if not water_history or not air_history or not heat_history:
        _debug(f"...No Data: Water[{len(water_history)}] Air[{len(air_history)}] Heat[{len(heat_history)}]")
        raise ESPException(STATUS_LEARNING, detail)

    water_temps = _parse_state_values(water_history, body_type, WATER_TEMP, body_config)
    air_temps = _parse_state_values(air_history, body_type, AIR_TEMP, body_config)
    _debug(f"...HeatStates : {len(heat_states)} records")
    _debug(f"...WaterTemps : {len(water_temps)} records")
    _debug(f"...AirTemps   : {len(air_temps)} records")

    try:
        # Run CPU-intensive work in executor — won't block event loop
        result = await instance.async_add_executor_job(
            _build_rate_table,
            intervals,
            water_temps,
            air_temps,
            AIR_TEMP_BIN_WIDTH,
            MIN_INTERVAL_MINUTES,
            MIN_DEGREES_GAINED,
            MIN_RATE_DEG_PER_HOUR,
        )

        dual_result = await instance.async_add_executor_job(
            _build_rate_table_dual,
            intervals,
            water_temps,
            air_temps,
            AIR_TEMP_BIN_WIDTH,
            MIN_INTERVAL_MINUTES,
            MIN_DEGREES_GAINED,
            MIN_RATE_DEG_PER_HOUR,
        )

        _debug(f"dual_result: {dual_result}")

        table = result["table"]
        used = result["used"]
        skipped_short = result["skipped_short"]
        skipped_no_rise = result["skipped_no_rise"]
        skipped_slow = result["skipped_slow"]
        skipped_no_water = result["skipped_no_water"]
        skipped_no_air = result["skipped_no_air"]
    except Exception as e:
        _LOGGER.error(traceback.format_exc())
        raise ESPException("ERROR", f"calculate: Failed to build rate table")

    _debug(f"...calculate: [{body_type}] rate table built — used={used} skipped(short={skipped_short}, no_rise={skipped_no_rise} slow={skipped_slow} no_water={skipped_no_water} no_air={skipped_no_air}")
    if not table:
        detail = f"calculate: [{body_type}] No usable heating intervals yet — need more history"
        _warning(f"calculate: [{body_type}] {detail}")
        raise ESPException(STATUS_LEARNING, detail)

    # Log rate table
    for bin_key in sorted(table.keys()):
        samples = table[bin_key]

        # Mean and sd for context
        avg = sum(samples) / len(samples)
        sd = (sum([(x - avg) ** 2 for x in samples]) / len(samples)) ** 0.5

        # Median — what's actually used for ETA calculation
        sorted_s = sorted(samples)
        n        = len(sorted_s)
        mid      = n // 2
        median   = sorted_s[mid] if n % 2 else (sorted_s[mid-1] + sorted_s[mid]) / 2.0

        _debug(f"Rate Table: [{body_type}] {bin_key:3d}F-{(bin_key + AIR_TEMP_BIN_WIDTH):3d}F AirTemp -> median={median:.2f} avg={avg:.2f} min/deg n={len(samples)} sd={sd:.2f}")

    # Current readings
    try:
        if (body_config):
            current_water  = coordinator._get_current_value(body_config[WATER_TEMP])
            current_air    = coordinator._get_current_value(body_config[AIR_TEMP])
            current_target = coordinator._get_current_value(body_config[TARGET_TEMP])
            heater_is_on   = coordinator._get_current_value(body_config[CLIMATE_STATUS]).lower() == HEATER_STATUS_HEATING_VALUE.lower()
        else:
            _LOGGER.error(f"calculate: Failed to get BodyConfig[{body_type}]")
    except (ValueError, TypeError) as e:
        detail = f"calculate: {body_type} cannot read current sensors; {e}"
        _LOGGER.error(f"calculate: {body_type} {detail}")
        raise ESPException(STATUS_ERROR, detail)

    _debug(f"...calculate: [{body_type}] WaterTemp[{current_water:.1f}F] TargetTemp[{current_target:.1f}F] AirTemp=[{current_air:.1f}F] IsHeaterActive[{heater_is_on}]")

    try:
        # Bin lookup also in executor

        target_bin = int(current_air // AIR_TEMP_BIN_WIDTH) * AIR_TEMP_BIN_WIDTH

        rate, confidence = await instance.async_add_executor_job(
            _weighted_rate,
            table,
            target_bin,
            AIR_TEMP_BIN_WIDTH,
        )

    except Exception as e:
        raise ESPException("ERROR", f"calculate: Failed to calculate weighted rate and confidence") from e

# ChatGPT
#    if bin_key is None:
#        detail = f"Insufficent samples near [{str(round(current_air))}F] air; require [{str(MIN_SAMPLES)}] per bin"
#        _warning(f"calculate: [{body_type}] {detail}")
#        raise ESPException(STATUS_LEARNING, detail)

    if rate is None:
        detail = f"rate is None"
        _warning(f"calculate: [{body_type}] {detail}")
        raise ESPException("NoData", detail)

    _debug(f"...air={round(current_air)}F confidence={confidence:.2f}")

    if confidence < 0.3:
        detail = f"Low confidence: air={round(current_air)}F (confidence={confidence:.2f} {confidence_label})"
        _warning(f"calculate: [{body_type}] {detail}")
        raise ESPException(STATUS_LEARNING, detail)

    confidence_penalty = 1.0 + (1.0 - confidence)

# end ChatGPT

    degrees_remaining = current_target - current_water
    if (degrees_remaining == 0 and heater_is_on):
        degrees_remaining = 1

    UNCERTAINTY_GAIN =  0.5

    base_esp = rate * degrees_remaining
    uncertainty_factor = 1.0 + (1.0 - confidence) * UNCERTAINTY_GAIN
    esp = base_esp * uncertainty_factor

    ##esp = (rate * degrees_remaining) + 5
    esp = round(esp / 5) * 5
    esp = esp * 60    # Converted to seconds
    _debug(f"...calculate: [{body_type}] esp[{esp:.1f}] Target[{current_target}] - Water[{current_water}] = Delta[{degrees_remaining}]")

#    bin_label   = str(bin_key) + "-" + str(bin_key + AIR_TEMP_BIN_WIDTH) + "F"

    days, hours, mins, fmt_esp = get_formatted_esp(esp)
    eta_wall    = now + timedelta(seconds=esp)
    eta_clock   = eta_wall.strftime("%I:%M %p UTC")
    heater_note = "ON" if heater_is_on else "OFF-est if started now"

    msg = (fmt_esp + " to " + str(int(current_target)) + "F"
        + " now=" + str(round(current_water, 1)) + "F"
        + " air=" + str(round(current_air)) + "F"
        + " ESP " + eta_clock
        + " [heater " + heater_note
#        + " n=" + str(n_samples)
#        + " @ " + bin_label
#        + " sd=" + str(round(sd, 1)) + "]")
    )

    label = confidence_label(confidence)
    _debug(f"...ESP using weighted model: rate[{rate:.2f} min/deg] confidence[{confidence:.2f} {label}")

###    _debug(f"...calculate: [{body_type}] ESP={esp:.1f} min ({(esp/60.0):.2f} rate={rate:2f}+-{sd:.2f} min/deg bin={bin_label} n={n_samples}")

    _debug(f"...calculate: [{body_type}] Complete: {msg}")

    duration = time.time() - starttime
    _debug(f"...calculate: [{body_type}] ESP Calculation took [{duration:.1f}s]")

    # Return esp and Status None (no display helper to set)
    # esp of 0 returns None
    esp = esp if esp > 0 else None
    return esp


def _parse_state_value(raw_state: State, entity_combo: str):
    """
    Parse state objects into [(timestamp, float|str), ...].
    Reads from s.attributes[attr] if entity_str has /attribute suffix,
    otherwise reads s.state.
    """
    ts = None
    value = None
    entity_type, entity_id, entity_attr, watch = parse_entity_combo(entity_combo)

    if raw_state:
        try:
            value = raw_state.attributes.get(entity_attr) if entity_attr else raw_state.state
            if value is not None and value not in ["unavailable", "unknown"]:
                value = float(value) if entity_type == "float" else value
                ts = raw_state.last_updated.timestamp()
        except (ValueError, TypeError, AttributeError) as e:
            _LOGGER.error(f"_parse_state_value({entity_combo}): {e}")

    return ts, value

def _parse_state_values(raw_states: list[State], body_type: str, metadata:str, body_config) -> list(Tuple):
    """
    Parse state objects into [(timestamp, float|str), ...].
    Reads from s.attributes[attr] if entity_str has /attribute suffix,
    otherwise reads s.state.
    """

    from homeassistant.core import State
    from typing import List, Tuple
    from datetime import datetime

    ts = None
    value = None
    results = []

    entity_combo = body_config.get(metadata)
    entity_type, entity_id, entity_attr, watch = parse_entity_combo(entity_combo)

    if raw_states:
        try:
            for state in raw_states:
                value = state.attributes.get(entity_attr) if entity_attr else state.state
                if value is not None and value not in ["unavailable", "unknown"]:
                    ts = state.last_updated.timestamp()
                    value = float(value) if entity_type == "float" else value
                    results.append((ts, value))
        except (ValueError, TypeError, AttributeError) as e:
            #timestamp = datetime.fromtimestamp(ts)
            #_warning(f"_parse_state_values: datatype[{entity_type} entity[{entity_id}] attr[{entity_attr}]; {e}")
            #_warning(f"...timestamp: [{timestamp}]")
            #_warning(f"...value: [{value}]")
            #_warning(f"...state {state}")
            pass # Silently Ignore

    return results

def _fetch_history(hass, entity_ids, start_dt, end_dt) -> dict[str, list[any]]:
    """
    Single recorder call for all entity_ids.
    Returns dict {entity_id: [state_objects, ...]}
    """
    import homeassistant.components.recorder.history as rec_history

    _trace(f"fetch_history")
    _debug(f"...entities[{entity_ids}]")
    _debug(f"...start[{start_dt}]")
    _debug(f"...end  [{end_dt}]")

    result = rec_history.get_significant_states(
        hass,
        start_dt,
        end_dt,
        entity_ids,  # entity_ids
        None,        # filters
        True,        # include_start_time_state
        False,       # significant_changes_only
        False,       # minimal_response
        False,       # no_attributes
    )

    if not result:
        return {s: [] for s in entity_ids}

    return result
    
async def _fetch_all_history(hass, config_entities):
    from datetime import datetime, timedelta, timezone
    import time
    from homeassistant.components.recorder import get_instance

    started = time.time()

    end: datetime   = datetime.now(timezone.utc)
    start: datetime = end - timedelta(days=HISTORY_DAYS)
    instance = get_instance(hass)

    history  = await instance.async_add_executor_job(
        _fetch_history, hass, list(config_entities), start, end
    )

    ended = time.time()
    delta = ended - started
    _debug(f"_fetch_all_history: Execution took {delta} seconds")

    return history, start, end



def _get_history_by_entity(metadata: str, body_type: str, all_history, body_config: Config):
    """
    Get the history for the specified BodyType and MetaData keyword
    """
    #_trace(f"_get_history_by_entity()")
    entity_combo = body_config.get(metadata)

    datatype, entity_id, attr, watch = parse_entity_combo(entity_combo)
    entity_history = all_history.get(entity_id)

    #_debug(f"...metadata       : {metadata}")
    #_debug(f"...body_type      : {body_type}")
    #_debug(f"...entity_combo   : {entity_combo}")
    #_debug(f"...entity_history : {len(entity_history)} {metadata} Records")

    return entity_history

