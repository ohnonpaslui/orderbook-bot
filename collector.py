"""
collector.py — Phase 1 : enregistrement du carnet d'ordres, multi-plateformes.

Aucune bourse ne publie d'historique de carnet : il faut le construire soi-même.
Ce collecteur interroge le carnet de chaque plateforme de C.VENUES à sa propre
cadence, en tire les features et les écrit dans
    data/<plateforme>/AAAA-MM-JJ/HH.csv.gz

On ne stocke PAS le carnet brut : 5000 niveaux x 2 côtés toutes les 5 s
représenteraient plusieurs Go par jour. Les features tiennent en ~120 octets
par ligne — un repo git absorbe ça.

Un thread par plateforme, un seul processus. Le thread est nécessaire, pas
décoratif : mesuré sur les vraies API, un appel Binance à 5000 niveaux bloque
2.5 s et un appel Kraken 1.2 s (bridage ccxt, la latence réseau réelle n'est
que de 24 ms). En séquentiel, Binance affamait Kraken et faisait tomber sa
cadence de 2 s à 7 s. En parallèle, chacune tient la sienne.

Un seul processus reste un seul écrivain sur data/ : aucun conflit git.

Lancement :
  local          : python collector.py
  GitHub Actions : GIT_PUSH=1 MAX_RUNTIME=17700 python collector.py
"""

import gzip
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

import ccxt

import config as C
import features

MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME", "0"))    # secondes ; 0 = infini
GIT_PUSH    = os.environ.get("GIT_PUSH") == "1"

# Une plateforme qui échoue en boucle (géo-blocage, maintenance) est mise de
# côté ce laps de temps au lieu de saturer les logs et de ralentir les autres.
BACKOFF_AFTER  = 10             # échecs consécutifs
BACKOFF_SEC    = 300


def path_for(venue, ts):
    """
    Un fichier par tranche de 10 minutes UTC.

    Le découpage n'est pas cosmétique. Avec des fichiers horaires réécrits à
    chaque commit, git stocke une copie complète du .gz à chaque fois (les
    fichiers compressés ne se « deltifient » pas) : mesuré, ~3.5x le volume
    utile dans l'historique. Une tranche alignée sur COMMIT_EVERY est écrite
    puis figée, donc stockée une seule fois.
    """
    d = datetime.fromtimestamp(ts, timezone.utc)
    tranche = f"{d.hour:02d}{d.minute // 10}0"
    return os.path.join(C.DATA_DIR, venue,
                        d.strftime("%Y-%m-%d"), tranche + ".csv.gz")


def flush(venue, buffer):
    """Écrit le buffer d'une plateforme, réparti dans ses fichiers horaires."""
    if not buffer:
        return
    by_file = {}
    for row in buffer:
        by_file.setdefault(path_for(venue, row["ts"]), []).append(row)

    for path, rows in by_file.items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        header_needed = not os.path.exists(path)
        # mode "at" : gzip ajoute un nouveau membre, que les lecteurs
        # concatènent de façon transparente. Pas besoin de tout relire.
        with gzip.open(path, "at", newline="", encoding="utf-8") as f:
            if header_needed:
                f.write(",".join(features.COLUMNS) + "\n")
            for row in rows:
                f.write(",".join(str(row[c]) for c in features.COLUMNS) + "\n")
    buffer.clear()


