import asyncio
import json
import os
import time
from collections import defaultdict
from typing import Any, Dict, List, Tuple, Optional

import aiohttp

RIOT_API_KEY = os.getenv("RIOT_API_KEY")
if not RIOT_API_KEY:
    raise SystemExit("Set RIOT_API_KEY env var first (RGAPI-...).")

# Routing:
PLATFORM_HOST = "https://euw1.api.riotgames.com"     # EUW1 platform routing
REGIONAL_HOST = "https://europe.api.riotgames.com"   # Match-V5 regional routing (EUROPE)

QUEUE_RANKED_SOLO = 420

OUT_CACHE = "draft_cache_euw_emerald.json"

# ----------------------------
# Small HTTP helper with basic rate-limit handling
# ----------------------------
class RiotHTTP:
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session

    async def get_json(self, url: str, params: Optional[dict] = None) -> Any:
        headers = {"X-Riot-Token": RIOT_API_KEY}
        while True:
            async with self.s.get(url, headers=headers, params=params) as r:
                if r.status == 429:
                    # Respect Retry-After if provided
                    ra = r.headers.get("Retry-After")
                    wait = float(ra) if ra else 1.0
                    await asyncio.sleep(wait + 0.25)
                    continue
                if r.status >= 400:
                    txt = await r.text()
                    raise RuntimeError(f"HTTP {r.status} for {url} params={params} body={txt[:200]}")
                return await r.json()

