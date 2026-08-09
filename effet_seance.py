"""
Effet de seance sur NQ — version statistiquement honnete.

Le premier test etait fausse : des rendements a 60 min mesures toutes les
5 min se recouvrent a 11/12. Les observations n'etant pas independantes, la
t-statistique etait gonflee d'un facteur ~3.

Ici : une bougie de 1h, un rendement de 1h, donc AUCUN recouvrement. Et sur
873 jours au lieu de 71, ce qui donne ~600 observations reellement
independantes par heure au lieu de 71 jours d'echantillon.

Le controle hors echantillon se fait par ANNEE, pas par moitie de fichier :
un biais qui tient sur deux annees distinctes est autrement plus credible.
"""
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, r"C:\Users\homaf\Desktop\orderbook-bot")
import candles as K
import chercher_signal as S

b = K.fetch_yahoo("NQ=F", "1h", verbose=False)
a = S.atr(b)
print(f"{len(b):,} bougies 1h — "
      f"{datetime.fromtimestamp(b[0]['ts']/1000, timezone.utc):%Y-%m-%d} -> "
      f"{datetime.fromtimestamp(b[-1]['ts']/1000, timezone.utc):%Y-%m-%d}\n")

# Rendement de LA bougie elle-meme, normalise par l'ATR precedent.
# Aucun recouvrement : chaque heure compte une fois.
par_heure = defaultdict(lambda: defaultdict(list))
for i in range(1, len(b)):
    if not a[i-1]:
        continue
    d = datetime.fromtimestamp(b[i]["ts"]/1000, timezone.utc)
    r = (b[i]["close"] - b[i]["open"]) / a[i-1]
    if abs(r) > 10:                      # bougie aberrante (rollover, halte)
        continue
    par_heure[d.hour][d.year].append(r)

annees = sorted({y for h in par_heure.values() for y in h})
print(f"annees couvertes : {annees}\n")
print(f"{'h UTC':>6} {'n':>5} {'moyenne':>10} {'t':>7} "
      + "".join(f"{y:>9}" for y in annees) + "  stable")
print("─" * (30 + 9*len(annees) + 9))

resultats = []
for h in sorted(par_heure):
    tous = [r for y in par_heure[h].values() for r in y]
    n = len(tous)
    if n < 200:
        continue
    m = statistics.fmean(tous)
    e = statistics.pstdev(tous) or 1e-9
    t = m / (e / math.sqrt(n))
    par_an = []
    for y in annees:
        v = par_heure[h].get(y, [])
        par_an.append(statistics.fmean(v) if len(v) > 40 else None)
    connus = [x for x in par_an if x is not None]
    stable = len(connus) >= 2 and all(x * connus[0] > 0 for x in connus)
    ligne = f"{h:>6} {n:>5} {m:>+10.4f} {t:>+7.2f} "
    ligne += "".join(f"{x:>+9.3f}" if x is not None else f"{'—':>9}"
                     for x in par_an)
    ligne += "  OUI" if stable and abs(t) > 2 else ""
    print(ligne)
    if stable and abs(t) > 2:
        resultats.append((abs(m), h, m, t, par_an))

atr_moy = statistics.fmean(x for x in a if x)
seuil = 1.24 / (atr_moy * 2)
print(f"\nATR moyen {atr_moy:.1f} pts = {atr_moy*2:.0f} $ par MNQ")
print(f"Commission A/R = {seuil:.4f} ATR — un biais doit le depasser largement.\n")

print("BIAIS HORAIRES STABLES SUR TOUTES LES ANNEES")
if not resultats:
    print("  aucun. Aucune heure ne produit un biais fiable et reproductible.")
else:
    resultats.sort(reverse=True)
    for amp, h, m, t, par_an in resultats:
        sens = "HAUSSE" if m > 0 else "BAISSE"
        gain = abs(m) * atr_moy * 2
        print(f"  {h:>2}h UTC  {sens}  {abs(m):.4f} ATR = {gain:.2f} $ par MNQ  "
              f"(t={t:+.2f}, net apres frais {gain-1.24:+.2f} $)")
    print(f"\n  {len(resultats)} heure(s) exploitable(s) sur 24.")
