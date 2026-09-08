import logging
import time
import json
from homeassistant.helpers.storage import Store
from datetime import datetime, timezone

from .util import local_time

from .const import (
    BODY_TYPES,
    DOMAIN,
    HISTORY_DAYS,
    MAX_SAMPLES_PER_BIN,
    MAX_RATE_AGE_DAYS,
)

_LOG = logging.getLogger(__name__)

KEY_STORAGE = f"{DOMAIN}_rates.json"
KEY_STORAGE_VERSION = 1

KEY_HIGHWATER_TS          = "highwater_ts"
KEY_RATE_TABLE            = "rate_table"
KEY_LAST_UPDATED          = "last_updated"
KEY_SAMPLE_COUNT          = "sample_count"
KEY_LAST_MERGE_INTERVALS  = "last_merge_intervals"
KEY_LAST_MERGE_ADDED      = "last_merge_added"
KEY_LAST_MERGE_PRUNED     = "last_merge_pruned"
KEY_TOTAL_RUNTIME_HOURS   = "total_runtime_hours"
KEY_TOTAL_COST            = "total_cost"


class Persistence:
    """
    Manages long-lived ESP rate table storage, merging, and pruning.
    Survives HA recorder's 30-day window.
    
    Rate table structure (per body_type):
    {
        "bodies": {
            "pool": {},
            "spa": {    
                "rate_table": {
                    "70": [[rate, timestamp], ...],
                    "75": [[rate, timestamp], ...]
                },
                "last_updated": "2026-05-12T00:00:00+00:00",
                "highwater_ts": 1234567890.0,
                "sample_count": 42,
                "last_merge_intervals": 3,
                "total_cost": 59.83,
                "total_runtime_hours": 84.5
            },
        "pool_type": "Screenlogic"
    }
    """

    def __init__(self, hass, pool_type:str = "Unknown" ):
        self._hass      = hass
        self._pool_type = pool_type
        self._store     = Store(hass, KEY_STORAGE_VERSION, KEY_STORAGE)
        self._data      = {}  # full storage data (all body types)
        self._loaded    = False

    # -------------------------------------------------------------------------
    # Load / Save
    # -------------------------------------------------------------------------

    async def async_load(self):
        """Load persistent data from HA storage."""
        if (not self._loaded):
            _LOG.debug(f"async_load() Pool Samples[{self.sample_count("pool")}], Spa Samples[{self.sample_count("spa")}]")
            self._data   = await self._store.async_load() or {}
            self._presort_rate_table()
            ### Pre-sort rate_table 'just-in-case'
            self._loaded = True

    def _presort_rate_table(self):
        """Ensure all Rate Tables are sorted"""
        for body_type in BODY_TYPES:
            body_data = self._data.setdefault(body_type, {})
            rate_table = body_data.get("rate_table", None)
            if rate_table is not None:
                rate_table = self._sort_rate_table(rate_table)
                self._data[body_type]["rate_table"] = rate_table

    def _sort_rate_table(self, rate_table:dict) -> dict:
        """Sort the Rate Table by timestamp in descending order"""

        sorted_rate_table = {
            temp: sorted(samples, key=lambda item: item[1], reverse=True)
                for temp, samples in rate_table.items()
        }

        return sorted_rate_table


    async def async_save(self):
        """Save persistent data to HA storage."""
        await self._store.async_save(self._data)
        _LOG.debug(f"Saved Persistence: HighWater pool[{local_time(self.highwater_ts("pool"))}] spa[{local_time(self.highwater_ts("spa"))}]")

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def merge_and_save(self, body_type:str, new_table:dict, intervals:list, intervals_used:int, cost_per_hour:float = 0.0):
        """
        Merge a freshly built rate table into persistent storage.
        Called by Estimator after each time the Rate Table is built.

        new_table:       dict of {air_bin_int: [(rate, ts), ...]}
        intervals:       list of (start_ts, end_ts, is_open) tuples
        intervals_used:  count of intervals that contributed to new_table (diagnostics)

        Duplicate prevention:
        - processed_intervals: set of end_ts values already merged — survives deletes
        - Only closed intervals (is_open=False) are permanently recorded
        - Open intervals contribute to current estimate but are re-evaluated next run
        - High-water mark only advances if samples were actually merged
        """

        await self.async_load()

        self._data["pool_type"] = self.pool_type

        highwater_ts  = self.highwater_ts(body_type)

        _LOG.debug(f"merge_and_save: [{body_type}] highwater[{local_time(highwater_ts) if highwater_ts else 'never'}]")

        # --- Identify new closed intervals ---------------------------------------
        new_closed = [
            (start, end) for start, end, is_open in intervals
                if not is_open and end > highwater_ts
        ]

        # Log open interval for visibility
        open_interval = next(
            ((start, end) for start, end, is_open in intervals if is_open), None)

        if not new_closed:
            _LOG.debug(f"...Recorder: No new closed intervals since highwater[{local_time(highwater_ts) if highwater_ts else 'never'}]")
            if open_interval:
                start, end = open_interval
                _LOG.debug(f"...Recorder: Open interval active: Start[{local_time(start)}] Duration[{(end - start) / 60.0:.1f} min]")
            return ### Nothing to merge or save
        
        ###
        ### ----- Merge New Closed Intervals ----------------------------------
        ###

        for start, end in new_closed:
            _LOG.debug(f"...Recorder: New Closed Interval: Start[{local_time(start)}] End[{local_time(end)}] Duration[{(end - start) / 60.0:.1f} min]")

        new_runtime_minutes = sum((end - start) / 60.0 for start, end in new_closed)

        # --- Merge samples ---------------------------------------------------
        now_ts = time.time()
        cutoff = now_ts - (MAX_RATE_AGE_DAYS * 86400)
        merged = 0
        pruned = 0
        body_data  = self.body_data(body_type)
        rate_table = body_data.setdefault(KEY_RATE_TABLE, {})

        for bin_key, new_samples in new_table.items():
            key      = str(bin_key)
            existing = rate_table.get(key, [])

            # Prune stale samples
            before   = len(existing)
            existing = [s for s in existing if s[1] >= cutoff]
            pruned  += before - len(existing)

            # Merge new samples AFTER highwater mark
            for sample_rate, sample_ts in new_samples:
                if (sample_ts > highwater_ts):
                    if (sample_ts == end for start, end in new_closed):
                        if ([sample_rate, sample_ts] in existing):
                            _LOG.debug(f"...ESP Rate DUP  : Bin[{bin_key}F] Rate[{sample_rate:.2f}] SampleTime[{local_time(sample_ts)}]")
                        else:
                            _LOG.debug(f"...ESP Rate MERGE: Bin[{bin_key}F] Rate[{sample_rate:.2f}] SampleTime[{local_time(sample_ts)}]")
                            existing.append([sample_rate, sample_ts])
                            merged += 1
                    else:
                        _LOG.debug(f"...ESP Rate SKIP : Bin[{bin_key}F] Rate[{sample_rate:.2f}] SampleTime[{local_time(sample_ts)}]")

            # Cap to max — keep most recent
            if len(existing) > MAX_SAMPLES_PER_BIN:
                existing = sorted(existing, key=lambda s: s[1])[-MAX_SAMPLES_PER_BIN:]

            rate_table[key] = existing

        ###
        ### ----- Advance high-water mark and Heater Costs --------------------
        ###       This occurs even if there is no new merged data,
        ###       But there *were* New Closed Intervals
        ###
        if merged > 0 or new_runtime_minutes > 0:
            if new_runtime_minutes > 0:
                self._update_heater_costs(body_data, new_runtime_minutes, cost_per_hour)

            if merged > 0:
                ### Only advance highwater mark if we actually merged samples
                ### prevents skipping intervals due to merges that didn't "take" (e.g. all samples were duplicates)
                new_highwater_ts = max(end for _, end in new_closed)
                _LOG.debug(f"...Merged [{merged}] items, Advancing HIGHWATER mark [{local_time(self.highwater_ts(body_type))} --> {local_time(new_highwater_ts)}]")
                self.set_highwater_ts(body_type, new_highwater_ts)
        else:
            _LOG.warning(f"Persistence.merge_and_save: [{body_type}] No samples merged despite {len(new_closed)} new closed intervals — HIGHWATER mark NOT advanced")

        ###
        ### ----- Sort the Rate Table
        ###
        sorted_rate_table = self._sort_rate_table(rate_table)

        # --- Update metadata -----------------------------------------------------
        body_data[KEY_RATE_TABLE]           = sorted_rate_table
        body_data[KEY_LAST_UPDATED]         = datetime.now(timezone.utc).isoformat()
        body_data[KEY_SAMPLE_COUNT]         = self.sample_count(body_type)
        body_data[KEY_LAST_MERGE_INTERVALS] = intervals_used
        body_data[KEY_LAST_MERGE_ADDED]     = merged
        body_data[KEY_LAST_MERGE_PRUNED]    = pruned

        self._data[body_type] = body_data
        await self.async_save()

        _LOG.debug(f"Persistence.merge_and_save: [{body_type}] merged[{merged}] pruned[{pruned}] total_samples[{self.sample_count(body_type)}]")
    

    def _update_heater_costs(self, body_data: dict, new_runtime_minutes: float, cost_per_hour: float):
        ### Update runtime and cost totals — these are used for diagnostics and to determine if the pool is "expensive" to heat
        new_runtime_hours = new_runtime_minutes / 60.0
        existing_runtime  = body_data.get(KEY_TOTAL_RUNTIME_HOURS, 0.0)
        existing_cost     = body_data.get(KEY_TOTAL_COST, 0.0)
        new_cost          = new_runtime_hours * cost_per_hour

        body_data[KEY_TOTAL_RUNTIME_HOURS] = existing_runtime + new_runtime_hours
        body_data[KEY_TOTAL_COST]          = existing_cost + new_cost

        _LOG.debug(f"...runtime[{new_runtime_hours:.2f} hours] total[{body_data[KEY_TOTAL_RUNTIME_HOURS]:.2f} hours]")
        _LOG.debug(f"...cost[+${new_cost:.2f}] total[${body_data[KEY_TOTAL_COST]:.2f}]")


    def get_rate_table(self, body_type:str) -> dict:
        """
        Return rate table in the format _weighted_rate() expects:
        {air_bin_int: [rate, ...]}  (timestamps stripped)
        """
        body_data  = self.body_data(body_type)
        rate_table = body_data.get("rate_table", {})

        return {
            int(bin_key): [s[0] for s in samples]
            for bin_key, samples in rate_table.items()
            if samples
        }

    def get_diagnostics(self, body_type:str) -> dict:
        """
        Return data for display in HA Integrations page diagnostics.
        """
        body_data  = self.body_data
        rate_table = body_data.get(KEY_RATE_TABLE, {})

        bins = {}
        for bin_key, samples in sorted(rate_table.items(), key=lambda x: int(x[0])):
            values = [s[0] for s in samples]
            if values:
                avg    = sum(values) / len(values)
                bins[f"{bin_key}F"] = {
                    "samples": len(values),
                    "avg_min_per_deg": round(avg, 2),
                    "newest": datetime.fromtimestamp(
                        max(s[1] for s in samples), tz=timezone.utc
                    ).isoformat(),
                }

        return {
            "body_type":            body_type,
            "sample_count":         body_data.get("sample_count", 0),
            "last_updated":         body_data.get("last_updated", "never"),
            "last_merge_intervals": body_data.get("last_merge_intervals", 0),
            "last_merge_added":     body_data.get("last_merge_added", 0),
            "last_merge_pruned":    body_data.get("last_merge_pruned", 0),
            "bins":                 bins,
        }

    # -------------------------------------------------------------------------
    # Public properties
    # -------------------------------------------------------------------------

    @property
    def data(self) -> dict:
        return self._data
    
    @property
    def pool_type(self) -> float:
        return self._pool_type

    def body_data(self, body_type:str) -> dict:
        return self._data.setdefault(body_type, {})

    def total_runtime_hours(self, body_type:str) -> float:
        return self.body_data(body_type).get(KEY_TOTAL_RUNTIME_HOURS, 0.0)

    def total_cost(self, body_type) -> float:
        return self.body_data(body_type).get(KEY_TOTAL_COST, 0.0)
    
    def highwater_ts(self, body_type:str) -> float:
        return self.body_data(body_type).get(KEY_HIGHWATER_TS, 0.0)
    
    def set_highwater_ts(self, body_type:str, value):
        self.body_data(body_type)[KEY_HIGHWATER_TS] = value

    def last_merge_intervals(self, body_type:str) -> float:
        return self.body_data(body_type).get(KEY_LAST_MERGE_INTERVALS, 0.0)

    def last_merge_added(self, body_type:str) -> float:
        return self.body_data(body_type).get(KEY_LAST_MERGE_ADDED, 0.0)

    def last_merge_pruned(self, body_type:str) -> float:
        return self.body_data(body_type).get(KEY_LAST_MERGE_PRUNED, 0.0)

    def sample_count(self, body_type) -> int:
        rate_table = self.body_data(body_type).get(KEY_RATE_TABLE, {})
        return sum(len(s) for s in rate_table.values())

    # -------------------------------------------------------------------------
    # Private properties
    # -------------------------------------------------------------------------


