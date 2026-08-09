"""
backtest_nq.py — Signal composite sur futures Nasdaq, avec frais de futures.

Ce qui distingue ce backtest des précédents :

  1. Le signal n'est pas supposé, il est DÉRIVÉ. Les poids viennent des
     corrélations mesurées sur la première moitié des données ; la seconde
     moitié n'est jamais regardée pour les choisir. C'est la seule façon
     d'éviter de se raconter une histoire.

  2. Les frais sont fixes par contrat, pas proportionnels au notionnel.
     C'est toute la différence entre futures et crypto : sur MNQ une
     commission d'environ 1,24 $ aller-retour représente ~3 % du risque
     là où la crypto en prenait 10 % et plus.

  3. La sortie est à horizon fixe, sans stop ni objectif. Un IC mesure la
     capacité à prédire le rendement à N barres : le monétiser tel quel
     évite d'attribuer à un signal ce qui viendrait d'une gestion de sortie.
     Les stops viendront après, si et seulement si l'espérance brute existe.

Usage :
  python backtest_nq.py --tf 5m --horizon 12
  python backtest_nq.py --tf 5m --horizon 12 --contrat NQ
"""

import argparse
import math
import statistics

import candles as K
import chercher_signal as S

# Spécifications des contrats Nasdaq (CME).
# La commission aller-retour est un ordre de grandeur courant chez les
# courtiers discount ; elle est le paramètre à ajuster selon le tien.
CONTRATS = {
    "MNQ": {"point": 2.0,  "tick": 0.25, "commission_ar": 1.24},
    "NQ":  {"point": 20.0, "tick": 0.25, "commission_ar": 3.10},
}


def zscore(serie, fenetre=200):
    """Normalise un candidat pour que les composantes soient comparables."""
    out = [None] * len(serie)
    valeurs = []
    for i, x in enumerate(serie):
        if x is None:
            valeurs.append(None)
            continue
        valeurs.append(x)
        recents = [v for v in valeurs[max(0, i - fenetre):i + 1] if v is not None]
        if len(recents) < 30:
            continue
        m = statistics.fmean(recents)
        e = statistics.pstdev(recents)
        out[i] = (x - m) / e if e > 0 else 0.0
    return out


def composer(F, a, bougies, horizon, fin_calibration):
    """
    Construit le signal composite.

    Les poids sont les IC mesurés sur la seule plage de calibration, bornés
    pour qu'aucun candidat ne domine, et les candidats sous le bruit sont
    écartés. Aucune information issue de la période de contrôle n'entre ici.
    """
    fut = S.rendements_futurs(bougies, a, horizon)
    bruit = 1 / math.sqrt(fin_calibration)
    poids, retenus = {}, []
    for nom, serie in F.items():
        if nom == "heure_utc":            # catégoriel, pas linéaire
            continue
        paires = [(s, f) for k, (s, f) in enumerate(zip(serie, fut))
                  if k < fin_calibration and s is not None and f is not None]
        if len(paires) < 500:
            continue
        ic, _ = S.correlation([p[0] for p in paires], [p[1] for p in paires])
        if abs(ic) < 2 * bruit:           # indiscernable du hasard
            continue
        poids[nom] = ic
        retenus.append((nom, ic))

    if not poids:
        return None, []

    z = {nom: zscore(F[nom]) for nom in poids}
    total_poids = sum(abs(p) for p in poids.values())
    signal = [None] * len(bougies)
    for i in range(len(bougies)):
        s, ok = 0.0, True
        for nom, p in poids.items():
            v = z[nom][i]
            if v is None:
                ok = False
                break
            s += p * max(-3.0, min(3.0, v))     # borne les valeurs extrêmes
        signal[i] = s / total_poids if ok else None
    return signal, sorted(retenus, key=lambda x: -abs(x[1]))


