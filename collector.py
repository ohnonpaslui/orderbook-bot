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
from diagnostics import Observateur

MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME", "0"))    # secondes ; 0 = infini
GIT_PUSH    = os.environ.get("GIT_PUSH") == "1"

# Une plateforme qui échoue en boucle (géo-blocage, maintenance) est mise de
# côté ce laps de temps au lieu de saturer les logs et de ralentir les autres.
BACKOFF_AFTER  = 10             # échecs consécutifs
BACKOFF_SEC    = 300

# Le flux est interroge a CHAQUE cycle, en meme temps que le carnet.
#
# Une premiere version l'interrogeait toutes les 6 s pour menager le bridage
# ccxt. Erreur : la fenetre d'un snapshot couvre les 2 dernieres secondes, or
# ces transactions-la n'avaient pas encore ete recuperees. Resultat mesure :
# 4 snapshots sur 37 portaient des transactions au lieu de la quasi-totalite.
#
# Or c'est exactement la synchronisation entre carnet et flux qui permet de
# dire si une vente agressive fait plier le carnet ou se fait absorber. La
# desynchroniser vide la collecte de son objet. On paie donc le prix : deux
# appels brides par cycle portent la cadence a ~2.5 s au lieu de 2 s.
TRADES_INTERVAL = 0.0


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


def git_commit(message, paths=(C.DATA_DIR, C.STATE_DIR)):
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
        # Flux des transactions. On l'interroge moins souvent que le carnet :
        # la fenetre renvoyee couvre plusieurs minutes, donc rien n'est perdu,
        # et le bridage ccxt ne permettrait pas deux appels par cycle sans
        # doubler l'intervalle. Chaque transaction porte son horodatage : on
        # les rattache ensuite au snapshot dont elles occupent la fenetre.
        self.trades = {}            # id -> transaction, pour dedoublonner
        self.dernier_trades = 0.0
        self.trades_ok = False      # le flux repond-il ?

    def take_buffer(self):
        """Retire et retourne les lignes accumulées, sans bloquer la collecte."""
        with self.lock:
            rows, self.buffer = self.buffer, []
        return rows

    def _rafraichir_trades(self, maintenant):
        """Interroge le flux si l'echeance est passee, et dedoublonne."""
        if maintenant - self.dernier_trades < TRADES_INTERVAL:
            return
        self.dernier_trades = maintenant
        try:
            for t in self.client.fetch_trades(self.cfg["symbol"], limit=500):
                cle = t.get("id") or f"{t.get('timestamp')}_{t.get('amount')}"
                self.trades[cle] = t
            self.trades_ok = True
        except Exception as e:
            if self.trades_ok is not False:
                print(f"[{self.venue}] flux transactions indisponible : "
                      f"{type(e).__name__}", flush=True)
            self.trades_ok = False
        # On ne garde que le passe recent : la memoire ne doit pas croitre.
        limite = (maintenant - 600) * 1000
        self.trades = {k: v for k, v in self.trades.items()
                       if (v.get("timestamp") or 0) >= limite}

    def _trades_fenetre(self, fin):
        """Transactions tombant dans la fenetre du snapshot courant."""
        debut = (fin - self.cfg["interval"]) * 1000
        return [t for t in self.trades.values()
                if debut <= (t.get("timestamp") or 0) < fin * 1000]

    def run(self):
        while not self.stop.is_set():
            t0 = time.time()
            try:
                # Kraken Futures ignore `limit` et renvoie tout le carnet :
                # on omet le paramètre plutôt que d'envoyer une valeur fictive.
                depth = self.cfg.get("depth")
                book = (self.client.fetch_order_book(self.cfg["symbol"], limit=depth)
                        if depth else
                        self.client.fetch_order_book(self.cfg["symbol"]))
                self._rafraichir_trades(t0)
                # La bande de murs est passée explicitement : `features` ne doit
                # dépendre d'aucun global, plusieurs threads l'appellent.
                row = features.compute(book, t0, self.cfg["span_bps"],
                                       trades=self._trades_fenetre(t0))
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

    # La stratégie tourne à blanc sur la plateforme cible pendant toute la
    # collecte : sans ça on enregistrerait deux semaines de carnet sans savoir
    # si un seul setup se serait arme.
    observateur = None
    if C.LIVE_VENUE in workers:
        observateur = Observateur(C.LIVE_VENUE)
        observateur.rafraichir()
        print(f"[diag] observation active sur {C.LIVE_VENUE} "
              f"({C.TIMEFRAME}) — aucune position ne sera ouverte", flush=True)

    def drain():
        for v, w in workers.items():
            rows = w.take_buffer()
            if observateur and v == C.LIVE_VENUE:
                observateur.consommer(rows)
            flush(v, rows)
        if observateur:
            observateur.ecrire()

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
                if observateur:
                    print("        " + observateur.resume(), flush=True)
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
