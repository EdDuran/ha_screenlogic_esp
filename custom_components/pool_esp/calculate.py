async def calculate_eta_wrapper(context):
    """
    Async Wrapper around Calculate ETA
    Returns: Days, Hours, Minutes, TTSP and Formatted TTSP
    """
    try:
        body_type = CONTEXT_get_body_type(context)
        ttsp = calculate_eta(body_type)

        if ttsp is not None:
            days, hours, mins, fmt_ttsp = _get_ttsp_formatted(ttsp)
            log.info(f"ttsp_calculate_eta_wrapper: [{body_type}] ttsp[{ttsp}s {fmt_ttsp}]")
            CONTEXT_set_ttsp(context, ttsp)
            return days, hours, mins, ttsp, fmt_ttsp
        else:
            return 0, 0, 0, 0, ""

    except ESPException as e:
        CONTEXT_set_ttsp(context, None)
        return 0, 0, 0, 0, STATUS_LEARNING

    except Exception as e:
        log.error(f"Failed to Calculate TTSP; {e}")
        return 0, 0, 0, 0, ""

# ---------------------------------------------------------------------------
# TTSP CALCULATION SERVICE  (async — uses recorder instance executor directly)
# ---------------------------------------------------------------------------

@service
async def calculate_eta(body_type):
    """
    Single async recorder call fetches all required entities (deduplicating
    shared base entity_ids). Builds a min/deg rate table keyed by air temp
    bin from historical heater-ON intervals, then estimates time to target.

    Works when heater is OFF: produces a hypothetical estimate.
    Results written to helper entities for dashboard display.
    Returns:
        ttsp,       int: total seconds to SetPoint
    """
    from datetime import datetime, timedelta, timezone
    from homeassistant.components.recorder import get_instance
    import time

    global _config, _watch_entities

    task.unique(TTSP_CALCULATE)

    # Get recorder instance once — reused for history fetch AND rate table
    instance = get_instance(hass)

    starttime = time.time()

    # Init return values
    ttsp = None

    now = datetime.now(timezone.utc)

    history = await _fetch_all_history()

    body_config = _config.get(body_type)
    _debug(f"{body_type.upper()}.BodyConfig: {body_config}")

    water_history = _get_history_by_entity(WATER_TEMP, body_type, history)
    _debug(f"WaterHistory: {water_history}")

    air_history = _get_history_by_entity(AIR_TEMP, body_type, history)
    _debug(f"AirHistory: {air_history}")

    heat_history = _get_history_by_entity(HEAT_STATUS, body_type, history)
    _debug(f"HeatHistory: {heat_history}")

    target_history = _get_history_by_entity(TARGET_TEMP, body_type, history)
    _debug(f"TargetHistory: {target_history}")

    heat_states = _parse_state_values(heat_history, body_type, HEAT_STATUS)
    
    now_ts = datetime.now(timezone.utc).timestamp()
    intervals = _extract_heater_on_intervals(heat_states, now_ts)

    detail = f"History Records WaterTemp={len(water_history)}, AirTemp={len(air_history)}, HeatStatus={len(heat_history)}, TargetTemp={len(target_history)}, Heat-On={len(intervals)}"
    log.info(f"ttsp_calculate_eta: [{body_type}] {detail}")

    if not water_history or not air_history or not heat_history:
        raise ESPException(STATUS_LEARNING, detail)

    water_temps = _parse_state_values(water_history, body_type, WATER_TEMP)
    air_temps = _parse_state_values(air_history, body_type, AIR_TEMP)
    _debug(f"WaterTemps: {water_temps}")
    _debug(f"AirTemps:  {air_temps}")

    # Run CPU-intensive work in executor — won't block event loop
    result = await instance.async_add_executor_job(
        _build_rate_table_compiled,
        intervals,
        water_temps,
        air_temps,
        AIR_TEMP_BIN_WIDTH,
        MIN_INTERVAL_MINUTES,
        MIN_DEGREES_GAINED,
        MIN_RATE_DEG_PER_HOUR,
    )
    
    table = result["table"]
    used = result["used"]
    skipped_short = result["skipped_short"]
    skipped_no_rise = result["skipped_no_rise"]
    skipped_slow = result["skipped_slow"]
    skipped_no_water = result["skipped_no_water"]
    skipped_no_air = result["skipped_no_air"]

    log.info(f"ttsp_calculate_eta: [{body_type}] rate table built — used={used} skipped(short={skipped_short}, no_rise={skipped_no_rise} slow={skipped_slow} no_water={skipped_no_water} no_air={skipped_no_air}")
    if not table:
        detail = f"ttsp_calculate_eta: [{body_type}] No usable heating intervals yet — need more history"
        log.info(f"ttsp_calculate_eta: [{body_type}] {detail}")
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

        log.info(f"ttsp_calculate_eta: [{body_type}] {bin_key:3d}F-{(bin_key + AIR_TEMP_BIN_WIDTH):3d}F AirTemp -> median={median:.2f} avg={avg:.2f} min/deg n={len(samples)} sd={sd:.2f}")

    # Current readings
    try:
        body_config = _config[body_type]
        if (body_config):
            current_water  = _get_current_value(body_config[WATER_TEMP])
            current_air    = _get_current_value(body_config[AIR_TEMP])
            current_target = _get_current_value(body_config[TARGET_TEMP])
            heater_is_on   = _get_current_value(body_config[HEAT_STATUS]).lower() == HEATER_STATUS_HEATING_VALUE.lower()
        else:
            log.error(f"Failed to get BodyConfig[{body_type}]")
    except (ValueError, TypeError) as e:
        detail = f"ttsp_calculate_eta: {body_type} cannot read current sensors; {e}"
        log.error(f"ttsp_calculate_eta: {body_type} {detail}")
        raise ESPException(STATUS_ERROR, detail)

    log.info(f"ttsp_calculate_eta: [{body_type}] WaterTemp[{current_water:.1f}F] TargetTemp[{current_target:.1f}F] AirTemp=[{current_air:.1f}F] HeatStatus[{heater_is_on}]")

    # Bin lookup also in executor
    bin_key, avg_rate, sd, n_samples = await instance.async_add_executor_job(
        _find_best_bin_compiled,
        table,
        current_air,
        AIR_TEMP_BIN_WIDTH,
        MIN_SAMPLES,
    )

    if bin_key is None:
        detail = f"Insufficent samples near [{str(round(current_air))}F] air; require [{str(MIN_SAMPLES)}] per bin"
        log.info(f"ttsp_calculate_eta: [{body_type}] {detail}")
        raise ESPException(STATUS_LEARNING, detail)

    degrees_remaining = current_target - current_water
    if (degrees_remaining == 0 and heater_is_on):
        degrees_remaining = 1

    ttsp = (avg_rate * degrees_remaining) + 5
    ttsp = round(ttsp / 5) * 5
    ttsp = ttsp * 60    # Converted to seconds
    log.info(f"ttsp_calculate_eta: [{body_type}] ttsp[{ttsp:.1f}] Target[{current_target}] - Water[{current_water}] = Delta[{degrees_remaining}]")

    bin_label   = str(bin_key) + "-" + str(bin_key + AIR_TEMP_BIN_WIDTH) + "F"
    days, hours, mins, fmt_ttsp = _get_ttsp_formatted(ttsp)
    eta_wall    = now + timedelta(seconds=ttsp)
    eta_clock   = eta_wall.strftime("%I:%M %p UTC")
    heater_note = "ON" if heater_is_on else "OFF-est if started now"

    msg = (fmt_ttsp + " to " + str(int(current_target)) + "F"
        + "  now=" + str(round(current_water, 1)) + "F"
        + " air=" + str(round(current_air)) + "F"
        + " TTSP " + eta_clock
        + " [heater " + heater_note
        + " n=" + str(n_samples)
        + " @ " + bin_label
        + " sd=" + str(round(sd, 1)) + "]")

    log.info(f"ttsp_calculate_eta: [{body_type}] TTSP={ttsp:.1f} min ({(ttsp/60.0):.2f} rate={avg_rate:2f}+-{sd:.2f} min/deg bin={bin_label} n={n_samples}")

    log.info(f"ttsp_calculate_eta: [{body_type}] Complete: {msg}")

    duration = time.time() - starttime
    log.info(f"ttsp_calculate_eta: [{body_type}] TTSP Calculation took [{duration:.1f}s]")

    # Return ttsp and Status None (no display helper to set)
    # ttsp of 0 returns None
    ttsp = ttsp if ttsp > 0 else None
    return ttsp


async def _fetch_all_history():
    from datetime import datetime, timedelta, timezone
    from homeassistant.components.recorder import get_instance

    now: datetime   = datetime.now(timezone.utc)
    start: datetime = now - timedelta(days=HISTORY_DAYS)
    instance = get_instance(hass)

    _debug("FetchAllHistory...")
    config_entities = get_config_entities(body_type)    # Set of unique entity ids
    _debug(f"  Entity: {config_entities}")

    history  = await instance.async_add_executor_job(
        _fetch_history, hass, list(config_entities), start, now
    )

    _debug(f"  History: {history}")

    return history


def _get_history_by_entity(metadata: str, body_type: str, history, coordinator):
    """
    Get the history for the specified BodyType and MetaData keyword
    """
    #    log.info("Get History by Entity(%s, %s, %s)", metadata, body_type, history)
    body_config = _config.get(body_type)
    body_entity_combo = body_config.get(metadata)
    #    log.info("  Body Entity Combo: %s", body_entity_combo)
    datatype, id, attr = coordinator.get_parse_entity_combo(body_entity_combo)
    body_history = history.get(id)
    #    log.info("  Body History: %s", body_history)
    return body_history


