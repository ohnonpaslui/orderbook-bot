"""
Valide la couche analyse technique sur de vraies bougies.

Deux objectifs :
  1. verifier qu'elle ne lit pas le futur (le piege classique des pivots) ;
  2. mesurer la LARGEUR DES STOPS qu'elle produit — c'est ce chiffre qui dit
     si la methode est compatible avec les frais, avant meme de parler carnet.
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))

import candles as K
import config as C
import technical as T

# Kraken ne renvoie que ~720 bougies OHLC quelle que soit la date demandee.
# Binance sert donc de source d'historique : la couche AT est de toute facon
# independante du support.
VENUE = "binance"
JOURS = 60

print("=== chargement ===")
bougies = K.fetch(VENUE, "5m", days=JOURS)
assert len(bougies) > 5000, f"pas assez de bougies : {len(bougies)}"
T.add_indicators(bougies)

# ---------------------------------------------------------------- 1. MM
print("\n=== 1. moyennes mobiles ===")
c = bougies[-1]
print(f"  cloture {c['close']:,.2f} | mm20 {c['mm20']:,.2f} | "
      f"mm50 {c['mm50']:,.2f} | mm200 {c['mm200']:,.2f} | atr {c['atr']:,.2f}")
assert bougies[T.MM_LONG - 2]["mm200"] is None, "mm200 ne doit pas exister trop tot"
assert bougies[T.MM_LONG - 1]["mm200"] is not None, "mm200 doit exister a l'indice 199"
# Verification independante de la MM200 sur la derniere bougie
attendu = sum(x["close"] for x in bougies[-T.MM_LONG:]) / T.MM_LONG
assert abs(c["mm200"] - attendu) < 0.01, f"mm200 fausse : {c['mm200']} vs {attendu}"
print("  valeurs et fenetre de chauffe correctes")

croisements = [T.croisement(bougies, i) for i in range(len(bougies))]
print(f"  golden cross : {croisements.count('golden')} | "
      f"death cross : {croisements.count('death')}")

# ---------------------------------------------- 2. absence de lecture du futur
print("\n=== 2. pas de lecture du futur ===")
# Un pivot pose en j ne doit etre visible qu'a partir de j + PIVOT_N.
i_test = len(bougies) // 2
hauts, bas = T._pivots_confirmes(bougies, i_test)
dernier = max([j for j, _ in hauts] + [j for j, _ in bas])
assert dernier <= i_test - T.PIVOT_N, \
    f"pivot visible trop tot : indice {dernier} vu depuis {i_test}"
print(f"  a l'indice {i_test}, dernier pivot connu = {dernier} "
      f"(ecart {i_test - dernier} >= {T.PIVOT_N})")

# Le calcul a l'instant i doit etre identique qu'on connaisse la suite ou non.
tronque = [dict(x) for x in bougies[:i_test + 1]]
T.add_indicators(tronque)
s_complet, _ = T.setup(bougies, i_test)
s_tronque, _ = T.setup(tronque, i_test)
memes = (s_complet is None) == (s_tronque is None)
if s_complet and s_tronque:
    memes = abs(s_complet["invalidation"] - s_tronque["invalidation"]) < 1e-6
assert memes, "le setup change selon qu'on connait le futur -> fuite"
print("  setup identique sur historique tronque")

# ---------------------------------------------------------------- 3. S/R
print("\n=== 3. zones S/R ===")
zones = T.sr_zones(bougies, len(bougies) - 1)
print(f"  {len(zones)} zones retenues (prix actuel {bougies[-1]['close']:,.0f})")
for z in zones[:5]:
    ecart = (z["prix"] - bougies[-1]["close"]) / bougies[-1]["close"] * 10_000
    print(f"    {z['prix']:>10,.0f}  {z['touches']:>2} touches  "
          f"{z['type']:<10} {ecart:>+8.0f} bps")
assert all(z["touches"] >= T.SR_MIN_TOUCHES for z in zones)

# ---------------------------------------------------------------- 4. setups
print("\n=== 4. balayage des setups ===")
setups, rejets = [], {}
for i in range(T.MM_LONG + T.PIVOT_N, len(bougies)):
    s, raison = T.setup(bougies, i)
    if s:
        setups.append((i, s))
    else:
        rejets[raison] = rejets.get(raison, 0) + 1

heures = len(bougies) * 5 / 60
print(f"  {len(setups):,} bougies en setup sur {len(bougies):,} "
      f"({heures / 24:.0f} jours)")
print("  rejets :")
for raison, n in sorted(rejets.items(), key=lambda x: -x[1]):
    print(f"    {n:>7,} x {raison}")
assert setups, "aucun setup detecte sur 60 jours — la couche AT ne sert a rien"

# Setups consecutifs = une meme zone visitee ; on compte les episodes distincts.
episodes = 1 + sum(1 for a, b in zip(setups, setups[1:]) if b[0] - a[0] > 12)
print(f"  soit ~{episodes} episodes distincts, ~{episodes / (heures / 24):.1f}/jour")

# ------------------------------------------- 5. largeur des stops (le point cle)
print("\n=== 5. largeur des stops produits ===")
stops_bps = []
for i, s in setups:
    entree = bougies[i]["close"]
    stop = s["invalidation"]
    stops_bps.append(abs(entree - stop) / entree * 10_000)

tries = sorted(stops_bps)
med = statistics.median(stops_bps)
print(f"  mediane {med:.0f} bps | p10 {tries[len(tries)//10]:.0f} | "
      f"p90 {tries[9*len(tries)//10]:.0f} bps")
print(f"\n  {'plateforme':<26} {'frais AR':>9} {'frais/R au stop median':>24}")
for nom, ar in (("Kraken taker 0.26%", 52), ("Binance taker 0.10%", 20),
                ("MNQ futures (~1.25$/AR)", None)):
    if ar is None:
        # MNQ : 1 pt = 2$, stop median en points a ~23700
        stop_pts = med / 10_000 * 23_700 / 1.0
        risque = stop_pts * 2.0
        print(f"  {nom:<26} {'~1.25$':>9} {1.25 / risque:>23.1%}")
    else:
        print(f"  {nom:<26} {ar:>6} bps {ar / med:>23.1%}")

print("\nTOUS LES TESTS TECHNICAL PASSENT")
