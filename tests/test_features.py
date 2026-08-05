"""Test de features.compute sur des carnets synthetiques."""
import os
import sys
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))

import config as C
C.use_venue("kraken")
import features

MID  = 60000.0
TICK = 1.0          # 1$ sur 60000 = 0.167 bps par niveau
SIZE = 0.05
N    = 600          # 600 niveaux = 100 bps d'etendue, comme le vrai carnet a 500

def bps_to_idx(bps):
    return int(bps / 10_000 * MID / TICK)

def book(bid_extra=None, ask_extra=None, third_field=False):
    """Carnet plat, avec injection optionnelle d'un mur (indice, quantite)."""
    mk = ((lambda p, a: [p, a, 1_700_000_000]) if third_field else (lambda p, a: [p, a]))
    bids = [mk(MID - 0.5 - i * TICK, SIZE) for i in range(N)]
    asks = [mk(MID + 0.5 + i * TICK, SIZE) for i in range(N)]
    if bid_extra:
        bids[bid_extra[0]][1] = bid_extra[1]
    if ask_extra:
        asks[ask_extra[0]][1] = ask_extra[1]
    return {"bids": bids, "asks": asks}

print("=== 1. carnet equilibre, sans mur ===")
r = features.compute(book(), 1_700_000_000.0)
assert r is not None
print(f"  mid={r['mid']}  spread={r['spread_bps']} bps  obi_10={r['obi_10']}")
# L'OBI est pondere en notionnel : un carnet symetrique en quantite donne un
# OBI legerement negatif (~-5e-4). Structurel, negligeable devant le seuil 0.35.
assert abs(r["obi_10"]) < 0.01, f"OBI devrait etre ~nul, vaut {r['obi_10']}"
assert r["bid_wall_px"] == 0 and r["ask_wall_px"] == 0, "aucun mur attendu"
print(f"  profondeur 10bps : bid={r['bid_10']:,.0f}$  ask={r['ask_10']:,.0f}$")

print("\n=== 2. mur d'achat a ~80 bps sous le mid ===")
i80 = bps_to_idx(80)
r = features.compute(book(bid_extra=(i80, 5.0)), 1_700_000_000.0)
print(f"  bid_wall_px={r['bid_wall_px']}  sz={r['bid_wall_sz']:,.0f}$  "
      f"dist={r['bid_wall_bps']} bps")
assert r["bid_wall_px"] > 0, "le mur aurait du etre detecte"
assert 75 < r["bid_wall_bps"] < 85, f"distance inattendue: {r['bid_wall_bps']}"
assert r["ask_wall_px"] == 0, "pas de mur cote ask attendu"

print("\n=== 3. mur au-dela de WALL_BAND_BPS -> ignore ===")
i200 = bps_to_idx(C.WALL_BAND_BPS + 50)
b = book()
b["bids"] += [[MID - 0.5 - i * TICK, SIZE] for i in range(N, i200 + 5)]
b["bids"][i200][1] = 5.0
r = features.compute(b, 1_700_000_000.0)
print(f"  bid_wall_px={r['bid_wall_px']} (attendu 0 : mur hors bande {C.WALL_BAND_BPS})")
assert r["bid_wall_px"] == 0

print("\n=== 4. mur trop petit -> ignore ===")
r = features.compute(book(bid_extra=(i80, 0.10)), 1_700_000_000.0)   # 2x la mediane
print(f"  bid_wall_px={r['bid_wall_px']} (attendu 0 : 2x < WALL_MIN_MULT={C.WALL_MIN_MULT})")
assert r["bid_wall_px"] == 0

print("\n=== 5. desequilibre acheteur -> OBI positif ===")
b = book()
for i in range(bps_to_idx(10)):
    b["bids"][i][1] = 0.20      # 4x plus de bids dans les 10 premiers bps
r = features.compute(b, 1_700_000_000.0)
print(f"  obi_5={r['obi_5']}  obi_10={r['obi_10']}  obi_50={r['obi_50']}")
assert r["obi_10"] > 0.5, f"OBI devrait etre fortement positif: {r['obi_10']}"

print("\n=== 6. microprice penche vers le cote mince ===")
b = book()
b["bids"][0][1] = 5.0           # grosse demande, offre mince -> le prix monte
r = features.compute(b, 1_700_000_000.0)
print(f"  mid={r['mid']}  microprice={r['microprice']}")
assert r["microprice"] > r["mid"], "microprice devrait pencher vers le haut"

print("\n=== 7. niveaux a 3 champs (format Kraken) ===")
r = features.compute(book(bid_extra=(i80, 5.0), third_field=True), 1_700_000_000.0)
assert r is not None and r["bid_wall_px"] > 0
print(f"  OK — mur detecte a {r['bid_wall_bps']} bps malgre le timestamp")

print("\n=== 8. carnets invalides -> None ===")
assert features.compute({"bids": [], "asks": []}, 0) is None
assert features.compute({"bids": [[100, 1]] * 6, "asks": [[99, 1]] * 6}, 0) is None, \
    "carnet croise doit etre rejete"
print("  OK")

print("\n=== 9. coherence du schema COLUMNS ===")
r = features.compute(book(), 1_700_000_000.0)
manquantes = set(features.COLUMNS) - set(r)
en_trop = set(r) - set(features.COLUMNS)
print(f"  {len(features.COLUMNS)} colonnes ; manquantes={manquantes} en_trop={en_trop}")
assert not manquantes and not en_trop

print("\nTOUS LES TESTS FEATURES PASSENT")
