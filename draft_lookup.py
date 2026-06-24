import json
import sys
from typing import Dict, Tuple

CACHE = "lolalytics_cache.jsonl"

def load_cache(path: str) -> Dict[Tuple[str, str, str, str, str], dict]:
    out = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            if "error" in obj:
                continue
            key = (
                obj.get("champion"),
                obj.get("role"),
                obj.get("region"),
                obj.get("tier"),
                obj.get("patch") or "",
            )
            out[key] = obj
    return out

def main():
    if len(sys.argv) < 3:
        print("Usage: python draft_lookup.py <champion_slug> <role> [region] [tier] [patch]")
        print("Example: python draft_lookup.py brand support all emerald_plus")
        sys.exit(1)

    champ = sys.argv[1].lower()
    role = sys.argv[2].lower()
    region = sys.argv[3].lower() if len(sys.argv) > 3 else "all"
    tier = sys.argv[4].lower() if len(sys.argv) > 4 else "emerald_plus"
    patch = sys.argv[5] if len(sys.argv) > 5 else ""

    data = load_cache(CACHE).get((champ, role, region, tier, patch))
    if not data:
        print("No cache entry found for:", champ, role, region, tier, patch)
        sys.exit(2)

    print(f"\n=== {champ} ({role}) ===")
    print("\nSynergies:")
    for row in data.get("synergies_top", [])[:10]:
        print(f"  {row.get('champion'):>16}  WR={row.get('winrate')}  games={row.get('games')}")

    print("\nWeak against:")
    for row in data.get("weak_against_top", [])[:10]:
        print(f"  {row.get('champion'):>16}  WR={row.get('winrate')}  games={row.get('games')}")

if __name__ == "__main__":
    main()
