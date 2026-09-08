import json

from paper_trail.config import CACHE_INDEX


def load_cache_index() -> dict:
    if CACHE_INDEX.exists():
        try:
            with open(CACHE_INDEX, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def update_cache_index(key: str, data: dict):
    cache = load_cache_index()
    cache[key] = data
    CACHE_INDEX.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_INDEX, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
