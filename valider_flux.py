"""
valider_flux.py — Le signal de flux survit-il à une mesure honnête ?

Le premier passage donnait IC +0.051 avec t = +5.1. Ce chiffre est faux : un
rendement à 24 barres mesuré toutes les barres se recouvre 24 fois, donc les
observations ne sont pas indépendantes et la t-statistique est gonflée d'un
facteur ~racine(24), soit ~5. C'est exactement l'erreur déjà commise sur
l'effet de séance, où t = -4.9 est devenu t = -0.4 une fois corrigé.

Ce script mesure la même chose SANS RECOUVREMENT : une observation tous les
`horizon` barres. Le nombre d'observations chute d'un facteur `horizon`, et
c'est le prix de l'honnêteté.

Trois contrôles supplémentaires :
  - découpage en quatre périodes consécutives : un signal réel garde son
    signe partout, un artefact non ;
  - test du décile extrême : le signal sépare-t-il vraiment, ou l'IC vient-il
    de quelques points aberrants ;
  - traduction en dollars, frais compris, pour savoir si ça vaut un trade.

Usage : python valider_flux.py [--horizon 24]
"""

import argparse
import math
import statistics
from datetime import datetime, timezone

import chercher_flux as CF
import chercher_signal as S

# Les candidats qui ressortaient du premier passage. On ne teste que ceux-là :
# multiplier les tests sur les mêmes données fabriquerait des faux positifs.
CANDIDATS = ["delta_cumule_12b", "gros_delta_6b", "trade_max_norm",
             "delta_cumule_6b", "petit_delta_norm", "delta_cumule_3b",
             "desequilibre_nombre"]


def sans_recouvrement(serie, fut, horizon):
    """Une observation tous les `horizon` pas : aucun chevauchement."""
    paires = []
    for i in range(0, len(serie) - horizon, horizon):
        if serie[i] is not None and fut[i] is not None:
            paires.append((serie[i], fut[i]))
    return paires


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fichier", default=CF.FICHIER)
    ap.add_argument("--horizon", type=int, default=24)
    args = ap.parse_args()

    b = CF.charger(args.fichier)
    F, a = CF.construire_flux(b)
    fut = S.rendements_futurs(b, a, args.horizon)
    jours = (b[-1]["ts"] - b[0]["ts"]) / 86_400_000
    d0 = datetime.fromtimestamp(b[0]["ts"]/1000, timezone.utc)
    d1 = datetime.fromtimestamp(b[-1]["ts"]/1000, timezone.utc)

    print(f"{len(b):,} barres — {jours:.0f} jours "
          f"({d0:%Y-%m-%d} -> {d1:%Y-%m-%d})")
    print(f"horizon {args.horizon} barres = {args.horizon*5} min\n")

    n_chev = len(b)
    n_indep = n_chev // args.horizon
    print(f"observations avec recouvrement : {n_chev:,} (bruit "
          f"{1/math.sqrt(n_chev):.4f})")
    print(f"observations INDEPENDANTES     : {n_indep:,} (bruit "
          f"{1/math.sqrt(n_indep):.4f})")
    print(f"-> les t-statistiques du premier passage etaient gonflees "
          f"d'environ {math.sqrt(args.horizon):.1f}x\n")

    # Quatre periodes consecutives : un signal reel garde son signe partout.
    q = len(b) // 4
    bornes = [(0, q), (q, 2*q), (2*q, 3*q), (3*q, len(b))]

    print(f"{'candidat':<22} {'n':>5} {'IC':>8} {'t':>7} "
          f"{'P1':>7}{'P2':>7}{'P3':>7}{'P4':>7}  verdict")
    print("─" * 84)

    survivants = []
    for nom in CANDIDATS:
        serie = F.get(nom)
        if serie is None:
            continue
        paires = sans_recouvrement(serie, fut, args.horizon)
        if len(paires) < 60:
            print(f"{nom:<22} trop peu d'observations independantes")
            continue
        x = [p[0] for p in paires]
        y = [p[1] for p in paires]
        ic, t = S.correlation(x, y)

        signes = []
        for d, f_ in bornes:
            pp = sans_recouvrement(serie[d:f_], fut[d:f_], args.horizon)
            signes.append(S.correlation([p[0] for p in pp], [p[1] for p in pp])[0]
                          if len(pp) > 15 else 0.0)

        meme_signe = all(s * ic > 0 for s in signes if s != 0)
        solide = abs(t) > 2 and meme_signe
        verdict = "SOLIDE" if solide else ("instable" if abs(t) > 2 else "bruit")
        print(f"{nom:<22} {len(paires):>5} {ic:>+8.4f} {t:>+7.2f} "
              + "".join(f"{s:>+7.3f}" for s in signes)
              + f"  {verdict}")
        if solide:
            survivants.append((nom, ic, t, x, y))

    print("\n" + "=" * 84)
    if not survivants:
        print("AUCUN candidat ne survit a une mesure sans recouvrement.")
        print("Le signal apparent venait du chevauchement des echantillons.")
        return

    print(f"{len(survivants)} candidat(s) survivent. Traduction en dollars :\n")
    atr_moy = statistics.fmean(v for v in a if v)
    prix = statistics.fmean(x["close"] for x in b)
    print(f"ATR moyen {atr_moy:.1f} $ sur BTC a {prix:,.0f} $\n")

    for nom, ic, t, x, y in survivants:
        # Ce que rapporte le decile extreme, la ou le signal est le plus net.
        paires = sorted(zip(x, y))
        k = max(1, len(paires)//10)
        haut = statistics.fmean(p[1] for p in paires[-k:])
        bas = statistics.fmean(p[1] for p in paires[:k])
        # On prend le decile du bon cote selon le signe du signal
        gain_atr = haut if ic > 0 else -bas
        gain = gain_atr * atr_moy
        # Frais perpetuels Binance : 0.05 % par cote, aller-retour sur le notionnel
        frais = prix * 0.001
        print(f"  {nom}")
        print(f"    IC {ic:+.4f} (t={t:+.2f}) sur {len(x)} observations independantes")
        print(f"    decile favorable : {gain_atr:+.3f} ATR = {gain:+.0f} $ "
              f"par BTC engage")
        print(f"    frais aller-retour a 0.05 %/cote : {frais:.0f} $")
        print(f"    NET : {gain - frais:+.0f} $ par BTC "
              f"({(gain-frais)/prix*10_000:+.1f} bps)")
        print(f"    frequence : le decile represente {k} occasions sur "
              f"{len(x)}, soit ~{k/jours:.1f} par jour\n")


if __name__ == "__main__":
    main()
