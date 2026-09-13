# ultim24-tracker

Collecte autonome des positions des 24H Ultim 2026 (Lorient, 23-27 septembre)
et enrichissement meteo, en vue d'extrapoler des polaires routees.

Tourne entierement sur GitHub Actions : rien a laisser allume a la maison.
Le depot lui-meme sert de stockage et d'historique.

## Architecture

Deux couches strictement separees, comme dans `sailrating/`.

**Collecte** (`collect.yml`, toutes les 10 min pendant la fenetre) : charge le
viewer Geovoile dans un Chromium headless, lit l'objet JS une fois les traces
decodees par le viewer, ecrit des pings bruts en JSONL et les commite.

**Enrichissement** (`enrich.yml`, manuel, apres la course) : recalcule COG/SOG
depuis les positions, joint le vent Open-Meteo, produit un CSV avec TWA/TWS.

Le schema des pings bruts est celui du projet : `ping_id`, `race_id`,
`hull_id`, `ts_utc`, `lat`, `lon`, `cog`, `sog`, `source`, `raw_json`.
Deduplication sur `(hull_id, ts_utc)`.

Aucun binning, filtrage MAD, regression ni lissage ici. Ce depot produit une
entree propre ; la construction de polaires reste dans `sailrating/`, sur
donnees combinees multi-courses.

## Pourquoi Playwright et pas un appel JSON

Les endpoints Geovoile renvoient du binaire chiffre (deja constate sur NYV2024
et Defi Azimut 2024). Plutot que de le dechiffrer, on laisse le JS du viewer
faire le travail et on lit le resultat en memoire. C'est plus lent mais
insensible au format de transport.

`src/probe.py` sert a verifier si cette edition fait exception : il intercepte
tout le trafic reseau et dump la forme de l'objet global. Si une URL renvoie du
JSON lisible, supprimer Playwright et passer sur `requests` (environ 10x moins
cher en minutes Actions, et beaucoup moins fragile).

```bash
pip install -r requirements.txt && python -m playwright install chromium
python src/probe.py --url https://24hultim.geovoile.com/2025/viewer/
```

A lancer d'abord sur l'edition 2025 (structure identique, donnees figees), puis
sur la 2026 des sa mise en ligne.

## Mise en route

1. Depot **public** : minutes Actions illimitees. En prive, la collecte sur
   5 jours consommerait une part serieuse du quota gratuit.
2. Settings > Actions > General > Workflow permissions : *Read and write*.
3. `config/races.yaml` : confirmer `viewer_url`. Le pattern a change entre
   editions (`/2023/tracker/`, `/2024/viewer/`, `/2025/viewer/`) : a verifier
   la veille sur la page cartographie d'ultimsailing.com.
4. Completer `hull_map` avec la liste officielle des engages (4 ULTIM,
   11 Ocean Fifty). Les noms sont matches exactement ; un nom absent tombe sur
   un slug automatique, recuperable mais moins propre.
5. Repasser le workflow une fois en `workflow_dispatch` pour valider la chaine
   avant le depart.

Test local sans ecriture :

```bash
PYTHONPATH=src python src/geovoile_scrape.py --race-id 24hultim2026 --dry-run
```

## Enrichissement meteo

Deux passes, ecrites dans des colonnes distinctes, jamais fusionnees.

- **AROME France HD** (~1.3 km, disponible immediatement) : la bonne resolution
  pour une boucle cotiere. ERA5 a 0.25 deg lisse completement les effets de
  site dans les courreaux de Groix.
- **ERA5** (J+5) : reference croisee, coherence avec le reste de `sailrating/`.

Un ecart systematique entre les deux sur les memes points est en soi une
mesure : il donne l'ordre de grandeur de l'erreur meteo qui se propage dans les
polaires construites sur ERA5.

```bash
PYTHONPATH=src python src/enrich.py --race-id 24hultim2026 --model arome
PYTHONPATH=src python src/enrich.py --race-id 24hultim2026 --model era5
```

## Les deux classes, et pourquoi les Ocean Fifty comptent plus

