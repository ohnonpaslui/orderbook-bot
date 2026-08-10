"""
tester_tsmom.py — Le momentum de série temporelle tient-il sur mes données ?

POURQUOI CETTE FAMILLE-LÀ

C'est la seule dont l'avantage soit documenté par des travaux revus : Moskowitz,
Ooi et Pedersen (Journal of Financial Economics, 2012) le mesurent sur 58
contrats futures liquides, avec un lookback de 12 mois et une détention d'un
mois, robuste sur sous-périodes. Tout le reste de ce que ce projet a testé
relevait du folklore technique.

Elle pointe à l'opposé de ce qu'on cherchait : détention d'un mois, pas de
scalping. Si elle tient ici, la configuration du bot en découle — et elle ne
ressemblera pas à un bot de carnet.

POURQUOI UN PANIER

Dix ans de données quotidiennes ne font que ~120 observations indépendantes à
un mois de détention : beaucoup trop peu pour trancher. Le papier d'origine
compense par la largeur — 58 instruments. On fait pareil à petite échelle :
huit contrats de familles différentes, mis en commun, pour atteindre une
puissance statistique réelle.

Usage :
  python tester_tsmom.py
  python tester_tsmom.py --lookbacks 21,63,126,252 --detention 21
"""

import argparse
import statistics

import candles as K
from laboratoire import Labo

# Un panier volontairement diversifié : actions, taux, matières, devises.
# Mettre huit fois le Nasdaq ne donnerait pas huit fois plus d'information.
PANIER = {
    "NQ=F": "Nasdaq 100",
    "ES=F": "S&P 500",
    "YM=F": "Dow Jones",
    "RTY=F": "Russell 2000",
    "CL=F": "Petrole WTI",
    "GC=F": "Or",
    "ZB=F": "Bons du Tresor 30 ans",
    "6E=F": "Euro/Dollar",
}


def serie_momentum(bougies, lookback, detention):
    """
    Momentum passé et rendement futur, normalisés par la volatilité.

    La normalisation est indispensable dans un panier : sans elle, le pétrole
    écraserait les bons du Trésor, et l'on mesurerait la volatilité relative
    des marchés au lieu du momentum.
    """
    c = [b["close"] for b in bougies]
    n = len(c)
    # Volatilité réalisée sur 60 séances, en rendement quotidien.
    rend = [0.0] + [(c[i] - c[i-1]) / c[i-1] for i in range(1, n)]
    vol = [None] * n
    for i in range(60, n):
        vol[i] = statistics.pstdev(rend[i-59:i+1]) or None

    signal, futur = [None] * n, [None] * n
    for i in range(n):
        if i < max(lookback, 60) or vol[i] is None:
            continue
        # Momentum : rendement du lookback rapporté à la volatilité de la
        # période, ce qui rend les instruments comparables entre eux.
        signal[i] = ((c[i] - c[i-lookback]) / c[i-lookback]) / (vol[i] * lookback**0.5)
        if i + detention < n:
            futur[i] = ((c[i+detention] - c[i]) / c[i]) / (vol[i] * detention**0.5)
    return signal, futur


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookbacks", default="21,63,126,252",
                    help="en seances : 21=1 mois, 252=1 an")
    ap.add_argument("--detention", type=int, default=21)
    ap.add_argument("--enregistrer", action="store_true",
                    help="inscrit l'hypothese au registre du laboratoire")
    args = ap.parse_args()

    labo = Labo()
    if args.enregistrer:
        labo.enregistrer(
            "momentum de serie temporelle sur panier de futures "
            f"(lookbacks {args.lookbacks}, detention {args.detention} seances)",
            "famille documentee par Moskowitz-Ooi-Pedersen 2012")
    print(labo.resume() + "\n")

    donnees = {}
    for sym, nom in PANIER.items():
        try:
            b = K.fetch_yahoo(sym, "1d", periode="10y", verbose=False)
            if len(b) > 400:
                donnees[sym] = b
                print(f"  {sym:<7} {nom:<24} {len(b):>5} seances")
            else:
                print(f"  {sym:<7} {nom:<24} ignore ({len(b)} seances)")
        except Exception as e:
            print(f"  {sym:<7} {nom:<24} indisponible ({type(e).__name__})")
    if not donnees:
        raise SystemExit("aucune donnee")
    print()

    lookbacks = [int(x) for x in args.lookbacks.split(",")]
    print(f"{'lookback':>10} {'n indep.':>9} {'IC':>8} {'t':>7} "
          f"{'par quart':>32} verdict")
    print("─" * 84)

    for lb in lookbacks:
        # Mise en commun des instruments : chaque contrat apporte ses propres
        # observations, ce qui donne la puissance qu'un seul ne peut pas avoir.
        sig_total, fut_total = [], []
        for sym, b in donnees.items():
            s, f = serie_momentum(b, lb, args.detention)
            # Echantillonnage sans recouvrement, instrument par instrument :
            # melanger d'abord puis echantillonner casserait l'alignement.
            for i in range(0, len(s), args.detention):
                if s[i] is not None and f[i] is not None:
                    sig_total.append(s[i])
                    fut_total.append(f[i])

        r = labo.evaluer(sig_total, fut_total, horizon=1,
                         nom=f"tsmom_{lb}j")
        if r.get("erreur"):
            print(f"{lb:>9}j {r['erreur']}")
            continue
        mois = lb / 21
        parts = " ".join(f"{p:+.3f}" for p in r["parts"])
        seuil = labo.seuil_t()
        ok = abs(r["t"]) >= seuil and r["stable"]
        print(f"{lb:>8}j ({mois:.0f}m) {r['n']:>9,} {r['ic']:>+8.4f} "
              f"{r['t']:>+7.2f} {parts:>32} "
              f"{'RETENU' if ok else ('instable' if abs(r['t'])>=seuil else 'bruit')}")

    print(f"\nseuil de |t| exige : {labo.seuil_t():.2f} "
          f"(apres {len(labo.essais)} essais enregistres)")
    print("\nLecture : un IC significatif ET stable sur les quatre quarts")
    print("signifierait que la famille tient sur ces donnees. Le rendement est")
    print("normalise par la volatilite, donc l'IC se lit en unites de risque.")


if __name__ == "__main__":
    main()
