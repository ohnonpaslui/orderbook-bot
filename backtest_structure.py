"""
backtest_structure.py — La couche technique a-t-elle une espérance, seule ?

Le backtest principal est limité par la rareté des données de carnet : 13
trades sur 22 jours ne permettent pas de conclure. Mais la couche technique
(MM 20/50/200, S/R, Fibonacci) ne dépend que des bougies, gratuites et
disponibles sur des années. On peut donc la juger sur des centaines de trades.

C'est le test décisif : si la structure n'a pas d'espérance sur 900 jours, le
carnet n'y changera rien — il ne fait que filtrer des setups déjà perdants.

Différence assumée avec le moteur principal : l'entrée se fait à la clôture de
la bougie de signal, et SL/TP sont testés sur le haut/bas des bougies suivantes.
C'est moins fin qu'au tick, mais suffisant pour trancher une espérance.

Usage :
  python backtest_structure.py
  python backtest_structure.py --jours 900 --tf 1h
  python backtest_structure.py --set RR=1.5 --set MAX_FEE_FRACTION_OF_RISK=0.5
"""

import argparse
import statistics
from datetime import datetime, timezone

import candles as K
import config as C
import technical as T


def rejouer(bougies, verbose=False):
    """Rejoue la couche technique seule. Retourne (stats, trades)."""
    capital = C.START_CAPITAL
    trades, rejets = [], {}
    pos = None
    equity = [capital]
    barres_max = int(C.MAX_HOLD_SEC / (K.TF_MS[C.TIMEFRAME] / 1000))
    cooldown_jusqua = -1

    for i in range(T.BOUGIES_REQUISES, len(bougies)):
        c = bougies[i]

        # ---- gestion de la position ouverte, sur la bougie qui vient de clore
        if pos:
            if pos["side"] == "buy":
                touche_sl = c["low"] <= pos["sl"]
                touche_tp = c["high"] >= pos["tp"]
            else:
                touche_sl = c["high"] >= pos["sl"]
                touche_tp = c["low"] <= pos["tp"]
            expire = (i - pos["i"]) >= barres_max

            if touche_sl or touche_tp or expire:
                # SL prioritaire : sans données intra-bougie on ne sait pas
                # lequel a été touché en premier, on suppose le défavorable.
                sortie = (pos["sl"] if touche_sl else
                          pos["tp"] if touche_tp else c["close"])
                res = "SL" if touche_sl else ("TP" if touche_tp else "TIMEOUT")
                sens = 1 if pos["side"] == "buy" else -1
                brut = pos["qty"] * (sortie - pos["entry"]) * sens
                frais = (pos["qty"] * pos["entry"] + pos["qty"] * sortie) \
                    * C.FEE_PCT_PER_SIDE / 100
                pnl = brut - frais
                capital = max(0.0, round(capital + pnl, 2))
                trades.append({
                    "ouvert": pos["ts"], "clos": c["ts"], "side": pos["side"],
                    "entry": pos["entry"], "exit": round(sortie, 2),
                    "result": res, "pnl": round(pnl, 2), "frais": round(frais, 2),
                    "r": round(pnl / pos["risque"], 2) if pos["risque"] else 0.0,
                    "barres": i - pos["i"], "stop_bps": pos["stop_bps"],
                    "capital": capital,
                })
                equity.append(capital)
                pos, cooldown_jusqua = None, i + 1
                if capital <= 0:
                    break

        if pos or i <= cooldown_jusqua:
            continue

        # ---- recherche d'un setup
        s, raison = T.setup(bougies, i)
        if not s:
            rejets[raison] = rejets.get(raison, 0) + 1
            continue

        entree = c["close"]
        buf = C.SL_BUFFER_ATR * (c["atr"] or 0.0)
        if s["direction"] > 0:
            sl = s["invalidation"] - buf
            risque_px = entree - sl
            tp = entree + C.RR * risque_px
            side = "buy"
        else:
            sl = s["invalidation"] + buf
            risque_px = sl - entree
            tp = entree - C.RR * risque_px
            side = "sell"

        if risque_px <= 0:
            rejets["invalidation du mauvais cote"] = \
                rejets.get("invalidation du mauvais cote", 0) + 1
            continue
        stop_bps = risque_px / entree * 10_000
        if stop_bps < C.MIN_STOP_BPS:
            rejets["stop trop serre"] = rejets.get("stop trop serre", 0) + 1
            continue

        risque = capital * C.RISK_PER_TRADE / 100
        qty = risque / risque_px
        if qty * entree > capital * C.MAX_NOTIONAL_MULT:
            qty = capital * C.MAX_NOTIONAL_MULT / entree
            risque = qty * risque_px
        pos = {"i": i, "ts": c["ts"], "side": side, "entry": entree, "sl": sl,
               "tp": tp, "qty": qty, "risque": risque,
               "stop_bps": round(stop_bps, 1)}

    return resume(trades, equity), trades, rejets


