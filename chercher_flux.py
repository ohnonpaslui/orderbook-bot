"""
chercher_flux.py — Le flux d'ordres prédit-il quelque chose ?

Même protocole que `chercher_signal.py`, appliqué cette fois aux indicateurs
que les scalpeurs order flow revendiquent : delta, CVD, absorption, poids des
grosses transactions. Ce sont les seules données de ce type qui soient
gratuites et actuelles — le carnet, lui, coûterait 290 $/mois sans historique.

L'HYPOTHÈSE À TESTER, formulée avant de regarder les résultats :

  - le DELTA (agression nette) devrait prédire la suite si les agresseurs
    sont informés, ou la prédire à contresens s'ils se font absorber ;
  - les GROSSES transactions devraient porter plus d'information que les
    petites, si l'argument institutionnel tient ;
  - l'ABSORPTION — beaucoup d'agression sans mouvement de prix — devrait
    signaler un mur passif, donc un retournement.

Les trois garde-fous de chercher_signal.py s'appliquent : aucun regard sur
le futur, rendements normalisés par l'ATR, contrôle hors échantillon
systématique. Le bruit statistique est rappelé en tête de sortie.

Usage :
  python chercher_flux.py
  python chercher_flux.py --horizons 1,3,6,12,24
"""

import argparse
import csv
import math
import os
import statistics

import chercher_signal as S

FICHIER = os.path.join("data", "flux", "BTCUSDT_300s.csv")


