"""
Test bout en bout : strategie -> moteur -> ecriture gzip -> relecture backtest.

Fabrique un scenario synthetique contenant un trade gagnant (TP), un trade
perdant (SL) et un trade expire (TIMEOUT), puis verifie que le backtest les
retrouve tous les trois apres un aller-retour sur disque.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))

import config as C

TMP = tempfile.mkdtemp(prefix="obtest_")
C.DATA_DIR = os.path.join(TMP, "data")
VENUE = "kraken"
C.use_venue(VENUE)

import backtest
import collector
import features
import paper_engine
from strategy import ObiWallsStrategy

T0 = 1_754_000_000.0        # ancrage arbitraire

# --- coherence de la config de chaque plateforme ---
print("Fenetres derivees des frais :")
for v in C.VENUES:
    C.use_venue(v)          # leve ValueError si la fenetre est vide
    print(f"  {v:<8} frais {C.FEE_ROUNDTRIP_BPS:>3.0f} bps AR | "
          f"murs [{C.WALL_MIN_DIST_BPS:>5.1f}, {C.WALL_MAX_DIST_BPS:>5.1f}] bps | "
          f"TP min {C.MIN_TP_BPS:>5.1f} bps | "
          f"frais/R au stop mini {C.FEE_ROUNDTRIP_BPS / C.WALL_MIN_DIST_BPS:>4.0%}")
C.use_venue(VENUE)
print()

# Le mur du scenario doit tomber dans la fenetre de la plateforme testee.
WALL_BPS = (C.WALL_MIN_DIST_BPS + C.WALL_MAX_DIST_BPS) / 2
print(f"Scenario construit avec un mur a {WALL_BPS:.0f} bps "
      f"(fenetre {VENUE} : [{C.WALL_MIN_DIST_BPS:.0f}, {C.WALL_MAX_DIST_BPS:.0f}])")


def row(ts, mid, obi, bid_wall_bps=0.0, ask_wall_bps=0.0):
    """Fabrique une ligne de features coherente avec le schema COLUMNS."""
    r = {"ts": round(ts, 2), "best_bid": round(mid - 0.5, 2),
         "best_ask": round(mid + 0.5, 2), "mid": round(mid, 2),
         "microprice": round(mid, 4),
         "spread_bps": round(1.0 / mid * 10_000, 3)}
    for band in C.DEPTH_BANDS_BPS:
        r[f"bid_{band}"] = 1_000_000.0
        r[f"ask_{band}"] = 1_000_000.0
        r[f"obi_{band}"] = obi
    for side, bps in (("bid", bid_wall_bps), ("ask", ask_wall_bps)):
        sign = -1 if side == "bid" else 1
        px = round(mid * (1 + sign * bps / 10_000), 2) if bps else 0.0
        r[f"{side}_wall_px"]  = px
        r[f"{side}_wall_sz"]  = 2_000_000.0 if bps else 0.0
        r[f"{side}_wall_bps"] = bps
    return r


# ------------------------------------------------------------------ scenario
rows, ts, mid = [], T0, 60_000.0

def phase(n, obi=0.0, drift=0.0, wall=0.0):
    global ts, mid
    for _ in range(n):
        rows.append(row(ts, mid, obi, bid_wall_bps=wall))
        ts  += C.SNAPSHOT_INTERVAL
        mid += drift

# Le stop tombe a ~WALL_BPS + SL_BUFFER sous l'entree ; le TP a RR fois cette
# distance. Les derives ci-dessous sont dimensionnees pour atteindre l'un ou
# l'autre en ~150 snapshots.
risque_usd = 60_000 * (WALL_BPS + C.SL_BUFFER_BPS) / 10_000
derive_tp  = C.RR * risque_usd / 140
derive_sl  = risque_usd / 140

# 1) calme : rodage de l'EMA, aucun signal attendu
phase(120)
# 2) pression acheteuse + mur dans la fenetre -> ouverture longue
phase(60, obi=0.60, wall=WALL_BPS)
# 3) le prix monte jusqu'au TP
phase(150, obi=0.60, drift=+derive_tp, wall=WALL_BPS)
# 4) cooldown (600 s = 300 snapshots)
phase(320)
# 5) nouveau signal, puis chute jusqu'au SL
phase(60, obi=0.60, wall=WALL_BPS)
phase(150, obi=0.60, drift=-derive_sl, wall=WALL_BPS)
# 6) cooldown puis signal qui stagne -> TIMEOUT (MAX_HOLD_SEC = 4 h = 7200 snap.)
phase(320)
phase(60, obi=0.60, wall=WALL_BPS)
phase(7400, obi=0.60, wall=WALL_BPS)

print(f"Scenario : {len(rows):,} snapshots, "
      f"{(rows[-1]['ts'] - rows[0]['ts']) / 3600:.1f} h simulees\n")

# ------------------------------------------------- ecriture disque (gzip)
buf = list(rows)
while buf:
    chunk, buf = buf[:C.FLUSH_EVERY], buf[C.FLUSH_EVERY:]
    collector.flush(VENUE, chunk)          # flush() vide la liste qu'on lui passe

fichiers = []
for d, _, fs in os.walk(C.DATA_DIR):
    fichiers += [os.path.join(d, f) for f in fs]
taille = sum(os.path.getsize(f) for f in fichiers)
print(f"Ecriture : {len(fichiers)} fichiers .csv.gz, {taille / 1024:.0f} Ko "
      f"({taille / len(rows):.1f} octets/snapshot compresse)")
print(f"           -> {taille / len(rows) * 43200 / 1024 / 1024:.2f} Mo/jour a 2 s\n")

# ------------------------------------------------- relecture
relus = backtest.load_rows(VENUE)
print(f"Relecture : {len(relus):,} lignes (ecrites : {len(rows):,})")
assert len(relus) == len(rows), "perte de lignes a l'aller-retour gzip !"
assert set(relus[0]) == set(features.COLUMNS), "schema altere a la relecture"
ecarts = [k for k in features.COLUMNS if abs(relus[0][k] - rows[0][k]) > 1e-6]
assert not ecarts, f"valeurs alterees : {ecarts}"
print("           schema et valeurs intacts\n")

# ------------------------------------------------- backtest
stats, trades = backtest.run(relus, verbose=True)
print("\n--- statistiques ---")
for k, v in stats.items():
    print(f"  {k:<10} {v}")

print("\n--- trades ---")
for t in trades:
    print(f"  {t['side']:<4} {t['result']:<8} entree={t['entry']:>9.2f} "
          f"sortie={t['exit']:>9.2f} pnl={t['pnl']:>+8.2f}$ "
          f"frais={t['fees']:>6.2f}$ r={t['r']:>+5.2f} duree={t['hold_s']:>6}s")

resultats = [t["result"] for t in trades]
assert "TP" in resultats,      f"aucun TP declenche : {resultats}"
assert "SL" in resultats,      f"aucun SL declenche : {resultats}"
assert "TIMEOUT" in resultats, f"aucun TIMEOUT declenche : {resultats}"

tp = next(t for t in trades if t["result"] == "TP")
sl = next(t for t in trades if t["result"] == "SL")
assert tp["pnl"] > 0, "un TP devrait etre gagnant apres frais"
assert sl["pnl"] < 0, "un SL devrait etre perdant"
assert tp["fees"] > 0, "les frais doivent etre preleves"

# Le PnL doit reconcilier avec le capital final
capital = C.START_CAPITAL + sum(t["pnl"] for t in trades)
assert abs(capital - stats["capital"]) < 0.05, \
    f"incoherence capital : {capital:.2f} vs {stats['capital']}"
print(f"\n  Reconciliation capital OK : {capital:.2f}$")

shutil.rmtree(TMP, ignore_errors=True)
print("\nTOUS LES TESTS PIPELINE PASSENT")
