"""
import_binance.py — Importe l'historique du sommet de carnet publié par Binance.

Binance publie sur data.binance.vision un fichier `bookTicker` par jour et par
symbole : chaque modification du meilleur bid ou du meilleur ask, avec les
tailles. Soit ~26,6 millions de lignes par jour, à 3 ms de résolution médiane.
Ce n'est pas documenté dans leur README, mais les fichiers existent.

Ça donne exactement l'entrée du microprice — le signal de confirmation retenu —
sur ~320 jours (mai 2023 → mars 2024), là où la collecte maison n'a qu'un jour.

CE QUE CES DONNÉES NE CONTIENNENT PAS
    Le sommet du carnet, et rien d'autre : ni profondeur, ni murs. Les colonnes
    obi_5..obi_50 et les murs sont donc écrits à zéro. Un zéro ressemblant à une
    valeur valide, un marqueur `_source.json` est déposé dans le dossier et le
    backtest refuse de tourner si le signal configuré n'est pas disponible.

LIMITES À GARDER EN TÊTE
    - perpétuels Binance, pas Kraken : mêmes frais (0.05 %), microstructure
      proche mais pas identique ;
    - l'historique s'arrête en mars 2024, soit un régime de marché ancien ;
    - ~300 Mo par jour à télécharger, sous-échantillonnés au vol puis effacés.

Usage :
  python import_binance.py --du 2024-03-01 --au 2024-03-15
  python import_binance.py --du 2024-03-01 --au 2024-03-31 --heures 8-16
"""

import argparse
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone

import collector
import config as C
import features

VENUE   = "binance_hist"
SYMBOLE = "BTCUSDT"
BASE    = ("https://data.binance.vision/data/futures/um/daily/bookTicker/"
           "{sym}/{sym}-bookTicker-{jour}.zip")

# Bornes reelles du jeu de donnees, verifiees sur le stockage.
DISPO_DEBUT = date(2023, 5, 16)
DISPO_FIN   = date(2024, 3, 30)

# Colonnes du fichier Binance
I_BID, I_BID_Q, I_ASK, I_ASK_Q, I_TS = 1, 2, 3, 4, 5

COLONNES_REELLES = ["ts", "best_bid", "best_ask", "mid", "microprice",
                    "spread_bps"]


def marqueur(dossier):
    """
    Déclare ce que ces données contiennent réellement.

    Sans ça, rien ne distingue un `obi_10` à zéro faute de donnée d'un `obi_10`
    à zéro parce que le carnet est équilibré. Le backtest lit ce fichier et
    refuse un signal indisponible plutôt que de produire un résultat faux.
    """
    os.makedirs(dossier, exist_ok=True)
    with open(os.path.join(dossier, "_source.json"), "w", encoding="utf-8") as f:
        json.dump({
            "source": "data.binance.vision / futures/um/daily/bookTicker",
            "symbole": SYMBOLE,
            "colonnes_reelles": COLONNES_REELLES,
            "colonnes_absentes": [c for c in features.COLUMNS
                                  if c not in COLONNES_REELLES],
            "signaux_utilisables": ["mpi"],
            "note": ("Sommet de carnet uniquement. Profondeur et murs sont "
                     "ecrits a zero et ne doivent pas etre interpretes."),
        }, f, indent=2, ensure_ascii=False)


def telecharger(jour, dest):
    url = BASE.format(sym=SYMBOLE, jour=jour)
    req = urllib.request.Request(url, headers={"User-Agent": "orderbook-bot"})
    with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
        total = 0
        while True:
            bloc = r.read(1 << 20)
            if not bloc:
                break
            f.write(bloc)
            total += len(bloc)
            print(f"\r    telechargement {total/1e6:.0f} Mo", end="", flush=True)
    print(f"\r    telecharge {total/1e6:.0f} Mo            ")
    return total


def ligne_depuis(bb, bq, ba, aq, ts_ms):
    """
    Reconstitue une ligne au format du collecteur.

    Le microprice est calcule ici de la meme facon que dans features.compute,
    pour que les donnees importees et collectees soient interchangeables.
    """
    mid = (bb + ba) / 2
    tot = bq + aq
    micro = ((bb * aq + ba * bq) / tot) if tot > 0 else mid
    r = {
        "ts": round(ts_ms / 1000, 2),
        "best_bid": bb, "best_ask": ba,
        "mid": round(mid, 2),
        "microprice": round(micro, 4),
        "spread_bps": round((ba - bb) / mid * 10_000, 3),
    }
    for bande in C.DEPTH_BANDS_BPS:       # indisponibles : voir _source.json
        r[f"bid_{bande}"] = r[f"ask_{bande}"] = r[f"obi_{bande}"] = 0.0
    for cote in ("bid", "ask"):
        r[f"{cote}_wall_px"] = r[f"{cote}_wall_sz"] = r[f"{cote}_wall_bps"] = 0.0
    return r


