"""
diagnose_structure.py — Pourquoi les setups perdent-ils ?

Un backtest dit COMBIEN on perd. Il ne dit pas POURQUOI. Ce script mesure,
pour chaque trade, jusqu'où le prix est allé en faveur (MFE) et contre (MAE)
avant la sortie, en multiples du risque.

Ce que ces deux mesures permettent de trancher :
  - MFE souvent > 1R sur des trades stoppés  -> le signal fonctionne, c'est
    l'objectif qui est trop loin ou la sortie mal gérée ;
  - MFE proche de 0                          -> le signal ne prédit rien, le
    prix part contre dès l'entrée ;
  - MAE faible sur les gagnants              -> les entrées sont bien placées ;
  - MAE proche de 1R partout                 -> le stop est trop serré.

Usage : python diagnose_structure.py [--jours 900] [--tf 15m]
"""

import argparse
import statistics
from collections import Counter
from datetime import datetime, timezone

import candles as K
import config as C
import technical as T


def rejouer(bougies):
    """Comme backtest_structure, mais en enregistrant MFE et MAE."""
    trades = []
    pos = None
    barres_max = int(C.MAX_HOLD_SEC / (K.TF_MS[C.TIMEFRAME] / 1000))
    cooldown = -1

    for i in range(T.BOUGIES_REQUISES, len(bougies)):
        c = bougies[i]

        if pos:
            sens = 1 if pos["side"] == "buy" else -1
            # Excursions, en multiples du risque
            fav = (c["high"] - pos["entry"]) if sens > 0 else (pos["entry"] - c["low"])
            adv = (pos["entry"] - c["low"]) if sens > 0 else (c["high"] - pos["entry"])
            pos["mfe"] = max(pos["mfe"], fav / pos["risque_px"])
            pos["mae"] = max(pos["mae"], adv / pos["risque_px"])

            if sens > 0:
                sl, tp = c["low"] <= pos["sl"], c["high"] >= pos["tp"]
            else:
                sl, tp = c["high"] >= pos["sl"], c["low"] <= pos["tp"]
            expire = (i - pos["i"]) >= barres_max

            if sl or tp or expire:
                res = "SL" if sl else ("TP" if tp else "TIMEOUT")
                sortie = pos["sl"] if sl else (pos["tp"] if tp else c["close"])
                r_brut = (sortie - pos["entry"]) * sens / pos["risque_px"]
                trades.append({
                    "ts": pos["ts"], "side": pos["side"], "result": res,
                    "r": r_brut, "mfe": pos["mfe"], "mae": pos["mae"],
                    "barres": i - pos["i"], "stop_bps": pos["stop_bps"],
                    "force": pos["force"], "touches": pos["touches"],
                    "fib": pos["fib"],
                })
                pos, cooldown = None, i + 1

        if pos or i <= cooldown:
            continue

        s, _ = T.setup(bougies, i)
        if not s:
            continue
        entree = c["close"]
        buf = C.SL_BUFFER_ATR * (c["atr"] or 0.0)
        sens = s["direction"]
        sl = s["invalidation"] - buf * sens
        risque_px = (entree - sl) * sens
        if risque_px <= 0 or risque_px / entree * 10_000 < C.MIN_STOP_BPS:
            continue
        # Position du prix dans la jambe, en retracement Fibonacci
        j = s["jambe"]
        fib = abs(entree - j["arrivee"]) / j["amplitude"] if j["amplitude"] else 0
        pos = {"i": i, "ts": c["ts"], "side": "buy" if sens > 0 else "sell",
               "entry": entree, "sl": sl, "tp": entree + C.RR * risque_px * sens,
               "risque_px": risque_px, "mfe": 0.0, "mae": 0.0,
               "stop_bps": risque_px / entree * 10_000,
               "force": s["force"], "touches": s["sr"]["touches"],
               "fib": round(fib, 3)}
    return trades


