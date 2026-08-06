"""
status.py — Etat de la collecte, en une commande.

Pendant les 1 a 2 semaines de collecte, la question est toujours la meme :
« est-ce que ca tourne vraiment, et est-ce que j'en ai assez pour la phase 2 ? »
Lire les logs GitHub Actions a la main ne repond ni a l'une ni a l'autre —
une session peut tourner en affichant des OK tout en laissant des trous.

Usage :
  git pull && python status.py
"""

import csv
import glob
import gzip
import os
from collections import defaultdict
from datetime import datetime, timezone

import config as C

# Un trou de plus de 2 minutes signale une session interrompue ou une panne
# de plateforme, pas un simple retard d'API.
TROU_SEC = 120.0
# Reperes pour la phase 2 : en dessous, la calibration n'a pas de sens.
CIBLE_JOURS = 10


def horodatages(venue):
    """Timestamps de tous les snapshots d'une plateforme, tries."""
    ts = []
    motif = os.path.join(C.DATA_DIR, venue, "*", "*.csv.gz")
    for chemin in sorted(glob.glob(motif)):
        try:
            with gzip.open(chemin, "rt", newline="", encoding="utf-8") as f:
                for rec in csv.DictReader(f):
                    v = rec.get("ts")
                    if v and v != "ts":
                        try:
                            ts.append(float(v))
                        except ValueError:
                            continue
        except (OSError, EOFError) as e:
            print(f"  [!] fichier illisible : {chemin} ({type(e).__name__})")
    ts.sort()
    return ts


def taille(venue):
    total = 0
    for racine, _, fichiers in os.walk(os.path.join(C.DATA_DIR, venue)):
        for f in fichiers:
            if f.endswith(".csv.gz"):
                total += os.path.getsize(os.path.join(racine, f))
    return total


def octets(n):
    for unite, seuil in (("Go", 1 << 30), ("Mo", 1 << 20), ("Ko", 1 << 10)):
        if n >= seuil:
            return f"{n / seuil:.1f} {unite}"
    return f"{n} o"


def humain(sec):
    if sec < 90:
        return f"{sec:.0f}s"
    if sec < 5400:
        return f"{sec / 60:.0f}min"
    if sec < 172800:
        return f"{sec / 3600:.1f}h"
    return f"{sec / 86400:.1f}j"


def main():
    actives = [v for v, cfg in C.VENUES.items() if cfg.get("collect", True)]
    print(f"Plateformes collectees : {', '.join(actives)}")
    print(f"Cible avant phase 2 : {CIBLE_JOURS} jours de donnees\n")

    aucune = True
    for venue in C.VENUES:
        ts = horodatages(venue)
        if not ts:
            if venue in actives:
                print(f"■ {venue:<16} AUCUNE DONNEE\n")
            continue
        aucune = False

        debut = datetime.fromtimestamp(ts[0], timezone.utc)
        fin   = datetime.fromtimestamp(ts[-1], timezone.utc)
        etendue = ts[-1] - ts[0]
        attendu = C.VENUES[venue]["interval"]

        # Trous et couverture reelle : c'est la couverture qui compte, pas
        # l'etendue calendaire — 14 jours a moitie couverts font 7 jours.
        trous = [(a, b - a) for a, b in zip(ts, ts[1:]) if b - a > TROU_SEC]
        perdu = sum(d for _, d in trous)
        couvert = etendue - perdu

        age = (datetime.now(timezone.utc) - fin).total_seconds()
        frais = "a jour" if age < 900 else f"DERNIER POINT IL Y A {humain(age)}"

        print(f"■ {venue}")
        print(f"    periode      {debut:%Y-%m-%d %H:%M} -> {fin:%Y-%m-%d %H:%M} UTC "
              f"({frais})")
        print(f"    snapshots    {len(ts):,}  |  cadence reelle "
              f"{couvert / len(ts):.2f}s (cible {attendu}s)")
        print(f"    couverture   {humain(couvert)} sur {humain(etendue)} "
              f"({couvert / etendue * 100:.0f} %)")
        octets_venue = taille(venue)
        par_jour = octets_venue / max(couvert, 1) * 86400
        print(f"    volume       {octets(octets_venue)}  "
              f"({octets_venue / len(ts):.0f} o/snapshot, "
              f"~{octets(par_jour)}/jour)")

        if trous:
            pires = sorted(trous, key=lambda x: -x[1])[:3]
            print(f"    trous        {len(trous)} de plus de {TROU_SEC:.0f}s "
                  f"(total {humain(perdu)})")
            for debut_trou, duree in pires:
                q = datetime.fromtimestamp(debut_trou, timezone.utc)
                print(f"                 {q:%m-%d %H:%M} — {humain(duree)}")
        else:
            print("    trous        aucun")

        jours = couvert / 86400
        reste = CIBLE_JOURS - jours
        if reste > 0:
            print(f"    phase 2      {jours:.1f}j collectes, encore "
                  f"~{reste:.1f}j (cadence actuelle)")
        else:
            print(f"    phase 2      PRET ({jours:.1f}j) — "
                  f"lance `python backtest.py --compare`")
        print()

    if aucune:
        print("Rien n'a encore ete collecte. Verifie que le workflow "
              "« Collecte carnet » tourne (onglet Actions), puis `git pull`.")


if __name__ == "__main__":
    main()
