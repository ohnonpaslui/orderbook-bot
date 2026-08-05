"""
collector.py — Phase 1 : enregistrement du carnet d'ordres.

Aucune bourse ne fournit d'historique de carnet : il faut le construire soi-même.
Ce collecteur interroge le carnet toutes les SNAPSHOT_INTERVAL secondes, en tire
les features (features.compute) et les écrit dans data/AAAA-MM-JJ/HH.csv.gz.

On ne stocke PAS le carnet brut : ~100 niveaux x 2 côtés toutes les 2 s
représenteraient plusieurs Go par semaine. Les features font ~120 octets par
ligne, soit ~2 Mo/jour compressés — un repo git tient ça sans broncher.

Lancement :
  local          : python collector.py
  GitHub Actions : GIT_PUSH=1 MAX_RUNTIME=17700 python collector.py
"""

import gzip
import os
import subprocess
import time
from datetime import datetime, timezone

import ccxt

import config as C
import features

MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME", "0"))    # secondes ; 0 = infini
GIT_PUSH    = os.environ.get("GIT_PUSH") == "1"


def path_for(ts):
    """Un fichier par heure UTC : découpage naturel et fichiers de taille stable."""
    d = datetime.fromtimestamp(ts, timezone.utc)
    return os.path.join(C.DATA_DIR, d.strftime("%Y-%m-%d"), d.strftime("%H") + ".csv.gz")


def flush(buffer):
    """Écrit le buffer, en répartissant les lignes dans leur fichier horaire."""
    by_file = {}
    for row in buffer:
        by_file.setdefault(path_for(row["ts"]), []).append(row)

    for path, rows in by_file.items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        header_needed = not os.path.exists(path)
        # mode "at" : gzip ajoute un nouveau membre, les lecteurs les
        # concatènent de façon transparente. Pas besoin de tout relire.
        with gzip.open(path, "at", newline="", encoding="utf-8") as f:
            if header_needed:
                f.write(",".join(features.COLUMNS) + "\n")
            for row in rows:
                f.write(",".join(str(row[c]) for c in features.COLUMNS) + "\n")
    buffer.clear()


def git_commit(message):
    try:
        subprocess.run(["git", "add", C.DATA_DIR], check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return                                    # rien de neuf
        subprocess.run(["git", "commit", "-m", message], check=True)
        subprocess.run(["git", "pull", "--rebase"], check=False)
        subprocess.run(["git", "push"], check=True)
    except Exception as e:
        print(f"[git] echec commit/push : {e}", flush=True)


def main():
    start = time.time()
    ex = getattr(ccxt, C.EXCHANGE)({"enableRateLimit": True})

    buffer, n_ok, n_err = [], 0, 0
    last_commit = time.time()
    print(f"Collecteur demarre — {C.SYMBOL} @ {C.EXCHANGE} — "
          f"1 snapshot / {C.SNAPSHOT_INTERVAL}s", flush=True)

    while True:
        cycle_start = time.time()
        if MAX_RUNTIME and cycle_start - start > MAX_RUNTIME:
            print("Duree max atteinte, arret propre.", flush=True)
            break

        try:
            book = ex.fetch_order_book(C.SYMBOL, limit=C.BOOK_DEPTH)
            row = features.compute(book, cycle_start)
            if row:
                buffer.append(row)
                n_ok += 1
            else:
                n_err += 1                            # carnet vide ou croisé
        except Exception as e:
            n_err += 1
            if n_err % 10 == 1:                       # ne pas noyer les logs
                print(f"[data] {type(e).__name__}: {e}", flush=True)
            time.sleep(5)

        if len(buffer) >= C.FLUSH_EVERY:
            flush(buffer)

        if GIT_PUSH and time.time() - last_commit >= C.COMMIT_EVERY:
            flush(buffer)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            git_commit(f"Collecte carnet {C.SYMBOL} — {now} UTC ({n_ok} snapshots)")
            last_commit = time.time()
            print(f"{now} UTC  {n_ok} snapshots OK, {n_err} erreurs", flush=True)

        # Cadence stable même si l'appel API a été lent.
        time.sleep(max(0.0, C.SNAPSHOT_INTERVAL - (time.time() - cycle_start)))

    flush(buffer)
    if GIT_PUSH:
        git_commit(f"Collecte carnet — fin de session ({n_ok} snapshots)")
    print(f"Termine : {n_ok} snapshots collectes, {n_err} erreurs.", flush=True)


if __name__ == "__main__":
    main()
