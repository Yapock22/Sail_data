"""
Couche d'enrichissement post-course : pings bruts -> table prete pour la
construction de polaires dans sailrating/.

Ce que fait ce script :
  - recalcule COG et SOG depuis lat/lon successifs, independamment du champ
    `heading` du viewer (dont la nature exacte n'est pas tranchee) ;
  - interpole lineairement le vent horaire Open-Meteo au timestamp du ping ;
  - calcule TWA signe et absolu, et VMG ;
  - marque les manoeuvres via la derivee du cap, JAMAIS via la vitesse.

Ce que ce script ne fait pas, volontairement : binning, filtrage MAD,
regression, lissage. Tout cela reste dans sailrating/, sur donnees combinees
multi-courses. Ici on ne produit qu'un CSV d'entree propre.

    python src/enrich.py --race-id 24hultim2026 --model arome
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib

import numpy as np
import pandas as pd

from store import load_race
from weather import build_lookup

OUT = pathlib.Path("data/processed")
R_EARTH_NM = 3440.065
# Seuil de manoeuvre : cf. anti-circularite. Valeur de depart a calibrer
# empiriquement sur les donnees reelles, pas a figer.
TURN_RATE_THRESHOLD_DEG_PER_MIN = 12.0


def bearing_and_distance(lat1, lon1, lat2, lon2):
    """Cap orthodromique (deg) et distance (nm) entre deux positions."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dl = np.radians(lon2 - lon1)
    y = np.sin(dl) * np.cos(p2)
    x = np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dl)
    brg = (np.degrees(np.arctan2(y, x)) + 360) % 360
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    dist = 2 * R_EARTH_NM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    return brg, dist


def angle_diff(a, b):
    """Ecart angulaire signe a-b ramene dans [-180, 180]."""
    return (np.asarray(a) - np.asarray(b) + 180) % 360 - 180


