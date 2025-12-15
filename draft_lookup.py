import json
import sys

CACHE = "draft_cache_euw_emerald.json"

def main():
    if len(sys.argv) < 2:
        print("Usage: python draft_lookup.py <champion_name>")
        print("Example: python draft_lookup.py jinx")
        raise SystemExit(1)

    champ = sys.argv[1].lower()

    with open(CACHE, "r", encoding="utf-8") as f:
        data = json.load(f)

    row = data["champions"].get(champ)
    if not row:
        print("Not found (or not enough games in your sample):", champ)
        raise SystemExit(2)

    print(f"\n=== {champ.upper()} (EUW Emerald Solo/Duo sample) ===")
    print("Overall:", row["overall"])

    print("\nSynergies (top):")
    for r in row["synergies_top"]:
        print(f"  {r['champion']:>16}  WR={r['winrate']:>6}%  Δ={r['delta']:>6}%  games={r['games']}")

    print("\nWeak against (top):")
    for r in row["weak_against_top"]:
        print(f"  {r['champion']:>16}  WR={r['winrate']:>6}%  games={r['games']}")

if __name__ == "__main__":
    main()
