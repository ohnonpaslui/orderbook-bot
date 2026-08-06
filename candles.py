"""
candles.py — Récupération et cache des bougies OHLCV.

Contrairement au carnet, l'historique des bougies est public et gratuit : pas
besoin de le collecter, on le télécharge à la demande et on le met en cache.
C'est ce qui permet de valider la couche analyse technique tout de suite,
sans attendre les deux semaines de collecte du carnet.

Cache : data/<plateforme>/candles_<tf>.csv, complété de façon incrémentale.
"""

import csv
import os
import time

import ccxt

import config as C

CACHE_TF = "5m"
TF_MS    = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
            "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
COLUMNS  = ["ts", "open", "high", "low", "close", "volume"]


def cache_path(venue, timeframe=CACHE_TF):
    return os.path.join(C.DATA_DIR, venue, f"candles_{timeframe}.csv")


def load_cache(venue, timeframe=CACHE_TF):
    path = cache_path(venue, timeframe)
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for rec in csv.DictReader(f):
            try:
                rows.append({k: float(rec[k]) for k in COLUMNS})
            except (KeyError, TypeError, ValueError):
                continue                       # ligne tronquée
    return rows


def save_cache(venue, rows, timeframe=CACHE_TF):
    path = cache_path(venue, timeframe)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def fetch(venue, timeframe=CACHE_TF, days=90, use_cache=True, verbose=True):
    """
    Retourne les bougies clôturées, les plus anciennes d'abord.
    Complète le cache au lieu de tout retélécharger.
    """
    v      = C.VENUES[venue]
    tf_ms  = TF_MS[timeframe]
    ex     = getattr(ccxt, v["exchange"])({"enableRateLimit": True, "timeout": 25000})
    now_ms = ex.milliseconds()
    debut  = now_ms - days * 86_400_000

    rows  = load_cache(venue, timeframe) if use_cache else []
    # Le cache ne s'étend que vers l'avant. Si on demande un historique plus
    # ancien que ce qu'il contient, il faut repartir du début — sinon on
    # travaillerait silencieusement sur une fenêtre plus courte que demandée.
    if rows and int(rows[0]["ts"]) > debut + tf_ms:
        rows = []

    connu = {int(r["ts"]) for r in rows}
    since = max(int(rows[-1]["ts"]) + tf_ms, debut) if rows else debut

    while since < now_ms:
        try:
            lot = ex.fetch_ohlcv(v["symbol"], timeframe, since=since, limit=720)
        except Exception as e:
            print(f"[candles] {type(e).__name__}: {str(e)[:120]} — nouvel essai", flush=True)
            time.sleep(5)
            continue
        if not lot:
            break
        for ts, o, h, l, c, vol in lot:
            if ts not in connu and ts + tf_ms <= now_ms:   # bougie clôturée
                connu.add(ts)
                rows.append({"ts": float(ts), "open": o, "high": h,
                             "low": l, "close": c, "volume": vol})
        nouveau = lot[-1][0] + tf_ms
        if nouveau <= since:
            break                                # l'API ne progresse plus
        since = nouveau
        if verbose:
            print(f"\r[candles] {len(rows):,} bougies...", end="", flush=True)

    rows.sort(key=lambda r: r["ts"])
    rows = [r for r in rows if r["ts"] >= debut]
    if use_cache:
        save_cache(venue, rows, timeframe)
    if verbose:
        print(f"\r[candles] {len(rows):,} bougies {timeframe} en cache.        ",
              flush=True)
    return rows
