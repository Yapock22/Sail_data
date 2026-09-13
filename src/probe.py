"""
Reconnaissance du viewer Geovoile.

A lancer UNE FOIS avant la course (sur l'edition 2025, puis sur la 2026 des
qu'elle est en ligne). Objectif : repondre a deux questions.

  1. Quelles URL reseau le viewer appelle-t-il ? Si l'une d'elles renvoie du
     JSON lisible, on supprime Playwright et on passe sur un simple requests
     (10x moins cher en minutes Actions, zero fragilite navigateur).
  2. Quelle est la forme de l'objet JS global (`sig`) une fois la carto
     chargee ? C'est la que se trouvent les traces deja decodees, ce qui
     contourne l'encodage binaire des fichiers de tracks.

Sortie : probe_out/network.json, probe_out/js_shape.json, probe_out/*.bin

    python src/probe.py --url https://24hultim.geovoile.com/2025/viewer/
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re

from playwright.sync_api import sync_playwright

OUT = pathlib.Path("probe_out")

# Extrait la forme de l'objet global sans tout serialiser (les traces peuvent
# peser plusieurs Mo). On descend en profondeur limitee et on note les types.
JS_SHAPE = r"""
() => {
  const seen = new WeakSet();
  function shape(v, depth) {
    if (v === null) return "null";
    const t = typeof v;
    if (t !== "object" && t !== "function") return t;
    if (t === "function") return "fn";
    if (seen.has(v)) return "<cycle>";
    seen.add(v);
    if (Array.isArray(v)) {
      if (v.length === 0) return "[]";
      return { __array_len: v.length, __sample: depth > 0 ? shape(v[0], depth - 1) : "..." };
    }
    if (depth <= 0) return "{...}";
    const o = {};
    for (const k of Object.keys(v).slice(0, 60)) {
      try { o[k] = shape(v[k], depth - 1); } catch (e) { o[k] = "<err>"; }
    }
    return o;
  }
  const globals = Object.keys(window).filter(
    k => !k.startsWith("webkit") && typeof window[k] === "object" && window[k] !== null
  );
  const out = { __globals: globals.slice(0, 200) };
  for (const name of ["sig", "geovoile", "gv", "race", "tracker", "app", "viewer"]) {
    if (window[name] !== undefined) out[name] = shape(window[name], 4);
  }
  return out;
}
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--wait", type=int, default=25, help="secondes d'observation")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    calls: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page()

        def on_response(resp):
            try:
                ct = resp.headers.get("content-type", "")
                entry = {
                    "url": resp.url,
                    "status": resp.status,
                    "content_type": ct,
                    "method": resp.request.method,
                }
                # On ne garde que ce qui ressemble a de la donnee.
                if re.search(r"\.(js|css|png|jpg|jpeg|svg|woff2?|ico)(\?|$)", resp.url):
                    entry["skipped"] = True
                    calls.append(entry)
                    return
                body = resp.body()
                entry["bytes"] = len(body)
                head = body[:64]
                entry["head_hex"] = head.hex()
                try:
                    entry["head_text"] = head.decode("utf-8")
                    entry["looks_text"] = True
                except UnicodeDecodeError:
                    entry["looks_text"] = False
                # Dump complet des payloads de donnees pour analyse hors ligne.
                if len(body) > 200:
                    fname = re.sub(r"[^A-Za-z0-9._-]", "_", resp.url.split("/")[-1])[:80]
                    (OUT / f"{resp.status}_{fname or 'payload'}.bin").write_bytes(body)
                calls.append(entry)
            except Exception as exc:  # noqa: BLE001
                calls.append({"url": resp.url, "error": str(exc)})

        page.on("response", on_response)
        page.goto(args.url, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(args.wait * 1000)

        shape = page.evaluate(JS_SHAPE)
        (OUT / "js_shape.json").write_text(json.dumps(shape, indent=2, ensure_ascii=False))
        (OUT / "network.json").write_text(json.dumps(calls, indent=2, ensure_ascii=False))
        browser.close()

    data_calls = [c for c in calls if not c.get("skipped") and c.get("bytes", 0) > 200]
    print(f"{len(calls)} requetes, dont {len(data_calls)} candidates donnees :\n")
    for c in sorted(data_calls, key=lambda x: -x.get("bytes", 0))[:25]:
        kind = "TEXTE" if c.get("looks_text") else "BINAIRE"
        print(f"  [{kind:7}] {c.get('bytes'):>9} o  {c['url']}")
    print(f"\nDetail : {OUT}/network.json et {OUT}/js_shape.json")


if __name__ == "__main__":
    main()
