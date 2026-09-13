"""
Collecte des positions depuis le viewer Geovoile.

Principe : on ne dechiffre pas les fichiers de tracks (binaires chez Geovoile,
deja documente comme impasse sur NYV2024 et Defi Azimut 2024). On laisse le JS
du viewer les decoder, puis on lit l'objet global en memoire. C'est plus lent
qu'un appel JSON mais insensible au format de transport.

Emet des pings au schema brut du projet :
    ping_id, race_id, hull_id, ts_utc, lat, lon, cog, sog, source, raw_json

Aucune transformation metier ici : couche de collecte pure.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import unicodedata

import yaml
from playwright.sync_api import sync_playwright

from store import append_pings

# Extraction cote navigateur. Ecrit en defensif : la structure exacte de `sig`
# n'est verifiee qu'au moment ou l'edition est en ligne, donc on cherche les
# objets par forme (presence d'un .track avec des locations) plutot que par
# chemin fixe.
JS_EXTRACT = r"""
() => {
  function num(v) { return (typeof v === "number" && isFinite(v)) ? v : null; }

  // Geovoile stocke parfois lat/lon en radians. On normalise en degres.
  function toDeg(v) {
    if (v === null) return null;
    return Math.abs(v) <= Math.PI + 1e-9 ? v * 180 / Math.PI : v;
  }

  function readLoc(loc) {
    if (!loc || typeof loc !== "object") return null;
    const lat = toDeg(num(loc.lat ?? loc.latitude ?? loc.y));
    const lon = toDeg(num(loc.lon ?? loc.lng ?? loc.longitude ?? loc.x));
    if (lat === null || lon === null) return null;
    let sog = num(loc.speed ?? loc.sog);
    if (sog === null && typeof loc.getSpeed === "function") {
      try { sog = num(loc.getSpeed()); } catch (e) {}
    }
    return {
      timecode: num(loc.timecode ?? loc.time ?? loc.date),
      lat: lat,
      lon: lon,
      heading: num(loc.heading ?? loc.cap ?? loc.cog),
      sog: sog
    };
  }

  function collectBoats(root) {
    const out = [];
    const seen = new WeakSet();
    (function walk(o, depth) {
      if (!o || typeof o !== "object" || seen.has(o) || depth > 5) return;
      seen.add(o);
      if (o.track && (o.track.locations || o.track.currentLocation)) {
        out.push(o);
        return;
      }
      const keys = Array.isArray(o) ? o.keys() : Object.keys(o);
      for (const k of keys) {
        let v; try { v = o[k]; } catch (e) { continue; }
        walk(v, depth + 1);
      }
    })(root, 0);
    return out;
  }

  // La classe (ULTIM / Ocean Fifty) n'est pas portee par l'objet bateau chez
  // Geovoile : `category` vaut "race" pour tous les engages. Elle vit dans la
  // table tracker._boatClassesByBoatIds, indexee par id de bateau (constate sur
  // l'edition 2025). On interroge d'abord cette table, puis on retombe sur les
  // champs directs si une future edition change de structure.
  function classOf(b) {
    try {
      const m = window.tracker && window.tracker._boatClassesByBoatIds;
      const c = (m && b.id != null) ? m[b.id] : null;
      if (c) return c.name ?? c.shortName ?? c.id ?? null;
    } catch (e) {}
    if (b.boatClass && (b.boatClass.name ?? b.boatClass.id))
      return b.boatClass.name ?? b.boatClass.id;
    return b.class ?? null;
  }

  let root = null;
  for (const name of ["sig", "geovoile", "gv", "race", "tracker", "app", "viewer"]) {
    if (window[name] && typeof window[name] === "object") { root = window[name]; break; }
  }
  if (!root) return { error: "objet global introuvable", globals: Object.keys(window).slice(0, 200) };

  const boats = collectBoats(root);
  return {
    scraped_at: Date.now(),
    boats: boats.map(b => {
      const t = b.track || {};
      const locs = t.locations || t.points || [];
      return {
        id: b.id ?? null,
        name: b.name ?? b.boatName ?? null,
        sail: b.sail ?? b.sailNumber ?? null,
        class: classOf(b),
        status: b.status ?? null,
        current: readLoc(t.currentLocation),
        track: Array.from(locs).map(readLoc).filter(Boolean)
      };
    })
  };
}
"""


def ping_id(race_id: str, hull_id: str, ts_utc: str) -> str:
    return hashlib.sha1(f"{race_id}|{hull_id}|{ts_utc}".encode()).hexdigest()[:16]


def slugify(name: str) -> str:
    # Les noms de bateaux sont accentues ; un hull_id doit rester ASCII, il sert
    # de cle et de fragment de nom de fichier.
    plain = unicodedata.normalize("NFKD", name or "inconnu")
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    keep = "".join(c.lower() if c.isascii() and c.isalnum() else "_" for c in plain)
    return "_".join(filter(None, keep.split("_")))


def to_iso(timecode: float | None, scraped_at_ms: float) -> str | None:
    """Geovoile emet des timecodes en secondes epoch (parfois en ms)."""
    if timecode is None:
        return None
    tc = float(timecode)
    if tc > 1e11:  # millisecondes
        tc /= 1000.0
    if tc < 1e9 or tc > 4e9:  # pas une epoch plausible -> on jette
        return None
    return dt.datetime.fromtimestamp(tc, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_class(raw: str | None) -> str:
    """Le viewer nomme les classes librement ; on ramene a deux cles stables."""
    s = (raw or "").upper().replace("-", " ").replace("_", " ")
    if "ULTIM" in s:
        return "ULTIM"
    if "FIFTY" in s or "50" in s or "OCEAN" in s:
        return "OCEAN_FIFTY"
    return "INCONNU"


def build_pings(payload: dict, race_id: str, hull_map: dict, source_url: str) -> list[dict]:
    pings: list[dict] = []
    scraped_at = payload.get("scraped_at", 0)
    for boat in payload.get("boats", []):
        name = boat.get("name")
        boat_class = normalize_class(boat.get("class"))
        hull_id = hull_map.get(name) or slugify(name)
        locs = list(boat.get("track") or [])
        if boat.get("current"):
            locs.append(boat["current"])
        for loc in locs:
            ts = to_iso(loc.get("timecode"), scraped_at)
            if ts is None:
                continue
            pings.append(
                {
                    "ping_id": ping_id(race_id, hull_id, ts),
                    "race_id": race_id,
                    "hull_id": hull_id,
                    "boat_class": boat_class,
                    "ts_utc": ts,
                    "lat": loc["lat"],
                    "lon": loc["lon"],
                    # NB : champ `heading` du viewer. Tant que la nature exacte
                    # (COG calcule vs cap compas) n'est pas tranchee, on le
                    # stocke en cog mais on recalculera le COG depuis lat/lon
                    # a l'enrichissement. Cf. principles-and-methodology.
                    "cog": loc.get("heading"),
                    "sog": loc.get("sog"),
                    "source": "geovoile_viewer",
                    "raw_json": json.dumps(
                        {"boat_id": boat.get("id"), "boat_name": name,
                         "class": boat.get("class"), "status": boat.get("status"),
                         "url": source_url, "loc": loc},
                        ensure_ascii=False,
                    ),
                }
            )
    return pings


def scrape(url: str, race_id: str, hull_map: dict, settle_s: int) -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(url, wait_until="networkidle", timeout=120_000)
        page.wait_for_timeout(settle_s * 1000)
        payload = page.evaluate(JS_EXTRACT)
        browser.close()
    if payload.get("error"):
        raise RuntimeError(f"extraction impossible : {payload['error']}")
    return build_pings(payload, race_id, hull_map, url)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--race-id", default="24hultim2026")
    ap.add_argument("--config", default="config/races.yaml")
    ap.add_argument("--settle", type=int, default=20, help="attente apres chargement (s)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    race = next(r for r in cfg["races"] if r["race_id"] == args.race_id)
    hull_map = {k: v for cls in cfg.get("hull_map", {}).values() for k, v in cls.items()}

    urls = [race["viewer_url"], *race.get("fallback_urls", [])]
    last_exc: Exception | None = None
    for url in urls:
        try:
            pings = scrape(url, args.race_id, hull_map, args.settle)
            if not pings:
                raise RuntimeError("0 ping extrait")
            print(f"{len(pings)} pings depuis {url}")

            # Inventaire par classe : sert a remplir hull_map avec les noms
            # exacts du viewer plutot qu'avec la liste des engages.
            inventory: dict[str, dict[str, int]] = {}
            for ping in pings:
                raw = json.loads(ping["raw_json"])
                key = f"{ping['boat_class']} | {raw['boat_name']} -> {ping['hull_id']}"
                inventory.setdefault(ping["boat_class"], {})
                inventory[ping["boat_class"]][key] = \
                    inventory[ping["boat_class"]].get(key, 0) + 1
            for cls in sorted(inventory):
                print(f"\n  {cls} ({len(inventory[cls])} bateaux)")
                for key, count in sorted(inventory[cls].items()):
                    print(f"    {count:>4} pts  {key.split('| ', 1)[1]}")

            if args.dry_run:
                print("\n" + json.dumps(pings[:2], indent=2, ensure_ascii=False))
                return 0
            added = append_pings(pings, args.race_id)
            print(f"{added} nouveaux pings ecrits ({len(pings) - added} doublons ignores)")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"echec sur {url} : {exc}", file=sys.stderr)
            last_exc = exc
    raise SystemExit(f"toutes les URL ont echoue : {last_exc}")


if __name__ == "__main__":
    raise SystemExit(main())
