import logging
import time
import json
from homeassistant.helpers.storage import Store
from datetime import datetime, timezone

from .util import local_time

from .const import (
    DOMAIN,
    HISTORY_DAYS,
    MAX_SAMPLES_PER_BIN,
    MAX_RATE_AGE_DAYS,
)

_LOG = logging.getLogger(__name__)

KEY_STORAGE = f"{DOMAIN}_rates.json"
KEY_STORAGE_VERSION = 1

KEY_HIGHWATER_TS  = "highwater_ts"
KEY_RATE_TABLE   = "rate_table"
KEY_LAST_UPDATED = "last_updated"
KEY_SAMPLE_COUNT = "sample_count"
KEY_LAST_MERGE_INTERVALS = "last_merge_intervals"
KEY_LAST_MERGE_ADDED     = "last_merge_added"
KEY_LAST_MERGE_PRUNED    = "last_merge_pruned"


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
                "last_merge_intervals": 3
            },
        "pool_type": "Screenlogic"
    }
    """

    def __init__(self, hass, body_type:str, pool_type:str = "Unknown" ):
        self._hass      = hass
        self._body_type = body_type
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
            self._data   = await self._store.async_load() or {}
            self._loaded = True
            self._highwater_ts = self.body_data.get(KEY_HIGHWATER_TS, 0.0)
            count = self.sample_count
            _LOG.debug(f"Persistence.async_load: [{self._body_type}] Samples[{count}] highwater[{local_time(self._highwater_ts) if self._highwater_ts else 'never'}]")

    async def async_save(self):
        """Save persistent data to HA storage."""
        await self._store.async_save(self._data)
        _LOG.debug(f"Persistence.async_save: [{self._body_type}] saved")

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def merge_and_save(self, new_table: dict, intervals: list, intervals_used: int):
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

        self._data["pool_type"] = self._pool_type

        body_data     = self.body_data
        rate_table    = body_data.setdefault(KEY_RATE_TABLE, {})

        _LOG.debug(f"Persistence.merge_and_save: [{self._body_type}] highwater[{local_time(self._highwater_ts) if self._highwater_ts else 'never'}]")

        # --- Identify new closed intervals ---------------------------------------
        new_closed = [
            (start, end) for start, end, is_open in intervals
            if not is_open and end > self._highwater_ts
        ]

        # Log open interval for visibility
        open_interval = next(
            ((start, end) for start, end, is_open in intervals if is_open),
            None
        )

        if not new_closed:
            _LOG.debug(f"...no new closed intervals since [{local_time(self._highwater_ts) if self._highwater_ts else 'never'}]")
            if open_interval:
                start, end = open_interval
                _LOG.debug(
                    f"...open interval still active: "
                    f"Start[{local_time(start)}] "
                    f"Duration[{(end - start) / 60.0:.1f} min]"
                )
            return

        for start, end in new_closed:
            _LOG.debug(
                f"...New Closed Interval: "
                f"Start[{local_time(start)}] "
                f"End[{local_time(end)}] "
                f"Duration[{(end - start) / 60.0:.1f} min]"
            )

        # --- Merge samples -------------------------------------------------------
        now_ts = time.time()
        cutoff = now_ts - (MAX_RATE_AGE_DAYS * 86400)
        merged = 0
        pruned = 0

        for bin_key, new_samples in new_table.items():
            key      = str(bin_key)
            existing = rate_table.get(key, [])

            # Prune stale samples
            before   = len(existing)
            existing = [s for s in existing if s[1] >= cutoff]
            pruned  += before - len(existing)

            # Merge new samples
            for sample_rate, sample_ts in new_samples:
                _LOG.debug(
                    f"...Merging Bin[{bin_key}F] "
                    f"Rate[{sample_rate:.2f}] "
                    f"SampleTime[{local_time(sample_ts)}]"
                )
                existing.append([sample_rate, sample_ts])
                merged += 1

            # Cap to max — keep most recent
            if len(existing) > MAX_SAMPLES_PER_BIN:
                existing = sorted(existing, key=lambda s: s[1])[-MAX_SAMPLES_PER_BIN:]

            rate_table[key] = existing

        # --- Advance high-water mark ---------------------------------------------
        if merged > 0:
            new_highwater_ts = max(end for _, end in new_closed)
            body_data[KEY_HIGHWATER_TS] = new_highwater_ts
            _LOG.debug(
                f"...high-water mark advanced to [{local_time(new_highwater_ts)}]"
            )
        else:
            _LOG.warning(
                f"Persistence.merge_and_save: [{self._body_type}] "
                f"no samples merged despite {len(new_closed)} new closed intervals — "
                f"high-water mark NOT advanced"
            )

        # --- Update metadata -----------------------------------------------------
        body_data[KEY_RATE_TABLE]           = rate_table
        body_data[KEY_LAST_UPDATED]         = datetime.now(timezone.utc).isoformat()
        body_data[KEY_SAMPLE_COUNT]         = self.sample_count
        body_data[KEY_LAST_MERGE_INTERVALS] = intervals_used
        body_data[KEY_LAST_MERGE_ADDED]     = merged
        body_data[KEY_LAST_MERGE_PRUNED]    = pruned

        self._data[self._body_type] = body_data
        await self.async_save()

        _LOG.debug(
            f"Persistence.merge_and_save: [{self._body_type}] "
            f"merged[{merged}] pruned[{pruned}] "
            f"total_samples[{body_data['sample_count']}]"
        )






    def get_rate_table(self) -> dict:
        """
        Return rate table in the format _weighted_rate() expects:
        {air_bin_int: [rate, ...]}  (timestamps stripped)
        """
        body_data  = self.body_data
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
        body_data  = self.body_data
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

    @property
    def body_data(self) -> dict:
        return self._data.setdefault(self._body_type, {})

    @property
    def sample_count(self) -> int:
        rate_table = self.body_data.get("rate_table", {})
        return sum(len(s) for s in rate_table.values())