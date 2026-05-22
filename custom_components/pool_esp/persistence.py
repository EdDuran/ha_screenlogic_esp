import logging
import time
import json
from datetime import datetime, timezone
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    MAX_SAMPLES_PER_BIN,
    MAX_RATE_AGE_DAYS,
    STORAGE_VERSION,
)

_LOG = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}_rates.json"


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
                "sample_count": 42,
                "last_merge_intervals": 3
            },
        "pool_type": "Screenlogic"
    }
    """

    def __init__(self, hass, body_type:str, pool_type:str = "Unknown" ):
        self._hass      = hass
        self._body_type = body_type
        self._pool_type = pool_type
        self._store     = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data      = {}  # full storage data (all body types)
        self._loaded    = False

    # -------------------------------------------------------------------------
    # Load / Save
    # -------------------------------------------------------------------------

    async def async_load(self):
        """Load persistent data from HA storage."""
        self._data   = await self._store.async_load() or {}
        self._loaded = True
        last_merge_ts = self._body_data().get("last_merge_ts", 0.0)
        count = self._sample_count()
        _LOG.debug(f"Persistence.async_load: [{self._body_type}] Samples[{count}] last_merge_ts=[{datetime.fromtimestamp(last_merge_ts, tz=timezone.utc).isoformat() if last_merge_ts else 'never'}]")

    async def async_save(self):
        """Save persistent data to HA storage."""
        await self._store.async_save(self._data)
        _LOG.debug(f"Persistence.async_save: [{self._body_type}] saved")

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def merge_and_save(self, new_table:dict, intervals:list, intervals_used:int):
        """
        Merge a freshly built rate table into persistent storage.
        Called by Estimator after each _build_rate_table().
        intervals: list of (start_ts, end_ts) tuples from _extract_heater_on_intervals
            Only merges intervals newer than last_merge_ts to avoid duplicates.
        """
        if not self._loaded:
            await self.async_load()

        self._data["pool_type"] = self._pool_type

        body_data = self._body_data()
        last_merge_ts  = body_data.get("last_merge_ts", 0.0)
        new_high_water = last_merge_ts
        rate_table = body_data.setdefault("rate_table", {})

        # Filter to only NEW intervals
        new_intervals = [
            (start, end, is_open) for start, end, is_open in intervals
            if end > last_merge_ts
        ]
        if not new_intervals:
            _LOG.debug(f"Persistence.merge_and_save: [{self._body_type}] no new intervals since last merge")
            return

        _LOG.debug(f"Persistence.merge_and_save: [{self._body_type}] new intervals[{len(new_intervals)}] of {len(intervals)} total)")
        for start, end, is_open in new_intervals:
            ending_at = datetime.fromtimestamp(end, tz=timezone.utc).isoformat()
            duration = (end - start) / 60.0
            _LOG.debug(f"...Interval: End:[{ending_at}] Duration:[{duration:.1f} min] Open[{is_open}]")

        # Update high-water mark to latest interval end
        # BUT Only advance high-water mark for closed intervals
        closed_new = [(s, e) for s, e, open_ in new_intervals if not open_]
        if closed_new:
            new_high_water = max(e for _, e in closed_new)
            body_data["last_merge_ts"] = new_high_water

        now_ts  = time.time()
        cutoff  = now_ts - (MAX_RATE_AGE_DAYS * 86400) # In seconds
        merged  = 0
        pruned  = 0

        for bin_key, new_samples in new_table.items():
            key      = str(bin_key)
            existing = rate_table.get(key, [])

            # Prune stale samples
            before  = len(existing)
            existing = [s for s in existing if s[1] >= cutoff]
            pruned  += before - len(existing)

            # Add new samples with timestamp
            for sample_rate, sample_ts in new_samples:
                existing.append([sample_rate, sample_ts])
                merged += 1

            # Cap to max — keep most recent
            if len(existing) > MAX_SAMPLES_PER_BIN:
                existing = sorted(existing, key=lambda s: s[1])[-MAX_SAMPLES_PER_BIN:]

            rate_table[key] = existing

        # Update metadata
        body_data["rate_table"]            = rate_table
        body_data["last_updated"]          = datetime.now(timezone.utc).isoformat()
        body_data["sample_count"]          = self._sample_count()
        body_data["last_merge_intervals"]  = intervals_used
        body_data["last_merge_added"]      = merged
        body_data["last_merge_pruned"]     = pruned

        self._data[self._body_type] = body_data

        await self.async_save()

        _LOG.debug(f"Persistence.merge_and_save: [{self._body_type}] merged[{merged}] pruned[{pruned}] total_samples=[{body_data['sample_count']}]")

    def get_rate_table(self) -> dict:
        """
        Return rate table in the format _weighted_rate() expects:
        {air_bin_int: [rate, ...]}  (timestamps stripped)
        """
        body_data  = self._body_data()
        rate_table = body_data.get("rate_table", {})

        return {
            int(bin_key): [s[0] for s in samples]
            for bin_key, samples in rate_table.items()
            if samples
        }

    def get_diagnostics(self) -> dict:
        """
        Return data for display in HA Integrations page diagnostics.
        """
        body_data  = self._body_data()
        rate_table = body_data.get("rate_table", {})

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
            "body_type":            self._body_type,
            "sample_count":         body_data.get("sample_count", 0),
            "last_updated":         body_data.get("last_updated", "never"),
            "last_merge_intervals": body_data.get("last_merge_intervals", 0),
            "last_merge_added":     body_data.get("last_merge_added", 0),
            "last_merge_pruned":    body_data.get("last_merge_pruned", 0),
            "bins":                 bins,
        }

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _body_data(self) -> dict:
        return self._data.setdefault(self._body_type, {})

    def _sample_count(self) -> int:
        rate_table = self._body_data().get("rate_table", {})
        return sum(len(s) for s in rate_table.values())