def resume(trades, equity):
    if not trades:
        return {"trades": 0}
    pnls = [t["pnl"] for t in trades]
    gagnants = [p for p in pnls if p > 0]
    perdants = [p for p in pnls if p <= 0]
    pic, dd = equity[0], 0.0
    for e in equity:
        pic = max(pic, e)
        dd = max(dd, (pic - e) / pic * 100 if pic else 0.0)
    gw, gl = sum(gagnants), abs(sum(perdants))
    return {
        "trades": len(trades),
        "winrate": round(len(gagnants) / len(trades) * 100, 1),
        "pnl": round(sum(pnls), 2),
        "capital": round(equity[-1], 2),
        "max_dd": round(dd, 1),
        "avg_r": round(statistics.fmean(t["r"] for t in trades), 3),
        "pf": round(gw / gl, 2) if gl else float("inf"),
        "frais": round(sum(t["frais"] for t in trades), 2),
        "tp": sum(1 for t in trades if t["result"] == "TP"),
        "sl": sum(1 for t in trades if t["result"] == "SL"),
        "timeout": sum(1 for t in trades if t["result"] == "TIMEOUT"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="binance_hist", choices=list(C.VENUES))
    ap.add_argument("--jours", type=int, default=900)
    ap.add_argument("--tf", default=None, help="ex 15m, 1h, 4h")
    ap.add_argument("--set", action="append", metavar="CLE=VALEUR")
    ap.add_argument("--moitiés", action="store_true",
                    help="calibre sur la 1re moitie, controle sur la 2nde")
    args = ap.parse_args()

    C.use_venue(args.venue)
    if args.tf:
        C.TIMEFRAME = args.tf
    for p in args.set or []:
        k, _, v = p.partition("=")
        if not hasattr(C, k):
            raise SystemExit(f"parametre inconnu : {k}")
        setattr(C, k, type(getattr(C, k))(v))

    print(f"Chargement des bougies {C.TIMEFRAME} ({args.jours} jours)...", flush=True)
    b = K.fetch(args.venue, C.TIMEFRAME, days=args.jours, verbose=False)
    T.add_indicators(b)
    d0 = datetime.fromtimestamp(b[0]["ts"] / 1000, timezone.utc)
    d1 = datetime.fromtimestamp(b[-1]["ts"] / 1000, timezone.utc)
    print(f"{len(b):,} bougies — {d0:%Y-%m-%d} -> {d1:%Y-%m-%d}")
    print(f"frais {C.FEE_ROUNDTRIP_BPS:.0f} bps AR | stop min "
          f"{C.MIN_STOP_BPS:.0f} bps | RR {C.RR}\n")

    lots = [("ensemble", b)]
    if args.moitiés:
        m = len(b) // 2
        lots = [("1re moitie (calibration)", b[:m]),
                ("2nde moitie (controle)", b[m - T.BOUGIES_REQUISES:])]

    for nom, lot in lots:
        s, trades, rejets = rejouer(lot)
        jours = (lot[-1]["ts"] - lot[0]["ts"]) / 86_400_000
        print(f"=== {nom} — {jours:.0f} jours ===")
        if not s["trades"]:
            print("  aucun trade\n")
            continue
        for k, v in s.items():
            print(f"  {k:<9} {v}")
        print(f"  {'freq':<9} {s['trades']/jours*30:.1f} trades / mois")
        pire = sorted(rejets.items(), key=lambda x: -x[1])[:3]
        print("  blocages : " + ", ".join(f"{k} ({v:,})" for k, v in pire))
        print()


if __name__ == "__main__":
    main()
