"""
ouvrir_reserve.py — Le test unique, sur les données jamais regardées.

C'EST UN COUP UNIQUE

La réserve est constituée des 30 % les plus récents, mis de côté avant toute
recherche. Elle n'a servi ni à choisir un candidat, ni à régler un seuil, ni
à décider d'un horizon. C'est ce qui lui donne sa valeur : elle est le seul
échantillon dont le résultat n'est pas déjà contaminé par la recherche.

Si l'on ajuste quoi que ce soit après l'avoir consultée, elle devient un
simple deuxième essai et ne vaut plus rien. On note donc le résultat tel
qu'il vient, bon ou mauvais.

CORRECTION POUR TESTS MULTIPLES

La recherche a balayé 22 candidats sur 4 horizons, soit 88 mesures. Avec 88
tests, on attend ~4 faux positifs au seuil habituel : le seuil corrigé n'est
plus 2.0 mais ~3.3. Ce script l'applique explicitement, ce que le compteur
d'hypothèses du laboratoire ne faisait pas — il ne comptait que les
hypothèses déclarées, pas les mesures effectuées.

Usage :
  python ouvrir_reserve.py --candidats pression_volume,volume_signe_5b
"""

import argparse
import math

import candles as K
import chercher_signal as S
from laboratoire import Labo

CONTRATS = {"MNQ": {"point": 2.0, "commission": 1.24, "sym": "NQ=F"},
            "MES": {"point": 5.0, "commission": 1.24, "sym": "ES=F"}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidats", default="pression_volume,volume_signe_5b")
    ap.add_argument("--horizon", type=int, default=6)
    ap.add_argument("--contrat", default="MNQ", choices=list(CONTRATS))
    ap.add_argument("--mesures", type=int, default=88,
                    help="nombre de mesures effectuees pendant la recherche")
    args = ap.parse_args()

    spec = CONTRATS[args.contrat]
    noms = [x.strip() for x in args.candidats.split(",")]

    # Seuil corrige du nombre de MESURES, pas d'hypotheses declarees.
    alpha = 0.05 / args.mesures
    seuil = math.sqrt(2 * math.log(1 / alpha))
    print(f"{args.mesures} mesures effectuees pendant la recherche")
    print(f"-> seuil de |t| exige sur la reserve : {seuil:.2f}")
    print(f"   (contre 2.00 si l'on ignorait les tests multiples)\n")

    b = K.fetch_yahoo(spec["sym"], "5m", verbose=False)
    travail, reserve = Labo.decouper(b)
    print(f"{len(b):,} barres — travail {len(travail):,}, "
          f"reserve {len(reserve):,}")

    labo = Labo()
    F_t, a_t = S.construire(travail)
    F_r, a_r = S.construire(reserve)
    fut_t = S.rendements_futurs(travail, a_t, args.horizon)
    fut_r = S.rendements_futurs(reserve, a_r, args.horizon)

    prix = reserve[-1]["close"]
    atr_r = sum(x for x in a_r if x) / sum(1 for x in a_r if x)
    frais_bps = spec["commission"] / (prix * spec["point"]) * 10_000
    print(f"prix {prix:,.0f} — ATR reserve {atr_r:.1f} pts = "
          f"{atr_r*spec['point']:.1f} $ par {args.contrat}\n")

    print("=" * 74)
    for nom in noms:
        if nom not in F_r:
            print(f"{nom} : candidat inconnu")
            continue
        rt = labo.evaluer(F_t[nom], fut_t, args.horizon, nom=nom)
        rr = labo.evaluer(F_r[nom], fut_r, args.horizon, nom=nom, prix=prix,
                          frais_bps=frais_bps, atr=atr_r * spec["point"])
        print(f"\n{nom}")
        print(f"  recherche : IC {rt['ic']:+.4f}  t {rt['t']:+.2f}  "
              f"n {rt['n']:,}")
        if rr.get("erreur"):
            print(f"  RESERVE   : {rr['erreur']}")
            continue
        parts = " ".join(f"{p:+.3f}" for p in rr["parts"])
        print(f"  RESERVE   : IC {rr['ic']:+.4f}  t {rr['t']:+.2f}  "
              f"n {rr['n']:,}")
        print(f"              par quart {parts} "
              f"{'stable' if rr['stable'] else 'INSTABLE'}")
        print(f"              net {rr['net']:+.2f} $ par contrat "
              f"({rr['net_bps']:+.1f} bps)")

        meme_sens = rt["ic"] * rr["ic"] > 0
        confirme = (meme_sens and abs(rr["t"]) >= seuil
                    and rr["stable"] and rr["net"] > 0)
        if confirme:
            print(f"  => CONFIRME sur donnees jamais vues.")
        elif meme_sens and rr["net"] > 0:
            print(f"  => meme sens et net positif, mais |t| {abs(rr['t']):.2f} "
                  f"< {seuil:.2f} : pas concluant")
        elif meme_sens:
            print(f"  => meme sens mais non rentable apres frais")
        else:
            print(f"  => SIGNE INVERSE sur la reserve : l'effet ne tient pas")

    print("\n" + "=" * 74)
    print("La reserve vient d'etre consommee pour ces candidats. Tout")
    print("reglage ulterieur devra etre valide sur des donnees encore")
    print("differentes — sinon on recommence a s'illusionner.")


if __name__ == "__main__":
    main()
