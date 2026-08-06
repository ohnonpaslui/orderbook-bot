"""
Test d'integration : vraies bougies -> setup technique -> confirmation carnet
-> ouverture -> TP / SL / TIMEOUT -> ecriture gzip -> relecture backtest.

Les bougies sont REELLES (donc les setups aussi) ; seuls les snapshots de
carnet sont fabriques, puisqu'aucun historique de carnet n'existe. C'est le
seul moyen de tester le raccord entre les deux couches avant la fin de la
collecte.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))

import config as C

TMP = tempfile.mkdtemp(prefix="obtest_")
VENUE = C.LIVE_VENUE
C.use_venue(VENUE)

import backtest
import candles as K
import collector
import features
import technical as T

print("=== coherence des plateformes ===")
for v in C.VENUES:
    C.use_venue(v)
    print(f"  {v:<16} frais {C.FEE_ROUNDTRIP_BPS:>3.0f} bps AR | "
          f"stop min {C.MIN_STOP_BPS:>5.1f} bps | TP min {C.MIN_TP_BPS:>5.1f} bps")
C.use_venue(VENUE)

# ------------------------------------------------------ 1. un vrai setup
print(f"\n=== 1. recherche d'un setup reel ({VENUE}, {C.TIMEFRAME}) ===")
bougies = K.fetch(VENUE, C.TIMEFRAME, days=120, verbose=False)
T.add_indicators(bougies)

candidat = None
for i in range(len(bougies) - 400, T.MM_LONG + T.PIVOT_N, -1):
    s, _ = T.setup(bougies, i)
    if not s:
        continue
    entree = bougies[i]["close"]
    stop_bps = abs(entree - s["invalidation"]) / entree * 10_000
    if stop_bps >= C.MIN_STOP_BPS * 1.2:       # assez large pour passer le filtre
        candidat = (i, s, entree, stop_bps)
        break
assert candidat, "aucun setup exploitable trouve dans l'historique"

i, s, prix_ref, stop_bps = candidat
sens = "LONG" if s["direction"] > 0 else "SHORT"
print(f"  indice {i} — {sens} — zone {s['zone'][0]:,.0f}-{s['zone'][1]:,.0f}")
print(f"  invalidation {s['invalidation']:,.0f} — stop {stop_bps:.0f} bps "
      f"(min requis {C.MIN_STOP_BPS:.0f}) — S/R {s['sr']['prix']:,.0f} "
      f"({s['sr']['touches']} touches)")

# ------------------------------------------------ 2. carnet qui confirme
tf_sec = K.TF_MS[C.TIMEFRAME] / 1000
# Les snapshots doivent tomber APRES la cloture de la bougie i, sinon le
# backtest n'aura pas encore arme ce setup.
t0 = bougies[i]["ts"] / 1000 + tf_sec + 1

obi_confirme = 0.60 * s["direction"]
buf = C.SL_BUFFER_ATR * (s["atr"] or 0.0)
if s["direction"] > 0:
    sl = s["invalidation"] - buf
    risque = prix_ref - sl
    cible = prix_ref + C.RR * risque
else:
    sl = s["invalidation"] + buf
    risque = sl - prix_ref
    cible = prix_ref - C.RR * risque


def ligne(ts, mid, obi):
    r = {"ts": round(ts, 2), "best_bid": round(mid - 0.5, 2),
         "best_ask": round(mid + 0.5, 2), "mid": round(mid, 2),
         "microprice": round(mid, 4), "spread_bps": round(1.0 / mid * 10_000, 3)}
    for bande in C.DEPTH_BANDS_BPS:
        r[f"bid_{bande}"] = 1_000_000.0
        r[f"ask_{bande}"] = 1_000_000.0
        r[f"obi_{bande}"] = obi
    for cote in ("bid", "ask"):
        r[f"{cote}_wall_px"] = 0.0
        r[f"{cote}_wall_sz"] = 0.0
        r[f"{cote}_wall_bps"] = 0.0
    return r


def phase(rows, ts, mid, n, obi, derive=0.0):
    for _ in range(n):
        rows.append(ligne(ts, mid, obi))
        ts += C.SNAPSHOT_INTERVAL
        mid += derive
    return ts, mid


print("\n=== 2. scenarios de carnet ===")
scenarios = {}
for nom, atteindre in (("TP", cible), ("SL", sl)):
    rows, ts, mid = [], t0, prix_ref
    ts, mid = phase(rows, ts, mid, 60, obi_confirme)          # confirmation
    derive = (atteindre - mid) / 200 * 1.2                    # marche vers la cible
    ts, mid = phase(rows, ts, mid, 200, obi_confirme, derive)
    scenarios[nom] = rows

# TIMEOUT : confirmation puis stagnation au-dela de MAX_HOLD_SEC
rows, ts, mid = [], t0, prix_ref
ts, mid = phase(rows, ts, mid, 60, obi_confirme)
ts, mid = phase(rows, ts, mid, int(C.MAX_HOLD_SEC / C.SNAPSHOT_INTERVAL) + 60,
                obi_confirme)
scenarios["TIMEOUT"] = rows

for nom, rows in scenarios.items():
    print(f"  {nom:<8} {len(rows):>5} snapshots, "
          f"{rows[0]['mid']:,.0f} -> {rows[-1]['mid']:,.0f}")

# --------------------------------------- 3. aller-retour disque + backtest
print("\n=== 3. ecriture gzip / relecture / backtest ===")
resultats = {}
for nom, rows in scenarios.items():
    C.DATA_DIR = os.path.join(TMP, nom)          # un dossier par scenario
    buf = list(rows)
    while buf:
        chunk, buf = buf[:C.FLUSH_EVERY], buf[C.FLUSH_EVERY:]
        collector.flush(VENUE, chunk)

    relus = backtest.load_rows(VENUE)
    assert len(relus) == len(rows), f"{nom} : {len(relus)} relus / {len(rows)} ecrits"
    assert set(relus[0]) == set(features.COLUMNS), f"{nom} : schema altere"

    stats, trades = backtest.run(relus, bougies=bougies)
    resultats[nom] = (stats, trades)
    if trades:
        t = trades[0]
        print(f"  {nom:<8} {t['side']:<4} {t['result']:<8} "
              f"entree {t['entry']:>10,.2f} sortie {t['exit']:>10,.2f} "
              f"pnl {t['pnl']:>+7.2f}$ frais {t['fees']:>5.2f}$ "
              f"r {t['r']:>+5.2f} duree {t['hold_s']:>6}s")
    else:
        print(f"  {nom:<8} aucun trade")

# ------------------------------------------------------------ 4. controles
print("\n=== 4. controles ===")
for nom in ("TP", "SL", "TIMEOUT"):
    stats, trades = resultats[nom]
    assert trades, f"{nom} : aucun trade declenche"
    assert trades[0]["result"] == nom, \
        f"{nom} : resultat obtenu = {trades[0]['result']}"
    capital = C.START_CAPITAL + sum(t["pnl"] for t in trades)
    assert abs(capital - stats["capital"]) < 0.05, f"{nom} : capital incoherent"
print("  les trois sorties (TP, SL, TIMEOUT) se declenchent")
print("  capital reconcilie sur les trois scenarios")

tp = resultats["TP"][1][0]
sl_t = resultats["SL"][1][0]
assert tp["pnl"] > 0, "un TP doit rester gagnant apres frais"
assert sl_t["pnl"] < 0, "un SL doit etre perdant"
print(f"  frais {tp['fees']:.2f}$ sur un TP a {tp['r']:+.2f}R "
      f"(stop {stop_bps:.0f} bps)")

# Le carnet doit vraiment servir de filtre : sans confirmation, rien ne passe.
print("\n=== 5. le carnet filtre-t-il ? ===")
C.DATA_DIR = os.path.join(TMP, "sansconf")
rows, ts, mid = [], t0, prix_ref
ts, mid = phase(rows, ts, mid, 260, 0.0)         # OBI neutre = pas de confirmation
buf = list(rows)
while buf:
    chunk, buf = buf[:C.FLUSH_EVERY], buf[C.FLUSH_EVERY:]
    collector.flush(VENUE, chunk)
stats, trades = backtest.run(backtest.load_rows(VENUE), bougies=bougies)
assert not trades, f"un trade s'est ouvert sans confirmation du carnet : {trades}"
print("  OBI neutre -> aucun trade : le carnet filtre bien")

shutil.rmtree(TMP, ignore_errors=True)
print("\nTOUS LES TESTS PIPELINE PASSENT")
