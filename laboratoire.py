"""
laboratoire.py — Protocole de recherche. Rend le surapprentissage visible.

POURQUOI CE MODULE EXISTE

« Tester jusqu'à trouver » produit toujours un résultat : à force d'essayer,
une combinaison finit par briller sur le passé. C'est ce qui est arrivé aux
bots OTE et FVG-IFVG — de jolis chiffres, puis -99,9 % sur trois ans.

Ce module inscrit la discipline dans l'outil au lieu de la confier à la bonne
volonté. Trois mécanismes :

  1. REGISTRE DES ESSAIS. Chaque hypothèse testée est enregistrée. Plus on
     teste, plus la barre monte : chercher vingt fois relève le seuil de
     significativité, sinon on finit par trouver du bruit qui a l'air d'un
     signal. C'est la correction pour tests multiples, appliquée
     automatiquement plutôt que oubliée.

  2. RÉSERVE VERROUILLÉE. Les 30 % de données les plus récentes sont
     inaccessibles par défaut. On calibre sur le reste ; on ne déverrouille
     qu'une fois, sur un candidat final. Une réserve consultée dix fois
     n'est plus une réserve.

  3. ÉVALUATION STANDARD. Sans recouvrement des échantillons, stabilité par
     quart de période, et traduction en dollars frais compris. Les trois
     pièges qui ont produit trois faux résultats dans ce projet.

USAGE TYPE

    from laboratoire import Labo
    labo = Labo()
    labo.enregistrer("delta des gros ordres suit le prix a 2h")
    r = labo.evaluer(signal, rendements, nom="gros_delta_6b", horizon=24)
    print(labo.verdict(r))
"""

import json
import math
import os
import statistics
from datetime import datetime, timezone

REGISTRE = os.path.join("data", "registre_essais.json")
PART_RESERVE = 0.30       # part la plus récente, verrouillée