def importer_jour(jour, pas, heures=None, garder=False):
    """Télécharge un jour, sous-échantillonne, écrit au format du collecteur."""
    tmp = tempfile.mkdtemp(prefix="binhist_")
    zip_path = os.path.join(tmp, f"{jour}.zip")
    try:
        try:
            telecharger(jour, zip_path)
        except urllib.error.HTTPError as e:
            print(f"    indisponible (HTTP {e.code})")
            return 0

        # Un seul instantané par tranche de `pas` secondes : le DERNIER état
        # connu de la tranche, ce que le collecteur live observe aussi.
        retenus, bucket_courant, dernier = [], None, None
        lues = 0
        with zipfile.ZipFile(zip_path) as z:
            nom = z.namelist()[0]
            with z.open(nom) as brut:
                flux = io.TextIOWrapper(brut, encoding="utf-8", newline="")
                lecteur = csv.reader(flux)
                next(lecteur, None)                      # en-tete
                for c in lecteur:
                    lues += 1
                    try:
                        ts_ms = int(c[I_TS])
                    except (IndexError, ValueError):
                        continue
                    if heures:
                        h = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).hour
                        if not (heures[0] <= h < heures[1]):
                            continue
                    b = ts_ms // int(pas * 1000)
                    if bucket_courant is None:
                        bucket_courant = b
                    elif b != bucket_courant:
                        if dernier:
                            retenus.append(dernier)
                        bucket_courant, dernier = b, None
                    try:
                        dernier = ligne_depuis(float(c[I_BID]), float(c[I_BID_Q]),
                                               float(c[I_ASK]), float(c[I_ASK_Q]),
                                               ts_ms)
                    except (IndexError, ValueError):
                        dernier = None
                    if lues % 4_000_000 == 0:
                        print(f"\r    lecture {lues/1e6:.0f}M lignes -> "
                              f"{len(retenus):,} instantanes", end="", flush=True)
        if dernier:
            retenus.append(dernier)
        print(f"\r    {lues/1e6:.1f}M lignes -> {len(retenus):,} instantanes"
              f" ({pas}s)                    ")

        buf = list(retenus)
        while buf:
            lot, buf = buf[:C.FLUSH_EVERY], buf[C.FLUSH_EVERY:]
            collector.flush(VENUE, lot)
        return len(retenus)
    finally:
        if not garder:
            shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--du", required=True, help="AAAA-MM-JJ inclus")
    ap.add_argument("--au", required=True, help="AAAA-MM-JJ inclus")
    ap.add_argument("--pas", type=float, default=C.VENUES[VENUE]["interval"],
                    help="secondes entre deux instantanes")
    ap.add_argument("--heures", help="restreindre a une plage UTC, ex 8-16")
    args = ap.parse_args()

    d0 = datetime.strptime(args.du, "%Y-%m-%d").date()
    d1 = datetime.strptime(args.au, "%Y-%m-%d").date()
    if d0 > d1:
        raise SystemExit("--du doit preceder --au")
    if d0 < DISPO_DEBUT or d1 > DISPO_FIN:
        print(f"Attention : le jeu de donnees couvre {DISPO_DEBUT} -> {DISPO_FIN}. "
              f"Les jours hors bornes seront ignores.")

    heures = None
    if args.heures:
        a, _, b = args.heures.partition("-")
        heures = (int(a), int(b))

    marqueur(os.path.join(C.DATA_DIR, VENUE))
    total, jours = 0, 0
    debut = time.time()
    j = d0
    while j <= d1:
        print(f"  {j}")
        n = importer_jour(j.isoformat(), args.pas, heures)
        total += n
        jours += 1 if n else 0
        j += timedelta(days=1)

    ecoule = time.time() - debut
    print(f"\n{total:,} instantanes importes sur {jours} jour(s) "
          f"en {ecoule/60:.1f} min")
    print(f"-> {C.DATA_DIR}/{VENUE}/")
    if total:
        print(f"\nBacktest : python backtest.py --venue {VENUE}")


if __name__ == "__main__":
    sys.exit(main())
