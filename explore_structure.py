"""
explore_structure.py — Balayage large de la DÉFINITION du setup.

Le diagnostic MFE/MAE a montré que la gestion de sortie n'est pas en cause :
l'espérance brute vaut ~+0.02R quel que soit l'objectif, ce qui est le profil
d'entrées aléatoires. Si un avantage existe, il est dans la définition de
l'entrée. Ce script en balaie les dimensions.

MÉTRIQUE : l'espérance BRUTE, avant frais. C'est le seul chiffre qui dit s'il
y a quelque chose à exploiter. Les frais valent ~0.10R par trade ; en dessous
de +0.10R brut, rien n'est récupérable, quelle que soit la plateforme.

Chaque configuration est évaluée à objectif variable (le meilleur RR est
retenu) pour ne pas condamner un signal à cause d'un mauvais réglage de sortie.

Usage :
  python explore_structure.py --jours 900 --tf 15m
  python explore_structure.py --jours 400 --tf 1h --rapide
"""

import argparse
import itertools
import statistics
from datetime import datetime, timezone

import candles as K
import config as C
import technical as T

# Objectifs testés pour chaque configuration : on retient le meilleur, pour
# juger le SIGNAL et non un réglage de sortie arbitraire.
RR_TESTES = (0.75, 1.0, 1.5, 2.0, 3.0)


def excursions(bougies):
    """
    Pour chaque setup : MFE et MAE en multiples du risque.

    Une seule passe suffit ensuite à évaluer tous les objectifs — c'est ce qui
    rend le balayage abordable.
    """
    res = []
    pos = None
    barres_max = int(C.MAX_HOLD_SEC / (K.TF_MS[C.TIMEFRAME] / 1000))
    cooldown = -1

    for i in range(T.BOUGIES_REQUISES, len(bougies)):
        c = bougies[i]
        if pos:
            sens = pos["sens"]
            fav = (c["high"] - pos["entry"]) if sens > 0 else (pos["entry"] - c["low"])
            adv = (pos["entry"] - c["low"]) if sens > 0 else (c["high"] - pos["entry"])
            pos["mfe"] = max(pos["mfe"], fav / pos["risque"])
            pos["mae"] = max(pos["mae"], adv / pos["risque"])
            fini = pos["mae"] >= 1.0 or (i - pos["i"]) >= barres_max
            if fini:
                sortie = (c["close"] - pos["entry"]) * sens / pos["risque"]
                res.append((pos["mfe"], pos["mae"], sortie))
                pos, cooldown = None, i + 1
        if pos or i <= cooldown:
            continue

        s, _ = T.setup(bougies, i)
        if not s:
            continue
        entree = c["close"]
        sens = s["direction"]
        sl = s["invalidation"] - C.SL_BUFFER_ATR * (c["atr"] or 0.0) * sens
        risque = (entree - sl) * sens
        if risque <= 0 or risque / entree * 10_000 < C.MIN_STOP_BPS:
            continue
        pos = {"i": i, "entry": entree, "sens": sens, "risque": risque,
               "mfe": 0.0, "mae": 0.0}
    return res


def esperance(exc, rr):
    """Espérance brute pour un objectif donné, à partir des excursions."""
    if not exc:
        return 0.0, 0, 0.0
    total, gagnants = 0.0, 0
    for mfe, mae, sortie in exc:
        if mfe >= rr:
            total += rr
            gagnants += 1
        elif mae >= 1.0:
            total -= 1.0
        else:
            total += sortie
            gagnants += 1 if sortie > 0 else 0
    return total / len(exc), len(exc), gagnants / len(exc) * 100