def charger(chemin):
    """Barres de flux, converties au format attendu par les outils communs."""
    rows = []
    with open(chemin, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                x = {k: float(v) for k, v in r.items()}
            except (TypeError, ValueError):
                continue
            x["ts"] = x["ts"] * 1000          # les outils attendent des ms
            rows.append(x)
    rows.sort(key=lambda x: x["ts"])
    # Barres manquantes (arrêt de plateforme) : on ne les invente pas, mais on
    # signale la discontinuité pour ne pas calculer un delta cumulé à travers.
    return rows


def construire_flux(b):
    """
    Indicateurs de flux d'ordres. Tous calculés sur la barre CLOSE.

    Le suffixe _norm ramène au volume de la barre : sans ça on mesurerait
    surtout l'activité du moment, pas le déséquilibre.
    """
    n = len(b)
    a = S.atr(b)
    vol = [x["volume"] for x in b]
    delta = [x["vol_achat"] - x["vol_vente"] for x in b]
    gros_d = [x["gros_achat"] - x["gros_vente"] for x in b]
    gros_v = [x["gros_achat"] + x["gros_vente"] for x in b]
    moyen_d = [x["moyen_achat"] - x["moyen_vente"] for x in b]
    # Le petit flux est le reste : ni gros ni moyen. Souvent le contraire
    # utile — c'est le flux qu'on suppose le moins informé.
    petit_d = [delta[i] - gros_d[i] - moyen_d[i] for i in range(n)]
    rendement = [(b[i]["close"] - b[i]["open"]) for i in range(n)]

    vol20 = S.sma(vol, 20)
    F = {}

    def pose(nom, f):
        F[nom] = [None] * n
        for i in range(n):
            try:
                F[nom][i] = f(i)
            except (TypeError, ZeroDivisionError, IndexError):
                F[nom][i] = None

    # --- agression nette ---
    pose("delta_norm", lambda i: delta[i] / vol[i] if vol[i] else None)
    for k in (3, 6, 12, 48):
        pose(f"delta_cumule_{k}b", lambda i, k=k:
             sum(delta[i-k+1:i+1]) / sum(vol[i-k+1:i+1])
             if i >= k and sum(vol[i-k+1:i+1]) else None)

    # --- qui agresse : gros, moyens, petits ---
    pose("gros_delta_norm", lambda i: gros_d[i] / vol[i] if vol[i] else None)
    pose("moyen_delta_norm", lambda i: moyen_d[i] / vol[i] if vol[i] else None)
    pose("petit_delta_norm", lambda i: petit_d[i] / vol[i] if vol[i] else None)
    pose("part_gros", lambda i: gros_v[i] / vol[i] if vol[i] else None)
    pose("gros_delta_6b", lambda i:
         sum(gros_d[i-5:i+1]) / sum(vol[i-5:i+1])
         if i >= 6 and sum(vol[i-5:i+1]) else None)

    # --- absorption : beaucoup d'agression, peu de mouvement ---
    pose("absorption", lambda i:
         (delta[i] / vol[i]) / (abs(rendement[i]) / a[i] + 0.1)
         if vol[i] and a[i] else None)
    pose("efficacite_delta", lambda i:
         (rendement[i] / a[i]) / (delta[i] / vol[i])
         if vol[i] and a[i] and abs(delta[i] / vol[i]) > 0.02 else None)

    # --- divergence entre le flux et le prix ---
    pose("divergence_flux", lambda i:
         (sum(delta[i-11:i+1]) / sum(vol[i-11:i+1]))
         - ((b[i]["close"] - b[i-11]["close"]) / a[i])
         if i >= 12 and sum(vol[i-11:i+1]) and a[i] else None)

    # --- structure de l'activite ---
    pose("taille_moyenne", lambda i:
         (vol[i] / b[i]["n_trades"]) if b[i]["n_trades"] else None)
    pose("trades_relatif", lambda i:
         b[i]["n_trades"] / (vol20[i] / (vol[i] / b[i]["n_trades"]))
         if vol20[i] and b[i]["n_trades"] and vol[i] else None)
    pose("volume_relatif", lambda i: vol[i] / vol20[i] if vol20[i] else None)
    pose("trade_max_norm", lambda i:
         b[i]["trade_max"] / (vol[i] * b[i]["close"]) if vol[i] else None)
    # Déséquilibre du NOMBRE de trades, pas de leur volume : si beaucoup de
    # petits achètent pendant que le volume vend, c'est du flux retail à contre.
    pose("desequilibre_nombre", lambda i:
         (2 * b[i]["n_achat"] / b[i]["n_trades"] - 1)
         if b[i]["n_trades"] else None)
    pose("nombre_vs_volume", lambda i:
         (2 * b[i]["n_achat"] / b[i]["n_trades"] - 1) - (delta[i] / vol[i])
         if b[i]["n_trades"] and vol[i] else None)

    return F, a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fichier", default=FICHIER)
    ap.add_argument("--horizons", default="1,3,6,12,24")
    args = ap.parse_args()

    if not os.path.exists(args.fichier):
        raise SystemExit(f"{args.fichier} absent — lance d'abord import_flux.py")
    b = charger(args.fichier)
    horizons = [int(x) for x in args.horizons.split(",")]
    jours = (b[-1]["ts"] - b[0]["ts"]) / 86_400_000
    print(f"{len(b):,} barres — {jours:.0f} jours — "
          f"bruit statistique ~{1/math.sqrt(len(b)):.4f}")
    print(f"horizons {horizons} barres "
          f"({[h*5 for h in horizons]} minutes)\n")

    F, a = construire_flux(b)
    print(f"{len(F)} indicateurs de flux\n")
    lignes = S.analyser(F, a, b, horizons)

    entete = f"{'indicateur':<22}"
    for hz in horizons:
        entete += f"{'IC@'+str(hz):>9}"
    entete += f"{'meilleur':>10}{'hors ech.':>11}{'decile':>9}"
    print(entete)
    print("─" * len(entete))

    classement = []
    for e in lignes:
        best = max((hz for hz in horizons if hz in e),
                   key=lambda hz: abs(e[hz]["ic"]), default=None)
        if best is None:
            continue
        ligne = f"{e['nom']:<22}"
        for hz in horizons:
            ligne += f"{e[hz]['ic']:>9.4f}" if hz in e else f"{'—':>9}"
        m = e[best]
        stable = (m["ic"] * m["ic2"]) > 0
        ligne += f"{m['ic']:>+10.4f}{m['ic2']:>+11.4f}{m['dec']:>+9.3f}"
        if abs(m["ic"]) >= 0.05 and stable:
            ligne += "  <<<"
        elif abs(m["ic"]) >= 0.03 and stable:
            ligne += "  <"
        print(ligne)
        classement.append((abs(m["ic"]) if stable else 0.0, e["nom"], m, best))

    print("\n" + "=" * 72)
    classement.sort(reverse=True)
    print("INDICATEURS DE FLUX STABLES HORS ECHANTILLON")
    trouve = False
    for score, nom, m, hz in classement[:8]:
        if score < 0.02:
            continue
        trouve = True
        sens = "SUIT le flux" if m["ic"] > 0 else "CONTRE le flux"
        print(f"  {nom:<22} IC {m['ic']:+.4f} a {hz*5:>3} min  "
              f"(hors ech. {m['ic2']:+.4f}, t={m['t']:+.1f})  {sens}")
    if not trouve:
        print("  aucun. Le flux d'ordres ne predit rien de stable.")

    meilleur = classement[0][0] if classement else 0.0
    print(f"\nMeilleur IC stable : {meilleur:.4f}")
    if meilleur < 0.03:
        print("Sous le bruit exploitable. Le flux ne contient pas d'avantage")
        print("directionnel a cet horizon.")
    elif meilleur < 0.05:
        print("Faible mais reel. A confronter aux frais avant d'y croire.")
    else:
        print("Assez fort pour construire dessus — apres verification par regime.")


if __name__ == "__main__":
    main()
