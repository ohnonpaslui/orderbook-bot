"""
Verifie que les compteurs du diagnostic survivent au redemarrage de session.

Une session GitHub Actions dure 4 h 55. Sans reprise, les compteurs
repartiraient de zero cinq fois par jour et la distribution de l'OBI
n'aurait jamais plus de cinq heures de profondeur — alors que la question
posee ne se tranche que sur plusieurs jours.
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))

import config as C

TMP = tempfile.mkdtemp(prefix="obcumul_")
C.STATE_DIR = TMP
C.use_venue(C.LIVE_VENUE)

from diagnostics import Observateur

CHEMIN = os.path.join(TMP, "diagnostics.json")


def ligne(ts, obi, mid=65000.0):
    # Le signal de confirmation est porte par le microprice, normalise par la
    # demi-fourchette (0.5 ici) : un decalage de 0.5*obi donne un signal = obi.
    r = {"ts": ts, "best_bid": mid - 0.5, "best_ask": mid + 0.5, "mid": mid,
         "microprice": mid + obi * 0.5, "spread_bps": 0.15}
    for b in C.DEPTH_BANDS_BPS:
        r[f"bid_{b}"] = 1e6
        r[f"ask_{b}"] = 1e6
        r[f"obi_{b}"] = obi
    for c in ("bid", "ask"):
        r[f"{c}_wall_px"] = r[f"{c}_wall_sz"] = r[f"{c}_wall_bps"] = 0.0
    return r


def session(n, obi, depart):
    """Une session : on court-circuite le reseau, seul le cumul est teste."""
    o = Observateur.__new__(Observateur)
    from strategy import SetupBookStrategy
    import candles as K
    o.venue, o.strat = C.LIVE_VENUE, SetupBookStrategy()
    o.tf_sec = K.TF_MS[C.TIMEFRAME] / 1000
    o.bougies, o.derniere_bougie = [], float("inf")   # jamais de rafraichissement
    o.erreur_structure = o.raison_setup = None
    o.premier_ts = o.dernier_ts = None
    o.chemin = CHEMIN
    o.cumul = o._charger()
    o.consommer([ligne(depart + i * 2.0, obi) for i in range(n)])
    return o.ecrire()


print("=== session 1 : 300 snapshots, OBI 0.10 ===")
d1 = session(300, 0.10, 1_786_000_000.0)
print(f"  cumul {d1['snapshots']:,} | sessions {d1['sessions']} | "
      f"OBI max {d1['obi_max']:.3f} | rejets {sum(d1['rejets'].values()):,}")
assert d1["snapshots"] == 300
assert d1["sessions"] == 1

print("\n=== session 2 : 500 snapshots, OBI 0.30 (plus fort) ===")
d2 = session(500, 0.30, 1_786_010_000.0)
print(f"  cumul {d2['snapshots']:,} | sessions {d2['sessions']} | "
      f"OBI max {d2['obi_max']:.3f} | rejets {sum(d2['rejets'].values()):,}")
assert d2["snapshots"] == 800, f"cumul perdu : {d2['snapshots']}"
assert d2["sessions"] == 2
assert d2["obi_max"] > d1["obi_max"], "le maximum doit progresser"
assert d2["session"]["snapshots"] == 500, "la session courante doit rester lisible"
assert sum(d2["obi_hist"]) == 800, "histogramme non cumule"
assert sum(d2["rejets"].values()) >= sum(d1["rejets"].values())
print("  cumul, histogramme et maximum conserves entre sessions")

print("\n=== session 3 : reprise apres coupure ===")
d3 = session(200, 0.05, 1_786_020_000.0)
print(f"  cumul {d3['snapshots']:,} | sessions {d3['sessions']}")
assert d3["snapshots"] == 1000
assert d3["obi_max"] >= d2["obi_max"], "le maximum ne doit jamais reculer"

print("\n=== table des seuils ===")
tot = sum(d3["obi_hist"])
paires = sorted(d3["obi_seuils"].items(), key=lambda x: float(x[0]))
for s, n in paires:
    print(f"  {s} -> {n:>5,} / {tot:,} ({n/tot*100:>5.1f} %)")
# Les seuils eux-memes suivent l'echelle du signal configure : on verifie la
# propriete, pas des valeurs figees. Un seuil plus haut ne peut pas etre
# franchi plus souvent qu'un seuil plus bas.
compte = [n for _, n in paires]
assert compte == sorted(compte, reverse=True), f"table non decroissante : {compte}"

print("\n=== changement de resolution -> remise a zero ===")
brut = json.load(open(CHEMIN, encoding="utf-8"))
brut["obi_pas"] = 0.05
brut["obi_hist"] = [0] * 20          # ancienne resolution
json.dump(brut, open(CHEMIN, "w", encoding="utf-8"))
d4 = session(100, 0.10, 1_786_030_000.0)
print(f"  cumul {d4['snapshots']:,} (attendu 100 : melange de resolutions evite)")
assert d4["snapshots"] == 100, "un histogramme incompatible doit repartir de zero"

shutil.rmtree(TMP, ignore_errors=True)
print("\nTOUS LES TESTS CUMUL PASSENT")