def bloc(titre, valeurs, unite=""):
    if not valeurs:
        print(f"  {titre:<28} (aucun)")
        return
    t = sorted(valeurs)
    n = len(t)
    print(f"  {titre:<28} med {statistics.median(t):>6.2f}{unite}  "
          f"p25 {t[n//4]:>6.2f}  p75 {t[3*n//4]:>6.2f}  "
          f"max {t[-1]:>6.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jours", type=int, default=900)
    ap.add_argument("--tf", default="15m")
    args = ap.parse_args()

    C.use_venue("binance_hist")
    C.TIMEFRAME = args.tf
    b = K.fetch("binance_hist", args.tf, days=args.jours, verbose=False)
    T.add_indicators(b)
    d0 = datetime.fromtimestamp(b[0]["ts"]/1000, timezone.utc)
    d1 = datetime.fromtimestamp(b[-1]["ts"]/1000, timezone.utc)
    print(f"{len(b):,} bougies {args.tf} — {d0:%Y-%m-%d} -> {d1:%Y-%m-%d}\n")

    tr = rejouer(b)
    if not tr:
        raise SystemExit("aucun trade")
    par_res = Counter(t["result"] for t in tr)
    print(f"=== {len(tr)} trades : "
          + "  ".join(f"{k} {v}" for k, v in par_res.most_common()) + " ===\n")

    print("EXCURSION MAXIMALE EN FAVEUR (MFE), en multiples du risque")
    bloc("tous les trades", [t["mfe"] for t in tr], "R")
    for res in ("SL", "TIMEOUT", "TP"):
        bloc(f"  parmi les {res}", [t["mfe"] for t in tr if t["result"] == res], "R")

    print("\nEXCURSION MAXIMALE CONTRE (MAE)")
    bloc("tous les trades", [t["mae"] for t in tr], "R")
    for res in ("SL", "TIMEOUT", "TP"):
        bloc(f"  parmi les {res}", [t["mae"] for t in tr if t["result"] == res], "R")

    sl = [t for t in tr if t["result"] == "SL"]
    print(f"\nLES {len(sl)} TRADES STOPPES")
    for seuil in (0.5, 1.0, 1.5, 2.0):
        n = sum(1 for t in sl if t["mfe"] >= seuil)
        print(f"  sont d'abord alles a +{seuil}R : {n:>4} "
              f"({n/len(sl)*100:>4.1f} %)")
    rapides = sum(1 for t in sl if t["barres"] <= 4)
    print(f"  stoppes en 4 bougies ou moins : {rapides} "
          f"({rapides/len(sl)*100:.0f} %)")

    print("\nCE QU'UN OBJECTIF PLUS PROCHE AURAIT DONNE")
    print("  (meme entrees, meme stops, seul le TP change)")
    print(f"  {'RR':>5} {'TP touches':>11} {'SL':>6} {'expires':>8} "
          f"{'esperance brute':>16}")
    for rr in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0):
        tp = sl_n = exp = 0
        somme = 0.0
        for t in tr:
            if t["mfe"] >= rr:            # l'objectif aurait ete atteint
                tp += 1
                somme += rr
            elif t["mae"] >= 1.0:         # sinon le stop aurait ete touche
                sl_n += 1
                somme -= 1.0
            else:                          # ni l'un ni l'autre : sortie a plat
                exp += 1
                somme += t["r"]
        print(f"  {rr:>5} {tp:>11} {sl_n:>6} {exp:>8} {somme/len(tr):>15.3f}R")

    print("\nSELECTIVITE : le resultat depend-il de la qualite du setup ?")
    for nom, cle, seuils in (("force de tendance", "force", (1, 2)),
                             ("touches du S/R", "touches", (5, 10, 15)),
                             ("retracement fibo", "fib", (0.5, 0.618, 0.786))):
        print(f"  {nom} :")
        for s_ in seuils:
            sous = [t for t in tr if t[cle] >= s_]
            if len(sous) < 20:
                continue
            r = statistics.fmean(t["r"] for t in sous)
            w = sum(1 for t in sous if t["r"] > 0) / len(sous) * 100
            print(f"    >= {s_:<6} {len(sous):>4} trades  "
                  f"winrate {w:>4.1f} %  esperance {r:>+6.3f}R")


if __name__ == "__main__":
    main()
