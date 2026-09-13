"""Verifie qu'on est dans la fenetre de course avant de payer un Chromium.

Sans dependance externe : tourne sur le Python systeme du runner, avant
setup-python et avant pip install. Une execution hors fenetre coute quelques
secondes au lieu de deux minutes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re


def parse_window(path: str, race_id: str) -> tuple[str, str]:
    """Mini-parseur : evite une dependance PyYAML avant l'install."""
    text = open(path, encoding="utf-8").read()
    block = re.split(r"^\s*-\s*race_id:\s*", text, flags=re.M)
    for chunk in block[1:]:
        if chunk.split("\n", 1)[0].strip() != race_id:
            continue
        start = re.search(r"window_start_utc:\s*\"?([0-9TZ:-]+)", chunk)
        end = re.search(r"window_end_utc:\s*\"?([0-9TZ:-]+)", chunk)
        if start and end:
            return start.group(1), end.group(1)
    raise SystemExit(f"fenetre introuvable pour {race_id}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--race-id", default="24hultim2026")
    ap.add_argument("--config", default="config/races.yaml")
    args = ap.parse_args()

    start, end = parse_window(args.config, args.race_id)
    now = dt.datetime.now(dt.timezone.utc)
    t0 = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
    t1 = dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
    active = t0 <= now <= t1

    print(f"maintenant {now:%Y-%m-%dT%H:%MZ} / fenetre {t0:%Y-%m-%d} -> {t1:%Y-%m-%d} : "
          f"{'ACTIVE' if active else 'hors fenetre'}")
    with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
        fh.write(f"active={'true' if active else 'false'}\n")


if __name__ == "__main__":
    main()