def git_commit(message, paths=(C.DATA_DIR,)):
    try:
        present = [p for p in paths if os.path.isdir(p)]
        if not present:
            return
        subprocess.run(["git", "add", *present], check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return                                    # rien de neuf
        subprocess.run(["git", "commit", "-m", message], check=True)
        subprocess.run(["git", "pull", "--rebase"], check=False)
        subprocess.run(["git", "push"], check=True)
    except Exception as e:
        print(f"[git] echec commit/push : {e}", flush=True)


class VenueCollector(threading.Thread):
    """Boucle de collecte d'une plateforme, indépendante des autres."""

    def __init__(self, venue, stop_event):
        super().__init__(name=venue, daemon=True)
        self.venue  = venue
        self.cfg    = C.VENUES[venue]
        self.stop   = stop_event
        self.client = getattr(ccxt, self.cfg["exchange"])({
            "enableRateLimit": True, "timeout": 25000,
        })
        self.buffer = []
        self.lock   = threading.Lock()
        self.n_ok = self.n_err = self.streak = 0

    def take_buffer(self):
        """Retire et retourne les lignes accumulées, sans bloquer la collecte."""
        with self.lock:
            rows, self.buffer = self.buffer, []
        return rows

    def run(self):
        while not self.stop.is_set():
            t0 = time.time()
            try:
                book = self.client.fetch_order_book(
                    self.cfg["symbol"], limit=self.cfg["depth"])
                # La bande de murs est passée explicitement : `features` ne doit
                # dépendre d'aucun global, plusieurs threads l'appellent.
                row = features.compute(book, t0, self.cfg["span_bps"])
                if row:
                    with self.lock:
                        self.buffer.append(row)
                    self.n_ok += 1
                    self.streak = 0
                else:
                    self.n_err += 1              # carnet vide ou croisé
            except Exception as e:
                self.n_err += 1
                self.streak += 1
                if self.streak in (1, BACKOFF_AFTER):
                    print(f"[{self.venue}] {type(e).__name__}: {str(e)[:150]}",
                          flush=True)
                if self.streak >= BACKOFF_AFTER:
                    # Géo-blocage (451 depuis les runners US) ou panne : cette
                    # plateforme se met en pause sans gêner les autres.
                    print(f"[{self.venue}] {self.streak} echecs consecutifs — "
                          f"pause de {BACKOFF_SEC}s", flush=True)
                    self.stop.wait(BACKOFF_SEC)
                    continue

            # `wait` plutôt que `sleep` : l'arrêt est immédiat en fin de session.
            self.stop.wait(max(0.0, self.cfg["interval"] - (time.time() - t0)))


def main():
    start = time.time()
    stop  = threading.Event()

    actives = [v for v, cfg in C.VENUES.items() if cfg.get("collect", True)]
    if not actives:
        raise SystemExit("Aucune plateforme active : mettre `collect=True` "
                         "sur au moins une entrée de C.VENUES.")

    workers = {v: VenueCollector(v, stop) for v in actives}
    for w in workers.values():
        w.start()

    resume = ", ".join(f"{v} ({C.VENUES[v]['symbol']}, {C.VENUES[v]['interval']}s, "
                       f"{C.VENUES[v]['depth']} niv.)" for v in actives)
    print(f"Collecteur demarre — {resume}", flush=True)

    def drain():
        for v, w in workers.items():
            flush(v, w.take_buffer())

    last_commit = time.time()
    try:
        while True:
            if MAX_RUNTIME and time.time() - start > MAX_RUNTIME:
                print("Duree max atteinte, arret propre.", flush=True)
                break
            stop.wait(10.0)

            if time.time() - last_commit >= C.COMMIT_EVERY:
                drain()
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
                if GIT_PUSH:
                    detail = " ".join(f"{v}:{w.n_ok}" for v, w in workers.items())
                    git_commit(f"Collecte carnet — {stamp} UTC ({detail})")
                last_commit = time.time()
                ecoule = time.time() - start
                print(f"{stamp} UTC  " + "  ".join(
                    f"{v}: {w.n_ok} OK / {w.n_err} err "
                    f"({ecoule / w.n_ok:.1f}s par snapshot)" if w.n_ok else
                    f"{v}: aucun snapshot" for v, w in workers.items()), flush=True)
    finally:
        stop.set()
        for w in workers.values():
            w.join(timeout=30)
        drain()

    if GIT_PUSH:
        git_commit("Collecte carnet — fin de session")
    ecoule = time.time() - start
    for v, w in workers.items():
        cadence = f"{ecoule / w.n_ok:.2f}s par snapshot" if w.n_ok else "—"
        print(f"Termine — {v}: {w.n_ok} snapshots, {w.n_err} erreurs, {cadence}",
              flush=True)


if __name__ == "__main__":
    main()
