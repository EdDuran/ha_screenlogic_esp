###
### ----- Persistence
###

import json
import os

class Persistence:

    _PATH = "/config/.storage/screenlogic_esp_rate_table_{body_type}.json"

    def __init__():
        pass

    def load_rate_table(path: str):
        if not os.path.exists(path):
            return {"version": 1, "bins": {}}

        with open(path, "r") as f:
            return json.load(f)

    def save_rate_table(path: str, data: dict):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)  # atomic write

    def merge_rate_tables(persistent, new_table):
        for bin_key, samples in new_table.items():
            if not samples:
                continue

            bin_data = persistent["bins"].setdefault(bin_key, {})
            update_bin(bin_data, samples)

        return persistent


    def _update_bin(bin_data, new_samples):
        for x in new_samples:
            count = bin_data.get("count", 0)
            mean  = bin_data.get("mean", 0.0)
            m2    = bin_data.get("m2", 0.0)

            count += 1
            delta = x - mean
            mean += delta / count
            delta2 = x - mean
            m2 += delta * delta2

            bin_data["count"] = count
            bin_data["mean"]  = mean
            bin_data["m2"]    = m2
            bin_data["min"]   = min(bin_data.get("min", x), x)
            bin_data["max"]   = max(bin_data.get("max", x), x)

        return bin_data