def evaluer(bougies):
    """Meilleure espérance brute d'une configuration, tous objectifs confondus."""
    exc = excursions(bougies)
    if len(exc) < 25:                      # trop peu pour signifier quoi que ce soit
        return None
    best = max(((esperance(exc, rr), rr) for rr in RR_TESTES),
               key=lambda x: x[0][0])
    (esp, n, win), rr = best
    return {"esp": esp, "n": n, "win": win, "rr": rr}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jours", type=int, default=900)
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--rapide", action="store_true", help="grille reduite")
    args = ap.parse_args()

    C.use_venue("binance_hist")
    C.TIMEFRAME = args.tf
    b = K.fetch("binance_hist", args.tf, days=args.jours, verbose=False)
    T.add_indicators(b)
    d0 = datetime.fromtimestamp(b[0]["ts"]/1000, timezone.utc)
    d1 = datetime.fromtimestamp(b[-1]["ts"]/1000, timezone.utc)
    print(f"{len(b):,} bougies {args.tf} — {d0:%Y-%m-%d} -> {d1:%Y-%m-%d}")
    print(f"frais ~{C.FEE_ROUNDTRIP_BPS:.0f} bps AR — seuil de viabilite "
          f"~+0.10R brut\n")

    zones = [(0.236, 0.5), (0.382, 0.618), (0.5, 0.786), (0.618, 0.786),
             (0.382, 0.786)]
    pivots = [3, 5, 8]
    legs = [None, 20, 50]
    vols = [None, 1.2]
    forces = [1, 2]
    srs = [True, False]
    if args.rapide:
        zones, pivots, legs, vols = zones[:3], [5], [None, 20], [None, 1.2]

    grille = list(itertools.product(zones, pivots, legs, vols, forces, srs))
    print(f"{len(grille)} configurations a evaluer...\n")
    print(f"{'zone fibo':<14} {'piv':>3} {'jambe':>6} {'vol':>5} {'F':>2} "
          f"{'S/R':>4} │ {'trades':>6} {'win%':>6} {'RR':>4} {'esp. brute':>11}")
    print("─" * 78)

    resultats = []
    for k, (z, p, lg, vol, force, sr) in enumerate(grille, 1):
        T.FIB_ZONE, T.PIVOT_N = z, p
        T.LEG_MAX_BARRES, T.VOL_MIN_RATIO = lg, vol
        T.FORCE_MIN, T.EXIGER_SR = force, sr
        T.BOUGIES_REQUISES = T.MM_LONG + T.SR_LOOKBACK + p + 10
        T.add_indicators(b)                # les pivots dependent de PIVOT_N

        r = evaluer(b)
        etiq = (f"{z[0]:.3f}-{z[1]:.3f}", p, lg or "-", vol or "-",
                force, "oui" if sr else "non")
        if r is None:
            print(f"{etiq[0]:<14} {p:>3} {str(lg or '-'):>6} {str(vol or '-'):>5} "
                  f"{force:>2} {etiq[5]:>4} │ {'trop peu de trades':>30}")
            continue
        marque = "  <<<" if r["esp"] >= 0.10 else ""
        print(f"{etiq[0]:<14} {p:>3} {str(lg or '-'):>6} {str(vol or '-'):>5} "
              f"{force:>2} {etiq[5]:>4} │ {r['n']:>6} {r['win']:>6.1f} "
              f"{r['rr']:>4} {r['esp']:>+10.3f}R{marque}")
        resultats.append((r["esp"], etiq, r))

    print("\n" + "=" * 78)
    resultats.sort(reverse=True, key=lambda x: x[0])
    print("MEILLEURES CONFIGURATIONS")
    for esp, etiq, r in resultats[:8]:
        print(f"  {esp:+.3f}R  zone {etiq[0]}  pivot {etiq[1]}  "
              f"jambe<={etiq[2]}  vol>={etiq[3]}  force {etiq[4]}  "
              f"S/R {etiq[5]}  ({r['n']} trades, RR {r['rr']})")
    if resultats:
        meilleure = resultats[0][0]
        print(f"\nMeilleure esperance brute : {meilleure:+.3f}R")
        if meilleure < 0.10:
            print("Sous le seuil de viabilite : les frais (~0.10R) l'absorbent.")
            print("Aucune combinaison de cette famille ne produit d'avantage.")
        else:
            print("Au-dessus du seuil : a verifier hors echantillon avant d'y croire.")


if __name__ == "__main__":
    main()