# ----------------------------
# Data Dragon champId <-> champName mapping
# ----------------------------
async def fetch_ddragon_champion_map(http: RiotHTTP) -> Tuple[Dict[int, str], Dict[str, int]]:
    # Data Dragon versions + champion.json
    versions = await http.get_json("https://ddragon.leagueoflegends.com/api/versions.json")
    version = versions[0]
    champ_json = await http.get_json(f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json")

    id_to_name: Dict[int, str] = {}
    name_to_id: Dict[str, int] = {}

    for champ_name, obj in champ_json["data"].items():
        champ_id = int(obj["key"])  # numeric championId as string
        id_to_name[champ_id] = champ_name
        name_to_id[champ_name.lower()] = champ_id

    return id_to_name, name_to_id

# ----------------------------
# Getting Emerald players (EUW) -> puuids
# ----------------------------
async def fetch_emerald_entries(http: RiotHTTP, division: str, page: int) -> List[dict]:
    # League-V4 entries:
    # /lol/league/v4/entries/{queue}/{tier}/{division}?page=...
    url = f"{PLATFORM_HOST}/lol/league/v4/entries/RANKED_SOLO_5x5/EMERALD/{division}"
    return await http.get_json(url, params={"page": page})

async def summoner_by_id(http: RiotHTTP, summoner_id: str) -> dict:
    url = f"{PLATFORM_HOST}/lol/summoner/v4/summoners/{summoner_id}"
    return await http.get_json(url)

async def collect_puuids(
    http: RiotHTTP,
    target_puuids: int = 300,
    max_pages_per_div: int = 2,
) -> List[str]:
    """
    Collect a sample of Emerald players' PUUIDs.
    Emerald has divisions I-IV; we sample pages from each until we hit target_puuids.
    """
    puuids: List[str] = []
    seen = set()

    for division in ["I", "II", "III", "IV"]:
        for page in range(1, max_pages_per_div + 1):
            entries = await fetch_emerald_entries(http, division, page)
            if not entries:
                break

            # entries include summonerId; we need puuid via summoner-v4
            for e in entries:
                sid = e.get("summonerId")
                if not sid:
                    continue
                # Fetch puuid
                s = await summoner_by_id(http, sid)
                puuid = s.get("puuid")
                if puuid and puuid not in seen:
                    seen.add(puuid)
                    puuids.append(puuid)
                    if len(puuids) >= target_puuids:
                        return puuids

    return puuids

# ----------------------------
# Match fetch + aggregation
# ----------------------------
async def fetch_match_ids_for_puuid(http: RiotHTTP, puuid: str, count: int = 20) -> List[str]:
    url = f"{REGIONAL_HOST}/lol/match/v5/matches/by-puuid/{puuid}/ids"
    params = {
        "queue": QUEUE_RANKED_SOLO,
        "type": "ranked",
        "start": 0,
        "count": count,
    }
    return await http.get_json(url, params=params)

async def fetch_match(http: RiotHTTP, match_id: str) -> dict:
    url = f"{REGIONAL_HOST}/lol/match/v5/matches/{match_id}"
    return await http.get_json(url)

def update_stats_from_match(
    match: dict,
    overall: Dict[int, List[int]],
    with_ally: Dict[int, Dict[int, List[int]]],
    vs_enemy: Dict[int, Dict[int, List[int]]],
):
    info = match.get("info", {})
    parts = info.get("participants", [])
    if len(parts) != 10:
        return

    # Build team lists
    team_to_champs: Dict[int, List[Tuple[int, bool]]] = defaultdict(list)  # teamId -> [(champId, win)]
    for p in parts:
        champ = p.get("championId")
        team = p.get("teamId")
        win = bool(p.get("win"))
        if champ and team:
            team_to_champs[team].append((int(champ), win))

    teams = list(team_to_champs.keys())
    if len(teams) != 2:
        return
    t1, t2 = teams[0], teams[1]

    champs_t1 = team_to_champs[t1]
    champs_t2 = team_to_champs[t2]

    # For each champ, update overall + synergy (allies) + vs (enemies)
    for team_champs, enemy_champs in [(champs_t1, champs_t2), (champs_t2, champs_t1)]:
        for champ_a, win_a in team_champs:
            overall[champ_a][0] += 1
            overall[champ_a][1] += 1 if win_a else 0

            # allies
            for champ_b, _ in team_champs:
                if champ_b == champ_a:
                    continue
                with_ally[champ_a][champ_b][0] += 1
                with_ally[champ_a][champ_b][1] += 1 if win_a else 0

            # enemies
            for champ_e, _ in enemy_champs:
                vs_enemy[champ_a][champ_e][0] += 1
                vs_enemy[champ_a][champ_e][1] += 1 if win_a else 0

def top_synergies_and_weak_against(
    champ_id: int,
    overall: Dict[int, List[int]],
    with_ally: Dict[int, Dict[int, List[int]]],
    vs_enemy: Dict[int, Dict[int, List[int]]],
    id_to_name: Dict[int, str],
    top_n: int = 10,
    min_games_pair: int = 30,
) -> Dict[str, Any]:
    games, wins = overall.get(champ_id, [0, 0])
    base_wr = (wins / games) if games else None

    # synergies by delta (pair_wr - base_wr)
    syn_list = []
    if base_wr is not None:
        for ally_id, (g, w) in with_ally.get(champ_id, {}).items():
            if g < min_games_pair:
                continue
            wr = w / g
            syn_list.append({
                "champion": id_to_name.get(ally_id, str(ally_id)),
                "winrate": round(wr * 100, 2),
                "games": g,
                "delta": round((wr - base_wr) * 100, 2),
            })
        syn_list.sort(key=lambda x: (x["delta"], x["games"]), reverse=True)

    # weak against: lowest winrate vs enemy
    weak_list = []
    for enemy_id, (g, w) in vs_enemy.get(champ_id, {}).items():
        if g < min_games_pair:
            continue
        wr = w / g
        weak_list.append({
            "champion": id_to_name.get(enemy_id, str(enemy_id)),
            "winrate": round(wr * 100, 2),
            "games": g,
        })
    weak_list.sort(key=lambda x: (x["winrate"], -x["games"]))

    return {
        "overall": {"games": games, "winrate": round(base_wr * 100, 2) if base_wr is not None else None},
        "synergies_top": syn_list[:top_n],
        "weak_against_top": weak_list[:top_n],
    }

async def main():
    async with aiohttp.ClientSession() as session:
        http = RiotHTTP(session)

        print("Loading champion mapping from Data Dragon...")
        id_to_name, name_to_id = await fetch_ddragon_champion_map(http)

        print("Collecting Emerald (EUW) player sample...")
        puuids = await collect_puuids(http, target_puuids=300, max_pages_per_div=2)
        print(f"Got {len(puuids)} puuids.")

        # Aggregation stores: [games, wins]
        overall = defaultdict(lambda: [0, 0])
        with_ally = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        vs_enemy = defaultdict(lambda: defaultdict(lambda: [0, 0]))

        seen_matches = set()

        # Keep it gentle; scale up slowly.
        matches_per_puuid = 20

        print("Fetching match IDs and match details...")
        for i, puuid in enumerate(puuids, 1):
            try:
                ids = await fetch_match_ids_for_puuid(http, puuid, count=matches_per_puuid)
            except Exception as e:
                continue

            for mid in ids:
                if mid in seen_matches:
                    continue
                seen_matches.add(mid)
                try:
                    m = await fetch_match(http, mid)
                    update_stats_from_match(m, overall, with_ally, vs_enemy)
                except Exception:
                    continue

            if i % 25 == 0:
                print(f"  processed {i}/{len(puuids)} puuids, matches={len(seen_matches)}")

            await asyncio.sleep(0.2)  # extra politeness

        print("Computing per-champion top synergies + weak-against...")
        out: Dict[str, Any] = {
            "meta": {
                "region": "EUW",
                "queue": "RANKED_SOLO_5x5",
                "tier": "EMERALD",
                "sample_puuids": len(puuids),
                "unique_matches": len(seen_matches),
                "built_at_unix": int(time.time()),
                "min_games_pair": 30,
            },
            "champions": {}
        }

        for champ_id, (g, w) in overall.items():
            name = id_to_name.get(champ_id)
            if not name or g < 50:
                continue
            out["champions"][name.lower()] = top_synergies_and_weak_against(
                champ_id, overall, with_ally, vs_enemy, id_to_name, top_n=10, min_games_pair=30
            )

        with open(OUT_CACHE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)

        print(f"Saved: {OUT_CACHE}")
        print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
