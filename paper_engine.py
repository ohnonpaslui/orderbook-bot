"""
paper_engine.py — Paper trading au tick (un snapshot de carnet = un tick).

Différence de fond avec le moteur des bots bougies : ici le PnL est calculé en
dollars réels à partir d'une quantité et de prix d'exécution, pas en multiples
de R. C'est indispensable parce que les frais Kraken (0.26 % par côté) sont du
même ordre de grandeur que le mouvement visé : les traiter comme un pourcentage
du risque, et non du notionnel, sous-estimerait leur poids d'un facteur ~50.

Les sorties se font au prix réellement disponible : on vend sur la demande
(best_bid) et on achète sur l'offre (best_ask), jamais au mid.

État persisté dans state/<bot>.json, trades dans trades/<bot>.csv
"""

import csv
import json
import os
from datetime import datetime, timezone

import config as C


# ----------------------------- Persistance ------------------------------------
def load_state(bot):
    path = os.path.join(C.STATE_DIR, f"{bot}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"capital": C.START_CAPITAL, "position": None, "wins": 0, "losses": 0,
            "fees_paid": 0.0, "updated": None}


def save_state(bot, state):
    os.makedirs(C.STATE_DIR, exist_ok=True)
    state["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(C.STATE_DIR, f"{bot}.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def append_trade(bot, row):
    os.makedirs(C.TRADES_DIR, exist_ok=True)
    path = os.path.join(C.TRADES_DIR, f"{bot}.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            w.writeheader()
        w.writerow(row)


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------- Cycle ------------------------------------------
def step(bot, state, row, signal, strat, record=True):
    """
    Un tick pour un bot : gestion de la position ouverte d'abord, puis
    ouverture éventuelle. Retourne (state, changed, events).

    `strat` sert uniquement à armer le cooldown à la clôture.
    `record=False` désactive l'écriture disque (utilisé par le backtest, qui
    collecte les trades en mémoire).
    """
    changed, events = False, []
    pos = state.get("position")
    ts  = row["ts"]

    # ---- sortie : SL, TP ou expiration ----
    if pos:
        if pos["side"] == "buy":
            exit_px = row["best_bid"]                # on sort en vendant
            hit_sl  = exit_px <= pos["sl"]
            hit_tp  = exit_px >= pos["tp"]
        else:
            exit_px = row["best_ask"]                # on sort en rachetant
            hit_sl  = exit_px >= pos["sl"]
            hit_tp  = exit_px <= pos["tp"]

        expired = (ts - pos["opened_ts"]) >= C.MAX_HOLD_SEC
        if hit_sl or hit_tp or expired:
            # SL prioritaire : dans le doute sur un même tick, on suppose le
            # scénario défavorable plutôt que de gonfler artificiellement les stats.
            outcome = "SL" if hit_sl else ("TP" if hit_tp else "TIMEOUT")
            direction = 1 if pos["side"] == "buy" else -1
            qty = pos["qty"]

            gross    = qty * (exit_px - pos["entry"]) * direction
            fee_exit = qty * exit_px * C.FEE_PCT_PER_SIDE / 100
            pnl      = gross - pos["fee_entry"] - fee_exit

            state["capital"]   = round(state["capital"] + pnl, 2)
            state["fees_paid"] = round(state.get("fees_paid", 0.0)
                                       + pos["fee_entry"] + fee_exit, 2)
            if pnl > 0:
                state["wins"] = state.get("wins", 0) + 1
            else:
                state["losses"] = state.get("losses", 0) + 1

            trade = {
                "opened": _iso(pos["opened_ts"]), "closed": _iso(ts),
                "side": pos["side"], "entry": pos["entry"], "sl": pos["sl"],
                "tp": pos["tp"], "exit": round(exit_px, 2), "result": outcome,
                "qty": round(qty, 6), "gross": round(gross, 2),
                "fees": round(pos["fee_entry"] + fee_exit, 2),
                "pnl": round(pnl, 2),
                "r": round(pnl / pos["risk_usd"], 2) if pos["risk_usd"] else 0.0,
                "hold_s": int(ts - pos["opened_ts"]),
                "capital": state["capital"],
                "wall_px": pos.get("wall_px", 0), "obi_ema": pos.get("obi_ema", 0),
            }
            if record:
                append_trade(bot, trade)
            state.setdefault("last_trades", [])
            state["last_trades"] = (state["last_trades"] + [trade])[-20:]

            state["position"] = None
            pos, changed = None, True
            strat.notify_close(ts)
            events.append(f"[{bot}] {outcome} @ {exit_px:.2f} — "
                          f"PnL {pnl:+.2f}$ ({trade['r']:+.2f}R) — "
                          f"capital {state['capital']:.2f}$")

    # ---- entrée sur signal ----
    if pos is None and signal:
        risk_px = abs(signal["entry"] - signal["sl"])
        if risk_px <= 0:
            return state, changed, events

        risk_usd = state["capital"] * C.RISK_PER_TRADE / 100
        qty      = risk_usd / risk_px

        # Plafond de levier : si le stop est très serré, on réduit la taille
        # plutôt que de prendre un notionnel disproportionné.
        max_notional = state["capital"] * C.MAX_NOTIONAL_MULT
        if qty * signal["entry"] > max_notional:
            qty      = max_notional / signal["entry"]
            risk_usd = qty * risk_px

        fee_entry = qty * signal["entry"] * C.FEE_PCT_PER_SIDE / 100
        state["position"] = {**signal, "qty": qty, "risk_usd": risk_usd,
                             "fee_entry": fee_entry, "opened_ts": ts}
        changed = True
        events.append(f"[{bot}] {signal['side'].upper()} @ {signal['entry']:.2f} "
                      f"(SL {signal['sl']:.2f} / TP {signal['tp']:.2f}) "
                      f"mur {signal['wall_px']:.0f} — OBI {signal['obi_ema']:+.2f}")

    return state, changed, events
