"""
run_bot.py — Phase 3 : bot en paper trading temps réel.

Fait exactement ce que fait le collecteur (snapshot + écriture dans data/) et,
en plus, alimente la stratégie et le moteur de paper trading. C'est voulu :
un seul processus, un seul appel API par tick, et la collecte ne s'arrête
jamais — on continue d'accumuler des données pour recalibrer plus tard.

=> Quand ce workflow est actif, DÉSACTIVER le workflow « Collecte carnet »,
   sinon deux processus écrivent dans data/ et se marchent dessus sur git.

Lancement :
  local          : python run_bot.py
  GitHub Actions : GIT_PUSH=1 MAX_RUNTIME=17700 python run_bot.py
"""

import os
import time
from datetime import datetime, timezone

import ccxt

import candles as K
import collector
import config as C
import features
import paper_engine
import technical as T
from strategy import SetupBookStrategy

MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME", "0"))
GIT_PUSH    = os.environ.get("GIT_PUSH") == "1"


def git_commit(message):
    collector.git_commit(message, paths=(C.DATA_DIR, C.STATE_DIR, C.TRADES_DIR))


def main():
    start = time.time()
    C.use_venue(C.LIVE_VENUE)       # fixe frais, symbole, fenêtre de murs
    ex    = getattr(ccxt, C.EXCHANGE)({"enableRateLimit": True, "timeout": 25000})
    strat = SetupBookStrategy()
    state = paper_engine.load_state(C.BOT_ID)

    # La structure ne bouge qu'à la clôture d'une bougie : inutile de
    # retélécharger l'historique à chaque snapshot de carnet.
    tf_sec = K.TF_MS[C.TIMEFRAME] / 1000

    def rafraichir_structure():
        bougies = K.fetch(C.VENUE, C.TIMEFRAME, days=30, verbose=False)
        T.add_indicators(bougies)
        raison = strat.update_candles(bougies)
        etat = ("aucun setup" if strat.setup is None
                else f"setup {'LONG' if strat.setup['direction'] > 0 else 'SHORT'} "
                     f"zone {strat.setup['zone'][0]:.0f}-{strat.setup['zone'][1]:.0f}")
        print(f"[structure] {etat}" + (f" ({raison})" if raison else ""), flush=True)
        return bougies[-1]["ts"] / 1000 if bougies else 0.0

    derniere_bougie = rafraichir_structure()

    buffer, n_ok, n_err = [], 0, 0
    last_commit  = time.time()
    pending_push = False

    pos = state.get("position")
    print(f"Bot {C.BOT_ID} demarre — {C.VENUE} {C.SYMBOL} — "
          f"capital {state['capital']:.2f}$ — "
          f"position {'ouverte ' + pos['side'] if pos else 'aucune'} — "
          f"frais {C.FEE_ROUNDTRIP_BPS:.0f} bps AR, murs "
          f"[{C.WALL_MIN_DIST_BPS:.0f}, {C.WALL_MAX_DIST_BPS:.0f}] bps", flush=True)

    while True:
        cycle_start = time.time()
        if MAX_RUNTIME and cycle_start - start > MAX_RUNTIME:
            print("Duree max atteinte, arret propre.", flush=True)
            break

        try:
            book = ex.fetch_order_book(C.SYMBOL, limit=C.BOOK_DEPTH)
            row  = features.compute(book, cycle_start)
        except Exception as e:
            n_err += 1
            if n_err % 10 == 1:
                print(f"[data] {type(e).__name__}: {e}", flush=True)
            time.sleep(5)
            continue

        if row is None:
            n_err += 1
            time.sleep(C.SNAPSHOT_INTERVAL)
            continue

        buffer.append(row)
        n_ok += 1

        # Nouvelle bougie clôturée -> on recalcule le setup technique.
        if cycle_start >= derniere_bougie + 2 * tf_sec:
            try:
                derniere_bougie = rafraichir_structure()
            except Exception as e:
                print(f"[structure] {type(e).__name__}: {e}", flush=True)
                derniere_bougie = cycle_start   # on retentera au prochain pas

        # L'EMA de l'OBI doit voir chaque snapshot, y compris pendant une
        # position ouverte — sinon elle repart de zéro à chaque sortie.
        signal = strat.update(row)
        if state.get("position"):
            signal = None

        state, changed, events = paper_engine.step(C.BOT_ID, state, row, signal, strat)
        if changed:
            paper_engine.save_state(C.BOT_ID, state)
            pending_push = True
            now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            for e in events:
                print(f"{now} UTC  {e}", flush=True)

        if len(buffer) >= C.FLUSH_EVERY:
            collector.flush(C.VENUE, buffer)

        # Un événement de trading est poussé tout de suite (le dashboard le
        # voit dans la minute) ; sinon on s'en tient à la cadence de commit.
        due = time.time() - last_commit >= C.COMMIT_EVERY
        if GIT_PUSH and (pending_push or due):
            collector.flush(C.VENUE, buffer)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            msg = (" | ".join(events) if events
                   else f"Collecte {C.SYMBOL} — {now} UTC ({n_ok} snapshots)")
            git_commit(msg)
            last_commit, pending_push = time.time(), False
            if not events:
                print(f"{now} UTC  {n_ok} snapshots, {n_err} erreurs — "
                      f"capital {state['capital']:.2f}$ — "
                      f"dernier filtre : {strat.last_reject}", flush=True)

        time.sleep(max(0.0, C.SNAPSHOT_INTERVAL - (time.time() - cycle_start)))

    collector.flush(C.VENUE, buffer)
    paper_engine.save_state(C.BOT_ID, state)
    if GIT_PUSH:
        git_commit(f"Fin de session — capital {state['capital']:.2f}$")
    print(f"Termine : {n_ok} snapshots, {n_err} erreurs, "
          f"capital {state['capital']:.2f}$", flush=True)


if __name__ == "__main__":
    main()
