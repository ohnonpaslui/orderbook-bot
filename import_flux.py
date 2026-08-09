"""
import_flux.py — Flux d'ordres reconstruit à partir des transactions publiques.

CE QU'ON PEUT ET NE PEUT PAS RECONSTRUIRE

Les ordres en attente — le carnet proprement dit — ne laissent aucune trace
dans les transactions : impossible de les retrouver. En revanche chaque
transaction publique indique QUI ÉTAIT L'AGRESSEUR, via le champ
`isBuyerMaker` : si l'acheteur était le teneur de marché, c'est le vendeur
qui a frappé. On reconstitue donc le flux d'ordres exact, et c'est lui que
regardent les scalpeurs — delta, CVD, absorption, footprint.

POURQUOI C'EST MIEUX QUE LE CARNET, ICI

  carnet (bookTicker) : abandonné depuis mars 2024, 300 Mo/jour
  transactions        : disponible jusqu'à hier, depuis fin 2019, 25 Mo/jour

Six ans d'historique gratuit, contre dix mois périmés. Et la licence CME
équivalente coûterait 290 $/mois sans le moindre historique.

Chaque barre agrège : prix OHLC, volume agressif à l'achat et à la vente,
nombre de transactions, et la même décomposition restreinte aux grosses
transactions — celles qui portent l'information institutionnelle.

Usage :
  python import_flux.py --du 2026-07-01 --au 2026-07-31
  python import_flux.py --du 2026-06-01 --au 2026-07-31 --barre 300
"""

import argparse
import csv
import io
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

SYMBOLE = "BTCUSDT"
URL = ("https://data.binance.vision/data/futures/um/daily/aggTrades/"
       "{sym}/{sym}-aggTrades-{jour}.zip")
SORTIE = os.path.join("data", "flux")

# Seuils de notionnel, en dollars. Un ordre de 200 000 $ ne porte pas la même
# information qu'un de 500 : les séparer est le fondement du footprint.
GROS = 100_000.0
MOYEN = 10_000.0

COLONNES = ["ts", "open", "high", "low", "close", "volume",
            "vol_achat", "vol_vente", "n_trades", "n_achat",
            "gros_achat", "gros_vente", "moyen_achat", "moyen_vente",
            "trade_max"]

# Champs du fichier Binance
I_PRIX, I_QTE, I_TS, I_MAKER = 1, 2, 5, 6


def telecharger(jour, dest, essais=4):
    """
    Télécharge un jour, avec réessais bornés.

    Sans réessai, une coupure DNS passagère fait perdre le jour entier : lors
    du premier import, 58 jours sur 93 ont été perdus ainsi, ce qui a réduit
    l'échantillon indépendant de 1 100 à 420 observations et rendu le test
    incapable de trancher. Une panne réseau ne doit pas se confondre avec une
    absence de données.
    """
    req = urllib.request.Request(URL.format(sym=SYMBOLE, jour=jour),
                                 headers={"User-Agent": "orderbook-bot"})
    for k in range(1, essais + 1):
        try:
            with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
                total = 0
                while True:
                    bloc = r.read(1 << 20)
                    if not bloc:
                        break
                    f.write(bloc)
                    total += len(bloc)
            return total
        except urllib.error.HTTPError:
            raise                          # 404 : le jour n'existe pas, inutile d'insister
        except Exception as e:
            if k == essais:
                raise
            print(f"      reseau ({type(e).__name__}), essai {k}/{essais}",
                  flush=True)
            time.sleep(3 * k)


