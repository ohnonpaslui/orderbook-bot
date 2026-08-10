"""
Le protocole detecte-t-il ce qu'il est cense detecter ?

On lui soumet des cas dont on connait la reponse : un signal reel, du bruit
pur, un signal instable qui ne vaut que sur une periode, et un signal reel
mais trop faible pour couvrir les frais. Un protocole qui ne distingue pas
ces quatre cas ne sert a rien.
"""
import os
import random
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))

from laboratoire import Labo

TMP = tempfile.mkdtemp(prefix="labo_")
labo = Labo(os.path.join(TMP, "registre.json"))
random.seed(7)

N, HZ = 4000, 8
PRIX, ATR = 69_000.0, 125.0


def fabriquer(force, dérive=0.0, seulement_debut=False):
    """Signal et rendements avec une correlation controlee."""
    sig, ren = [], []
    for i in range(N):
        s = random.gauss(0, 1)
        f = force if not (seulement_debut and i > N // 4) else 0.0
        sig.append(s)
        ren.append(f * s + random.gauss(0, 1) + dérive)
    return sig, ren


print("=== 1. bruit pur : doit etre rejete ===")
s, r = fabriquer(0.0)
res = labo.evaluer(s, r, HZ, nom="bruit", prix=PRIX, atr=ATR)
print(labo.verdict(res))
assert abs(res["t"]) < 3, f"le bruit ne doit pas etre significatif : t={res['t']}"

print("\n=== 2. signal fort et constant : doit ressortir ===")
s, r = fabriquer(0.45)
res = labo.evaluer(s, r, HZ, nom="signal_fort", prix=PRIX, atr=ATR)
print(labo.verdict(res))
assert abs(res["t"]) > 3, "un signal fort doit etre detecte"
assert res["stable"], "un signal constant doit etre stable par quart"

print("\n=== 3. signal present sur un seul quart : doit etre INSTABLE ===")
s, r = fabriquer(0.9, seulement_debut=True)
res = labo.evaluer(s, r, HZ, nom="signal_episodique", prix=PRIX, atr=ATR)
print(labo.verdict(res))
assert not res["stable"], \
    "un signal limite a une periode doit etre signale instable"

print("\n=== 4. signal reel mais trop faible pour les frais ===")
s, r = fabriquer(0.06)
res = labo.evaluer(s, r, HZ, nom="signal_faible", prix=PRIX, atr=ATR,
                   frais_bps=10.0)
print(labo.verdict(res))
assert res["net"] < 0, "un signal faible doit etre negatif apres frais"

print("\n=== 4bis. signal INVERSE : un IC negatif reste exploitable ===")
# Un IC negatif ne veut pas dire « pas d'avantage » mais « trader a l'envers ».
# Une version anterieure inversait le calcul et masquait un candidat valide.
s, r = fabriquer(-0.45)
res = labo.evaluer(s, r, HZ, nom="signal_inverse", prix=PRIX, atr=ATR)
print(labo.verdict(res))
assert res["ic"] < 0, "le signal de test doit bien etre negativement correle"
assert res["decile_atr"] > 0, \
    f"un IC negatif exploite a l'envers doit donner un gain positif, " \
    f"obtenu {res['decile_atr']:+.3f}"
assert res["net"] > 0, "ce signal est assez fort pour couvrir les frais"

print("\n=== 5. le recouvrement est-il bien supprime ? ===")
s, r = fabriquer(0.2)
avec = labo.evaluer(s, r, 1, nom="horizon_1")
sans = labo.evaluer(s, r, HZ, nom=f"horizon_{HZ}")
print(f"  echantillonnage tous les 1 pas  : {avec['n']:,} observations")
print(f"  echantillonnage tous les {HZ} pas : {sans['n']:,} observations")
assert sans["n"] < avec["n"] / (HZ - 1), \
    "l'echantillonnage doit reduire le nombre d'observations d'un facteur ~horizon"

print("\n=== 6. le seuil monte-t-il avec le nombre d'essais ? ===")
seuils = []
for k in range(1, 41):
    labo.enregistrer(f"hypothese de test numero {k}")
    if k in (1, 5, 10, 20, 40):
        seuils.append((k, labo.seuil_t()))
        print(f"  apres {k:>2} essais : |t| exige >= {labo.seuil_t():.2f}")
assert seuils[-1][1] > seuils[0][1], \
    "tester davantage doit relever la barre"
assert seuils[0][1] >= 2.0, "le seuil ne doit jamais descendre sous 2.0"

print("\n=== 7. la reserve est-elle bien la periode la plus recente ? ===")
donnees = list(range(1000))
travail, reserve = Labo.decouper(donnees)
print(f"  travail {len(travail)} points, reserve {len(reserve)} points")
assert reserve[0] > travail[-1], "la reserve doit suivre la periode de travail"
assert len(reserve) == 300, f"30 % attendus, obtenu {len(reserve)}"

print(f"\n{labo.resume()}")
shutil.rmtree(TMP, ignore_errors=True)
print("\nTOUS LES TESTS DU PROTOCOLE PASSENT")
