"""
Verifie que la recuperation des bougies ne peut pas boucler indefiniment.

Ce test existe a cause d'une panne reelle : le 2026-08-08, une boucle de
reessai sans limite a fige une session entiere pendant 5 heures. Les threads
de collecte remplissaient la memoire, le thread principal reessayait sans fin,
et plus aucune donnee n'etait commitee.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))

import candles as K
import config as C


class ApiCassee:
    """Client ccxt factice qui echoue systematiquement."""

    def __init__(self):
        self.appels = 0

    def milliseconds(self):
        return int(time.time() * 1000)

    def fetch_ohlcv(self, *a, **k):
        self.appels += 1
        raise ConnectionError("panne simulee")


class ApiIntermittente(ApiCassee):
    """Echoue deux fois puis repond normalement."""

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.appels += 1
        if self.appels <= 2:
            raise ConnectionError("panne temporaire")
        pas = K.TF_MS[timeframe]
        base = since or (self.milliseconds() - 100 * pas)
        return [[base + i * pas, 100.0, 101.0, 99.0, 100.5, 1.0]
                for i in range(limit or 10)]


faux = {}
K_originale = K.ccxt


class FabriqueFactice:
    def __init__(self, client):
        self.client = client

    def __getattr__(self, nom):
        return lambda *a, **k: self.client


print("=== 1. API totalement en panne -> l'erreur remonte, pas de blocage ===")
casse = ApiCassee()
K.ccxt = FabriqueFactice(casse)
depart = time.time()
try:
    K.fetch(C.LIVE_VENUE, "15m", days=1, use_cache=False, verbose=False)
    leve = False
except Exception as e:
    leve = True
    print(f"  exception remontee : {type(e).__name__}")
duree = time.time() - depart
print(f"  {casse.appels} tentatives en {duree:.1f}s")
assert leve, "l'erreur aurait du remonter au lieu de boucler"
assert casse.appels <= 6, f"trop de tentatives : {casse.appels}"
assert duree < 60, f"trop long : {duree:.0f}s — signe d'une boucle non bornee"

print("\n=== 2. panne temporaire -> reprise automatique ===")
inter = ApiIntermittente()
K.ccxt = FabriqueFactice(inter)
rows = K.fetch(C.LIVE_VENUE, "15m", days=1, use_cache=False, verbose=False)
print(f"  {inter.appels} tentatives, {len(rows)} bougies recuperees")
assert rows, "la reprise apres panne temporaire a echoue"
assert inter.appels >= 3, "le reessai n'a pas eu lieu"

K.ccxt = K_originale
print("\nTOUS LES TESTS DE REESSAI PASSENT")
