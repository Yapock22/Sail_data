"""
Recuperation du vent Open-Meteo, en deux passes volontairement distinctes.

  - AROME France HD (~1.3 km) via historical-forecast-api : disponible
    immediatement apres la course, et c'est la bonne resolution pour une
    boucle cotiere dans les courreaux de Groix et le golfe de Gascogne.
    ERA5 a 0.25 deg lisse completement les effets de site a cet endroit.
  - ERA5 via archive-api : disponible a J+5 environ, sert de reference
    croisee et garde la coherence avec le reste du projet sailrating.

Les deux passes sont ecrites dans des colonnes separees ; on ne fusionne
jamais silencieusement. Si un modele ne couvre pas un point, la valeur reste
nulle et le ping est ecarte a l'analyse (pas de fallback muet).
"""

from __future__ import annotations

import json
import pathlib
import time

import requests

CACHE = pathlib.Path("data/processed/.weather_cache")
ENDPOINTS = {
    "arome": ("https://historical-forecast-api.open-meteo.com/v1/forecast",
              "meteofrance_arome_france_hd"),
    "ecmwf_ifs": ("https://historical-forecast-api.open-meteo.com/v1/forecast",
                  "ecmwf_ifs025"),
    "era5": ("https://archive-api.open-meteo.com/v1/archive", None),
}
HOURLY = "wind_speed_10m,wind_direction_10m,wind_gusts_10m"


def _cache_path(key: str) -> pathlib.Path:
    return CACHE / f"{key}.json"


def fetch_grid(lat: float, lon: float, start: str, end: str, model: str) -> dict:
    """Serie horaire pour une maille. `start`/`end` au format YYYY-MM-DD."""
    CACHE.mkdir(parents=True, exist_ok=True)
    key = f"{model}_{lat:.2f}_{lon:.2f}_{start}_{end}"
    path = _cache_path(key)
    if path.exists():
        return json.loads(path.read_text())

    url, model_param = ENDPOINTS[model]
    params = {
        "latitude": round(lat, 2),
        "longitude": round(lon, 2),
        "start_date": start,
        "end_date": end,
        "hourly": HOURLY,
        "wind_speed_unit": "kn",
        "timezone": "UTC",
    }
    if model_param:
        params["models"] = model_param

    for attempt in range(5):
        resp = requests.get(url, params=params, timeout=60)
        if resp.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        resp.raise_for_status()
        data = resp.json()
        path.write_text(json.dumps(data))
        return data
    raise RuntimeError(f"Open-Meteo indisponible pour {key}")


def build_lookup(cells: set[tuple[float, float]], start: str, end: str,
                 model: str) -> dict[tuple[float, float], dict]:
    """Une requete par maille de 0.1 deg. Sur une boucle de 500 nm, cela
    represente typiquement quelques dizaines d'appels."""
    out = {}
    for i, (lat, lon) in enumerate(sorted(cells), 1):
        out[(lat, lon)] = fetch_grid(lat, lon, start, end, model)
        print(f"  [{i}/{len(cells)}] {model} {lat:.2f},{lon:.2f}")
        time.sleep(0.2)
    return out