class Labo:

    def __init__(self, registre=REGISTRE):
        self.chemin = registre
        self.essais = self._charger()
        # Ce qui compte pour la correction, ce sont les MESURES effectuées,
        # pas les hypothèses déclarées. Un balayage de 22 candidats sur 4
        # horizons fait 88 tests, pas un seul : la barre doit monter en
        # conséquence. Constaté en production — un candidat à t = -3.15
        # semblait passer le seuil de 2.00, alors que 88 mesures exigeaient
        # 3.87. Il s'est effondré sur la réserve.
        self.mesures = 0

    # ------------------------------------------------------------- registre
    def _charger(self):
        try:
            with open(self.chemin, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _sauver(self):
        os.makedirs(os.path.dirname(self.chemin) or ".", exist_ok=True)
        with open(self.chemin, "w", encoding="utf-8") as f:
            json.dump(self.essais, f, indent=2, ensure_ascii=False)

    def enregistrer(self, hypothese, note=""):
        """
        Déclare une hypothèse AVANT de la tester.

        L'ordre compte : une hypothèse formulée après avoir vu le résultat
        n'est pas une hypothèse, c'est une description. Le registre garde la
        trace de ce qui a été tenté, et sert à corriger le seuil.
        """
        self.essais.append({
            "hypothese": hypothese, "note": note,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        })
        self._sauver()
        return len(self.essais)

    def seuil_t(self, extra=0):
        """
        Seuil de |t| exigé, corrigé du nombre d'essais déjà menés.

        Avec N tests indépendants, la probabilité qu'au moins un dépasse le
        seuil habituel par hasard vaut ~N x 5 %. On relève donc la barre
        (correction de Bonferroni) : plus on a cherché, plus il faut être
        convaincant. Plancher à 2.0, plafond à 4.0 — au-delà on ne mesure
        plus rien de réaliste sur des échantillons de cette taille.
        """
        n = max(1, len(self.essais) + getattr(self, "mesures", 0) + extra)
        # quantile normal approché pour alpha = 0.05 / n, test bilatéral
        alpha = 0.05 / n
        z = 2.0 if n == 1 else math.sqrt(2 * math.log(n / alpha))
        return max(2.0, min(4.0, z / 1.6))

    # -------------------------------------------------------------- reserve
    @staticmethod
    def decouper(donnees, part_reserve=PART_RESERVE):
        """
        Sépare travail et réserve. La réserve est la période la plus RÉCENTE.

        Prendre la réserve au début plutôt qu'à la fin flatterait le résultat :
        un signal se dégrade avec le temps, et le marché récent est celui sur
        lequel on tradera.
        """
        coupe = int(len(donnees) * (1 - part_reserve))
        return donnees[:coupe], donnees[coupe:]

    # ------------------------------------------------------------ evaluation
    def evaluer(self, signal, rendements, horizon, nom="", prix=None,
                frais_bps=10.0, atr=None):
        """
        Évaluation standard d'un candidat, avec les trois garde-fous.

        `signal` et `rendements` sont alignés, `rendements` mesuré à `horizon`
        pas en avant. On échantillonne tous les `horizon` pas pour supprimer
        le recouvrement — sans quoi la t-statistique est gonflée d'un facteur
        racine(horizon).
        """
        self.mesures += 1
        paires = [(s, r) for i, (s, r) in enumerate(zip(signal, rendements))
                  if i % horizon == 0 and s is not None and r is not None]
        n = len(paires)
        if n < 100:
            return {"nom": nom, "n": n, "erreur": "trop peu d'observations"}

        x = [p[0] for p in paires]
        y = [p[1] for p in paires]
        ic, t = _correlation(x, y)

        # Stabilité par quart : un signal réel garde son signe partout.
        q = n // 4
        parts = []
        for k in range(4):
            sous = paires[k*q:(k+1)*q] if k < 3 else paires[3*q:]
            if len(sous) > 20:
                parts.append(_correlation([p[0] for p in sous],
                                          [p[1] for p in sous])[0])
        stable = len(parts) == 4 and all(p * ic > 0 for p in parts)

        # Décile extrême : c'est là qu'on traderait. Un IC qui ne s'y retrouve
        # pas n'est pas exploitable, quelle que soit sa significativité.
        #
        # Le SENS compte. Un IC négatif n'est pas une absence d'avantage : il
        # dit qu'il faut trader à l'inverse du signal. On prend donc l'écart
        # entre les deux déciles, orienté par le signe de l'IC, et divisé par
        # deux puisqu'un trade n'exploite qu'un côté à la fois.
        # (Une version antérieure prenait -bas pour un IC négatif, ce qui
        # inversait le résultat et masquait un candidat valide.)
        tri = sorted(paires)
        k = max(1, n // 10)
        haut = statistics.fmean(p[1] for p in tri[-k:])
        bas = statistics.fmean(p[1] for p in tri[:k])
        decile = (haut - bas) / 2 * (1 if ic > 0 else -1)

        res = {"nom": nom, "n": n, "ic": ic, "t": t, "stable": stable,
               "parts": parts, "decile_atr": decile, "occasions": k}

        # Traduction en dollars : le seul chiffre qui décide.
        if prix and atr:
            gain = decile * atr
            frais = prix * frais_bps / 10_000
            res.update({"gain": gain, "frais": frais, "net": gain - frais,
                        "net_bps": (gain - frais) / prix * 10_000})
        return res

    def verdict(self, r):
        """Phrase de conclusion, seuil corrigé du nombre d'essais compris."""
        if r.get("erreur"):
            return f"  {r['nom']}: {r['erreur']}"
        seuil = self.seuil_t()
        lignes = [
            f"  {r['nom']}  n={r['n']:,}  IC {r['ic']:+.4f}  t={r['t']:+.2f}"
            f"  (seuil exige {seuil:.2f} apres {self.mesures} mesures)",
            f"    par quart : " + " ".join(f"{p:+.3f}" for p in r["parts"])
            + ("  stable" if r["stable"] else "  INSTABLE"),
        ]
        if "net" in r:
            lignes.append(
                f"    decile {r['decile_atr']:+.3f} ATR = {r['gain']:+.0f} $"
                f"  frais {r['frais']:.0f} $  NET {r['net']:+.0f} $"
                f" ({r['net_bps']:+.1f} bps)")
        ok = (abs(r["t"]) >= seuil and r["stable"]
              and r.get("net", -1) > 0)
        lignes.append("    => RETENU" if ok else "    => rejete")
        return "\n".join(lignes)

    def resume(self):
        return (f"{len(self.essais)} hypotheses declarees, "
                f"{self.mesures} mesures effectuees — "
                f"seuil de |t| exige : {self.seuil_t():.2f}")


def _correlation(x, y):
    n = len(x)
    if n < 30:
        return 0.0, 0.0
    mx, my = statistics.fmean(x), statistics.fmean(y)
    num = sum((a-mx)*(b-my) for a, b in zip(x, y))
    dx = math.sqrt(sum((a-mx)**2 for a in x))
    dy = math.sqrt(sum((b-my)**2 for b in y))
    if dx == 0 or dy == 0:
        return 0.0, 0.0
    r = num / (dx*dy)
    return r, r * math.sqrt(max(n-2, 1) / max(1e-12, 1 - r*r))


if __name__ == "__main__":
    labo = Labo()
    print(labo.resume())
    if labo.essais:
        print("\nHypotheses deja testees :")
        for i, e in enumerate(labo.essais, 1):
            print(f"  {i:>2}. [{e['date']}] {e['hypothese']}")