def rejouer(bougies, signal, horizon, seuil, spec, debut, fin):
    """
    Entre quand |signal| dépasse le seuil, sort après `horizon` barres.

    Une seule position à la fois : sans ça, on compterait plusieurs fois le
    même mouvement et l'espérance serait artificiellement gonflée.
    """
    trades = []
    prochaine_libre = debut
    for i in range(debut, min(fin, len(bougies) - horizon)):
        s = signal[i]
        if s is None or abs(s) < seuil or i < prochaine_libre:
            continue
        sens = 1 if s > 0 else -1
        entree = bougies[i]["close"]
        sortie = bougies[i + horizon]["close"]
        points = (sortie - entree) * sens
        brut = points * spec["point"]
        net = brut - spec["commission_ar"]
        trades.append({"i": i, "sens": sens, "signal": s, "points": points,
                       "brut": brut, "net": net})
        prochaine_libre = i + horizon
    return trades


def bilan(nom, trades, spec):
    if not trades:
        print(f"  {nom:<26} aucun trade")
        return None
    nets = [t["net"] for t in trades]
    bruts = [t["brut"] for t in trades]
    gagnants = sum(1 for x in nets if x > 0)
    total = sum(nets)
    esp = statistics.fmean(nets)
    esp_brut = statistics.fmean(bruts)
    # t de Student sur l'espérance nette : le résultat est-il distinguable de zéro ?
    e = statistics.pstdev(nets) or 1e-9
    t = esp / (e / math.sqrt(len(nets)))
    print(f"  {nom:<26} {len(trades):>5} trades  "
          f"win {gagnants/len(trades)*100:>4.1f} %  "
          f"brut {esp_brut:>+6.2f} $  frais {spec['commission_ar']:.2f} $  "
          f"net {esp:>+6.2f} $/trade  total {total:>+8.0f} $  t={t:>+5.2f}")
    return {"n": len(trades), "esp": esp, "total": total, "t": t}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbole", default="NQ=F")
    ap.add_argument("--tf", default="5m")
    ap.add_argument("--horizon", type=int, default=12)
    ap.add_argument("--contrat", default="MNQ", choices=list(CONTRATS))
    ap.add_argument("--seuils", default="0.3,0.5,0.8,1.2")
    args = ap.parse_args()

    spec = CONTRATS[args.contrat]
    b = K.fetch_yahoo(args.symbole, args.tf, verbose=False)
    print(f"{len(b):,} bougies {args.tf} — {args.symbole}")
    print(f"contrat {args.contrat} : 1 point = {spec['point']} $, "
          f"commission A/R {spec['commission_ar']} $")
    print(f"horizon de sortie : {args.horizon} bougies\n")

    F, a = S.construire(b)
    milieu = len(b) // 2

    signal, retenus = composer(F, a, b, args.horizon, milieu)
    if signal is None:
        raise SystemExit("aucun candidat ne depasse le bruit sur la calibration")

    print("CANDIDATS RETENUS (poids = IC mesure sur la 1re moitie seulement)")
    for nom, ic in retenus:
        sens = "suit" if ic > 0 else "contre"
        print(f"  {nom:<24} IC {ic:>+7.4f}  ({sens})")

    atr_moy = statistics.fmean(x for x in a[milieu:] if x)
    print(f"\nATR moyen : {atr_moy:.1f} points = "
          f"{atr_moy * spec['point']:.0f} $ par contrat {args.contrat}")
    print(f"Commission = {spec['commission_ar'] / (atr_moy * spec['point']) * 100:.1f} % "
          f"d'un ATR\n")

    for seuil in [float(x) for x in args.seuils.split(",")]:
        print(f"seuil {seuil} :")
        bilan("  calibration (1re moitie)",
              rejouer(b, signal, args.horizon, seuil, spec, 200, milieu), spec)
        r = bilan("  CONTROLE (2nde moitie)",
                  rejouer(b, signal, args.horizon, seuil, spec, milieu,
                          len(b)), spec)
        if r and r["esp"] > 0 and r["t"] > 2:
            print("    ^ positif et statistiquement distinguable de zero")
        print()

    print("Lecture : seule la ligne CONTROLE compte. La calibration est")
    print("optimiste par construction, les poids en viennent.")


if __name__ == "__main__":
    main()
