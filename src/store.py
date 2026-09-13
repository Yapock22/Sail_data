"""Stockage JSONL append-only, deduplique sur (hull_id, ts_utc).

Un fichier par jour UTC : data/raw/tracks/<race_id>/<YYYY-MM-DD>.jsonl
Le git du repo sert d'historique et de sauvegarde ; pas de base a administrer.
"""

from __future__ import annotations

import json
import pathlib
from collections import defaultdict

RAW = pathlib.Path("data/raw/tracks")


def _day_file(race_id: str, ts_utc: str) -> pathlib.Path:
    return RAW / race_id / f"{ts_utc[:10]}.jsonl"


def _existing_keys(path: pathlib.Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    keys = set()
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            keys.add((rec["hull_id"], rec["ts_utc"]))
    return keys


def summarize(race_id: str) -> None:
    """Inventaire rapide de ce qui est en base, par classe et par bateau."""
    from collections import Counter

    pings = load_race(race_id)
    by_class: Counter = Counter(p.get("boat_class", "INCONNU") for p in pings)
    by_hull: Counter = Counter(p["hull_id"] for p in pings)
    print(f"{len(pings)} pings / {len(by_hull)} bateaux")
    for cls, n in by_class.most_common():
        print(f"  {cls:<12} {n:>6} pings")
    for hull, n in by_hull.most_common():
        print(f"    {hull:<28} {n:>5}")


def append_pings(pings: list[dict], race_id: str) -> int:
    by_day: dict[pathlib.Path, list[dict]] = defaultdict(list)
    for ping in pings:
        by_day[_day_file(race_id, ping["ts_utc"])].append(ping)

    added = 0
    for path, batch in by_day.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        seen = _existing_keys(path)
        new = []
        for ping in batch:
            key = (ping["hull_id"], ping["ts_utc"])
            if key in seen:
                continue
            seen.add(key)
            new.append(ping)
        if new:
            new.sort(key=lambda p: (p["ts_utc"], p["hull_id"]))
            with path.open("a", encoding="utf-8") as fh:
                for ping in new:
                    fh.write(json.dumps(ping, ensure_ascii=False) + "\n")
            added += len(new)
    return added


def load_race(race_id: str) -> list[dict]:
    out: list[dict] = []
    for path in sorted((RAW / race_id).glob("*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    out.sort(key=lambda p: (p["hull_id"], p["ts_utc"]))
    return out
