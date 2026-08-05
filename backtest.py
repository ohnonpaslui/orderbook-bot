"""
backtest.py — Phase 2 : rejoue les données collectées à travers le bot.

Le backtest ne réimplémente rien : il fait tourner exactement `strategy.py` et
`paper_engine.py` sur les snapshots de data/. Si le backtest et le live
divergent un jour, ce sera une différence de données, jamais de logique.

Usage :
  python backtest.py                       # config.py telle quelle
  python backtest.py --from 2026-08-06     # sur une plage de dates
  python backtest.py --set OBI_ENTRY=0.45 --set RR=2.5
  python backtest.py --sweep               # balayage des seuils principaux
"""

import argparse
import csv
import gzip
import glob
import itertools
import os
from datetime import datetime, timezone

import config as C
import paper_engine
from strategy import ObiWallsStrategy

FLOAT_COLS = None       # rempli au premier chargement


# ----------------------------- Chargement -------------------------------------
def load_rows(date_from=None, date_to=None):
    """Charge tous les snapshots de data/, triés chronologiquement."""
    rows = []
    for day_dir in sorted(glob.glob(os.path.join(C.DATA_DIR, "*"))):
        day = os.path.basename(day_dir)
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        for path in sorted(glob.glob(os.path.join(day_dir, "*.csv.gz"))):
            with gzip.open(path, "rt", newline="", encoding="utf-8") as f:
                for rec in csv.DictReader(f):
                    # gzip multi-membres : chaque flush réécrit potentiellement
                    # un en-tête, que DictReader relit comme une ligne de données.
                    if rec.get("ts") in (None, "ts"):
                        continue
                    try:
                        rows.append({k: float(v) for k, v in rec.items()})
                    except (TypeError, ValueError):
                        continue                      # ligne tronquée en fin de fichier
    rows.sort(key=lambda r: r["ts"])
    return rows


# ----------------------------- Statistiques -----------------------------------
def stats(trades, equity):
    if not trades:
        return {"trades": 0}

    pnls   = [t["pnl"] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    peak, max_dd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100 if peak else 0.0)

    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades":   len(trades),
        "winrate":  round(len(wins) / len(trades) * 100, 1),
        "pnl":      round(sum(pnls), 2),
        "capital":  round(equity[-1], 2),
        "max_dd":   round(max_dd, 1),
        "avg_r":    round(sum(t["r"] for t in trades) / len(trades), 2),
        "pf":       round(gross_win / gross_loss, 2) if gross_loss else float("inf"),
        "fees":     round(sum(t["fees"] for t in trades), 2),
        "timeouts": sum(1 for t in trades if t["result"] == "TIMEOUT"),
        "hold_med": int(sorted(t["hold_s"] for t in trades)[len(trades) // 2]),
    }


# ----------------------------- Moteur -----------------------------------------
def run(rows, verbose=False):
    """Rejoue les snapshots. Retourne (stats, trades)."""
    strat  = ObiWallsStrategy()
    state  = {"capital": C.START_CAPITAL, "position": None, "wins": 0,
              "losses": 0, "fees_paid": 0.0}
    equity = [C.START_CAPITAL]
    trades, rejects = [], {}

    for row in rows:
        # L'EMA doit voir chaque snapshot ; le signal n'est retenu que hors position.
        signal = strat.update(row)
        if state["position"]:
            signal = None
        elif signal is None and strat.last_reject:
            rejects[strat.last_reject] = rejects.get(strat.last_reject, 0) + 1

        # last_trades est plafonné à 20 : on compte les clôtures, pas la liste.
        before = state["wins"] + state["losses"]
        state, _, events = paper_engine.step(C.BOT_ID, state, row, signal,
                                             strat, record=False)
        if state["wins"] + state["losses"] > before:
            trades.append(state["last_trades"][-1])
            equity.append(state["capital"])
        if verbose and events:
            for e in events:
                print(f"  {datetime.fromtimestamp(row['ts'], timezone.utc):%m-%d %H:%M}  {e}")

    if verbose and rejects:
        top = sorted(rejects.items(), key=lambda x: -x[1])[:6]
        print("\n  Rejets les plus fréquents :")
        for reason, n in top:
            print(f"    {n:>9,} × {reason}")

    return stats(trades, equity), trades


# ----------------------------- CLI --------------------------------------------
def apply_overrides(pairs):
    for pair in pairs or []:
        key, _, val = pair.partition("=")
        if not hasattr(C, key):
            raise SystemExit(f"Paramètre inconnu dans config.py : {key}")
        setattr(C, key, type(getattr(C, key))(val))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", help="AAAA-MM-JJ inclus")
    ap.add_argument("--to",   dest="date_to",   help="AAAA-MM-JJ inclus")
    ap.add_argument("--set",  action="append", metavar="CLE=VALEUR",
                    help="surcharge un paramètre de config.py")
    ap.add_argument("--sweep", action="store_true", help="balayage des seuils")
    ap.add_argument("--csv", help="écrit les trades dans ce fichier")
    args = ap.parse_args()

    apply_overrides(args.set)

    print("Chargement des snapshots...", flush=True)
    rows = load_rows(args.date_from, args.date_to)
    if not rows:
        raise SystemExit(
            "Aucune donnée dans data/. Lance d'abord le collecteur "
            "(workflow « Collecte carnet ») et laisse-le tourner quelques jours.")

    span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600
    print(f"{len(rows):,} snapshots — {span_h:.1f} h de marché "
          f"({datetime.fromtimestamp(rows[0]['ts'], timezone.utc):%Y-%m-%d %H:%M} → "
          f"{datetime.fromtimestamp(rows[-1]['ts'], timezone.utc):%Y-%m-%d %H:%M} UTC)\n")

    if args.sweep:
        grid = list(itertools.product((0.25, 0.35, 0.45, 0.55),   # OBI_ENTRY
                                      (5, 10, 20),                # OBI_MIN_HOLD
                                      (1.5, 2.0, 3.0)))           # RR
        print(f"{'OBI':>5} {'HOLD':>5} {'RR':>4} │ {'trades':>6} {'win%':>6} "
              f"{'PnL$':>9} {'PF':>6} {'DD%':>6} {'avgR':>6}")
        print("─" * 66)
        for obi, hold, rr in grid:
            C.OBI_ENTRY, C.OBI_MIN_HOLD, C.RR = obi, hold, rr
            s, _ = run(rows)
            if not s["trades"]:
                print(f"{obi:>5} {hold:>5} {rr:>4} │ {'aucun trade':>6}")
                continue
            print(f"{obi:>5} {hold:>5} {rr:>4} │ {s['trades']:>6} {s['winrate']:>6} "
                  f"{s['pnl']:>9.2f} {s['pf']:>6} {s['max_dd']:>6} {s['avg_r']:>6}")
        return

    s, trades = run(rows, verbose=True)
    print("\n" + "═" * 50)
    if not s["trades"]:
        print("Aucun trade déclenché sur la période.")
        print("Les rejets ci-dessus indiquent quel filtre bloque.")
        return
    for k, v in s.items():
        print(f"  {k:<10} {v}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            w.writeheader()
            w.writerows(trades)
        print(f"\n  Trades écrits dans {args.csv}")


if __name__ == "__main__":
    main()