Les 4 ULTIM et les 11 Ocean Fifty sont sur la meme carto, avec des heures de
depart distinctes par classe (le viewer expose `boatClass.run.date`). Le
scraper collecte les deux sans configuration : il balaie l'objet global et
retient tout ce qui porte une trace. `boat_class` est un champ de premier rang
dans le schema, normalise en `ULTIM` / `OCEAN_FIFTY`.

L'interet n'est pas d'avoir 15 bateaux au lieu de 4, c'est la structure de la
flotte. Les 4 ULTIM sont des prototypes uniques, sans point commun de carene :
impossible d'en tirer une cohorte, chaque polaire est un cas isole et on
retombe sur la circularite un skipper / une coque. Les Ocean Fifty sont une
jauge de boite avec des sisterships et des generations identifiables. C'est la
seule des deux classes ou l'estimation de polaire par cohorte, que la
methodologie du projet impose pour sortir de la circularite, est reellement
applicable.

S'ajoute une comparaison qui ne demande aucun modele : meme parcours, memes
instants, meme eau, meme vent. La dispersion des SOG entre 11 bateaux dans des
conditions identiques donne une mesure directe du plancher de bruit
skipper + routage, celui qu'un SPI doit depasser pour signifier quelque chose.
`enrich.py` sort ce tableau (SOG au 90e percentile par bin TWA et par classe)
sans autre traitement.

```bash
PYTHONPATH=src python src/enrich.py --race-id 24hultim2026 --class OCEAN_FIFTY
PYTHONPATH=src python src/enrich.py --race-id 24hultim2026 --class all
```

Remplir `hull_map` apres un `--dry-run`, pas depuis la liste des engages : le
scraper imprime l'inventaire exact par classe avec le slug applique a chaque
nom. Un bateau non mappe est collecte quand meme.

## Limites a garder en tete

**Cadence.** Le viewer public affiche un point toutes les 15 min, soit environ
96 points par bateau sur 24h. Les balises Ocean Tracking emettent beaucoup plus
finement (jusqu'a 5 s), mais ce flux n'est pas expose. Avec 4 ULTIM, on parle de
quelques centaines de points exploitables : de quoi valider la chaine, pas de
quoi construire une polaire.

**Courant.** Parcours cotier de 500 nm dans le golfe de Gascogne, avec un
courant de maree de 1 a 3 noeuds. Le SOG n'est donc pas la vitesse surface. A
30 noeuds cela reste 3 a 10 % d'erreur, non aleatoire et correlee a la phase de
maree, donc au cap. C'est exactement le type de biais qui deforme une polaire
plutot que de l'elargir. Une correction par atlas de courant (SHOM) serait a
prevoir avant d'utiliser ces donnees autrement qu'en test.

Ce biais est mecaniquement pire sur les Ocean Fifty : a 20 noeuds au lieu de 35,
les memes 2 noeuds de courant pesent deux fois plus. Le gain de cohorte se paie
donc par une erreur relative superieure. Les deux effets ne s'annulent pas et
ne se comparent pas directement : c'est la raison d'etre du recoupement
AROME / ERA5 et, a terme, de la correction de courant.

**Champ `heading`.** La nature du champ du viewer (COG calcule ou cap compas)
n'est pas tranchee. Il est stocke tel quel en `cog`, mais l'enrichissement
recalcule systematiquement `cog_calc` depuis les positions successives.
N'utiliser que `cog_calc` en aval.

**Filtrage de manoeuvres.** Fait sur la derivee du cap, jamais sur la vitesse.
Le seuil de 12 deg/min est une valeur de depart a calibrer empiriquement ; a
15 min d'echantillonnage il est de toute facon largement aveugle.

**Crons GitHub.** Les crons planifies derivent de 5 a 20 min sous charge, et
peuvent sauter. Le pas de 10 min et la deduplication absorbent la derive. Si le
viewer sert la trace complete a chaque chargement, un seul passage reussi
suffit d'ailleurs a tout rattraper : la collecte continue est surtout une
assurance contre une coupure ou un retrait du site.

## Ce que ce test valide reellement

Moins une polaire qu'une chaine : URL -> extraction -> schema -> stockage ->
join meteo -> CSV, avec un evenement court et un volume faible. Si elle tient
sur 4 bateaux et 24h, elle tiendra sur la Route du Rhum en novembre, ou le
volume et la duree rendent une panne beaucoup plus couteuse.