def recompute_kinematics(df: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for hull, grp in df.groupby("hull_id", sort=False):
        g = grp.sort_values("ts").reset_index(drop=True)
        lat, lon = g["lat"].to_numpy(), g["lon"].to_numpy()
        dt_s = g["ts"].diff().dt.total_seconds().to_numpy()
        brg, dist = bearing_and_distance(lat[:-1], lon[:-1], lat[1:], lon[1:])
        g["cog_calc"] = np.concatenate([[np.nan], brg])
        g["sog_calc"] = np.concatenate([[np.nan], dist / (dt_s[1:] / 3600.0)])
        g["dt_s"] = dt_s
        # Derivee du cap : vitesse de rotation, en degres par minute.
        turn = np.concatenate([[np.nan], angle_diff(brg[1:], brg[:-1])])
        turn = np.concatenate([[np.nan], turn])[: len(g)]
        with np.errstate(invalid="ignore", divide="ignore"):
            g["turn_rate"] = np.abs(turn) / (dt_s / 60.0)
        g["is_maneuver"] = g["turn_rate"] > TURN_RATE_THRESHOLD_DEG_PER_MIN
        parts.append(g)
    return pd.concat(parts, ignore_index=True)


def join_wind(df: pd.DataFrame, model: str) -> pd.DataFrame:
    df["cell_lat"] = (df["lat"] / 0.1).round() * 0.1
    df["cell_lon"] = (df["lon"] / 0.1).round() * 0.1
    cells = set(zip(df["cell_lat"].round(2), df["cell_lon"].round(2)))
    start = (df["ts"].min() - dt.timedelta(hours=2)).strftime("%Y-%m-%d")
    end = (df["ts"].max() + dt.timedelta(hours=2)).strftime("%Y-%m-%d")
    print(f"{len(cells)} mailles a interroger ({model}) du {start} au {end}")
    lookup = build_lookup(cells, start, end, model)

    tws, twd = [], []
    for row in df.itertuples():
        data = lookup.get((round(row.cell_lat, 2), round(row.cell_lon, 2)), {}).get("hourly")
        if not data:
            tws.append(np.nan), twd.append(np.nan)
            continue
        times = pd.to_datetime(data["time"], utc=True)
        idx = times.searchsorted(row.ts)
        if idx == 0 or idx >= len(times):
            tws.append(np.nan), twd.append(np.nan)
            continue
        t0, t1 = times[idx - 1], times[idx]
        w = (row.ts - t0).total_seconds() / (t1 - t0).total_seconds()
        s0, s1 = data["wind_speed_10m"][idx - 1], data["wind_speed_10m"][idx]
        d0, d1 = data["wind_direction_10m"][idx - 1], data["wind_direction_10m"][idx]
        if None in (s0, s1, d0, d1):
            tws.append(np.nan), twd.append(np.nan)
            continue
        tws.append(s0 + w * (s1 - s0))
        # Interpolation circulaire de la direction.
        twd.append((d0 + w * ((d1 - d0 + 180) % 360 - 180)) % 360)

    df[f"tws_{model}"] = tws
    df[f"twd_{model}"] = twd
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--race-id", default="24hultim2026")
    ap.add_argument("--model", default="arome", choices=["arome", "ecmwf_ifs", "era5"])
    ap.add_argument("--class", dest="boat_class", default="all",
                    choices=["all", "ULTIM", "OCEAN_FIFTY"])
    args = ap.parse_args()

    pings = load_race(args.race_id)
    if not pings:
        raise SystemExit(f"aucun ping pour {args.race_id}")
    df = pd.DataFrame(pings).drop(columns=["raw_json"])
    if "boat_class" not in df.columns:
        df["boat_class"] = "INCONNU"
    if args.boat_class != "all":
        df = df[df["boat_class"] == args.boat_class]
        if df.empty:
            raise SystemExit(f"aucun ping en classe {args.boat_class}")
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    print(f"{len(df)} pings, {df['hull_id'].nunique()} bateaux, "
          f"{df['ts'].min()} -> {df['ts'].max()}")
    print(df.groupby("boat_class")["hull_id"].nunique().to_string())

    df = recompute_kinematics(df)
    df = join_wind(df, args.model)

    twd, tws = df[f"twd_{args.model}"], df[f"tws_{args.model}"]
    df["twa_signed"] = angle_diff(twd, df["cog_calc"])
    df["twa"] = df["twa_signed"].abs()
    df["tws"] = tws
    df["vmg"] = df["sog_calc"] * np.cos(np.radians(df["twa"]))

    OUT.mkdir(parents=True, exist_ok=True)
    suffix = "" if args.boat_class == "all" else f"_{args.boat_class.lower()}"
    path = OUT / f"{args.race_id}_enriched_{args.model}{suffix}.csv"
    df.drop(columns=["ts", "cell_lat", "cell_lon"]).to_csv(path, index=False)

    usable = df[(~df["is_maneuver"]) & df["tws"].notna() & df["sog_calc"].notna()]
    print(f"\n-> {path}")
    print(f"   {len(usable)}/{len(df)} points exploitables "
          f"({df['is_maneuver'].sum()} manoeuvres, {tws.isna().sum()} sans vent)")
    if not len(usable):
        return

    for cls, grp in usable.groupby("boat_class"):
        print(f"\n   {cls} : {len(grp)} points, {grp['hull_id'].nunique()} bateaux")
        print(f"     TWS {grp['tws'].min():.1f}-{grp['tws'].max():.1f} nds, "
              f"SOG max {grp['sog_calc'].max():.1f} nds")
        bins = pd.cut(grp["twa"], np.arange(0, 181, 20))
        counts = bins.value_counts().sort_index()
        print("     TWA " + "  ".join(
            f"{int(i.left)}-{int(i.right)}:{n}" for i, n in counts.items()))

    # Rapport inter-classes : memes conditions, memes instants. C'est la
    # comparaison qui a le plus de valeur ici, et elle ne demande aucun modele.
    if usable["boat_class"].nunique() > 1:
        pivot = usable.pivot_table(index=pd.cut(usable["twa"], np.arange(0, 181, 20)),
                                   columns="boat_class", values="sog_calc",
                                   aggfunc=lambda s: s.quantile(0.9), observed=False)
        print("\n   SOG au 90e percentile par bin TWA (nds) :")
        print(pivot.round(1).to_string())


if __name__ == "__main__":
    main()
