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
        "spa": {
            "rate_table": {
                "70": [[rate, timestamp], ...],
                "75": [[rate, timestamp], ...]
            },
            "last_updated": "2026-05-12T00:00:00+00:00",
            "sample_count": 42,
            "last_merge_intervals": 3
        }
    }
    """

    def __init__(self, hass, body_type: str):
        self._hass      = hass
        self._body_type = body_type
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
        count = self._sample_count()
        _LOG.debug(f"Persistence.async_load: [{self._body_type}] {count} samples loaded")

    async def async_save(self):
        """Save persistent data to HA storage."""
        await self._store.async_save(self._data)
        _LOG.debug(f"Persistence.async_save: [{self._body_type}] saved")

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def merge_and_save(self, new_table: dict, intervals_used: int):
        """
        Merge a freshly built rate table into persistent storage.
        Called by Estimator after each _build_rate_table().
        """
        if not self._loaded:
            await self.async_load()

        body_data = self._body_data()
        rate_table = body_data.setdefault("rate_table", {})

        now_ts  = time.time()
        cutoff  = now_ts - (MAX_RATE_AGE_DAYS * 86400)
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
            for sample in new_samples:
                existing.append([sample, now_ts])
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