def agreger(zip_path, barre_sec):
    """Agrège les transactions en barres, avec la décomposition du flux."""
    barres = {}
    lues = 0
    with zipfile.ZipFile(zip_path) as z:
        with z.open(z.namelist()[0]) as brut:
            flux = io.TextIOWrapper(brut, encoding="utf-8", newline="")
            lecteur = csv.reader(flux)
            premiere = next(lecteur, None)
            # Certains fichiers ont un en-tete, d'autres non.
            if premiere and not premiere[0].lstrip("-").isdigit():
                premiere = None
            for c in ([premiere] if premiere else []) + list(lecteur):
                if not c or len(c) <= I_MAKER:
                    continue
                lues += 1
                try:
                    prix = float(c[I_PRIX])
                    qte = float(c[I_QTE])
                    ts = int(c[I_TS])
                except ValueError:
                    continue
                # isBuyerMaker vrai => l'acheteur subissait => VENTE agressive
                vente = c[I_MAKER].strip().lower() in ("true", "1")
                notionnel = prix * qte
                cle = ts // 1000 // barre_sec * barre_sec

                b = barres.get(cle)
                if b is None:
                    b = barres[cle] = {
                        "ts": cle, "open": prix, "high": prix, "low": prix,
                        "close": prix, "volume": 0.0, "vol_achat": 0.0,
                        "vol_vente": 0.0, "n_trades": 0, "n_achat": 0,
                        "gros_achat": 0.0, "gros_vente": 0.0,
                        "moyen_achat": 0.0, "moyen_vente": 0.0,
                        "trade_max": 0.0}
                b["high"] = max(b["high"], prix)
                b["low"] = min(b["low"], prix)
                b["close"] = prix
                b["volume"] += qte
                b["n_trades"] += 1
                b["trade_max"] = max(b["trade_max"], notionnel)
                if vente:
                    b["vol_vente"] += qte
                    if notionnel >= GROS:
                        b["gros_vente"] += qte
                    elif notionnel >= MOYEN:
                        b["moyen_vente"] += qte
                else:
                    b["vol_achat"] += qte
                    b["n_achat"] += 1
                    if notionnel >= GROS:
                        b["gros_achat"] += qte
                    elif notionnel >= MOYEN:
                        b["moyen_achat"] += qte
    return barres, lues


def ecrire(barres, chemin):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    neuf = not os.path.exists(chemin)
    with open(chemin, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLONNES)
        if neuf:
            w.writeheader()
        for cle in sorted(barres):
            w.writerow({k: round(v, 8) if isinstance(v, float) else v
                        for k, v in barres[cle].items()})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--du", required=True)
    ap.add_argument("--au", required=True)
    ap.add_argument("--barre", type=int, default=300, help="secondes par barre")
    args = ap.parse_args()

    d0 = datetime.strptime(args.du, "%Y-%m-%d").date()
    d1 = datetime.strptime(args.au, "%Y-%m-%d").date()
    chemin = os.path.join(SORTIE, f"{SYMBOLE}_{args.barre}s.csv")
    if os.path.exists(chemin):
        os.remove(chemin)

    total_barres, total_trades, jours = 0, 0, 0
    debut = time.time()
    j = d0
    while j <= d1:
        tmp = tempfile.mkdtemp(prefix="flux_")
        zp = os.path.join(tmp, "j.zip")
        try:
            taille = telecharger(j.isoformat(), zp)
            barres, lues = agreger(zp, args.barre)
            ecrire(barres, chemin)
            total_barres += len(barres)
            total_trades += lues
            jours += 1
            ecoule = time.time() - debut
            print(f"  {j}  {taille/1e6:>5.0f} Mo  {lues/1e6:>5.2f}M trades  "
                  f"-> {len(barres):>4} barres  ({ecoule/60:.0f} min ecoulees)",
                  flush=True)
        except urllib.error.HTTPError as e:
            print(f"  {j}  indisponible (HTTP {e.code})", flush=True)
        except Exception as e:
            print(f"  {j}  echec : {type(e).__name__}: {str(e)[:80]}", flush=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        j += timedelta(days=1)

    print(f"\n{total_barres:,} barres de {args.barre}s sur {jours} jours "
          f"({total_trades/1e6:.1f}M transactions) en "
          f"{(time.time()-debut)/60:.0f} min")
    print(f"-> {chemin}")


if __name__ == "__main__":
    main()
