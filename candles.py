"""
candles.py — Récupération et cache des bougies OHLCV.

Contrairement au carnet, l'historique des bougies est public et gratuit : pas
besoin de le collecter, on le télécharge à la demande et on le met en cache.
C'est ce qui permet de valider la couche analyse technique tout de suite,
sans attendre les deux semaines de collecte du carnet.

Cache : data/<plateforme>/candles_<tf>.csv, complété de façon incrémentale.
"""

import csv
import json
import os
import time
import urllib.request

import ccxt

import config as C

CACHE_TF = "5m"
TF_MS    = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
            "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
COLUMNS  = ["ts", "open", "high", "low", "close", "volume"]


def cache_path(venue, timeframe=CACHE_TF):
    return os.path.join(C.DATA_DIR, venue, f"candles_{timeframe}.csv")


# ----------------------------- Source Yahoo -----------------------------------
# Les bougies de futures ne sont pas accessibles via ccxt. Yahoo en publie
# gratuitement, ce qui permet de tester une stratégie sur NQ ou ES avant de
# payer quoi que ce soit — la licence CME pour le carnet en API coûte 290 $/mois,
# et rien ne justifie cette dépense tant qu'aucun avantage n'est démontré.
#
# Profondeurs disponibles (mesurées) : 1h -> 873 jours, 15m et 5m -> 60 jours,
# 1m -> 7 jours, 1d -> 10 ans.
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
YAHOO_MAX = {"1m": "7d", "5m": "60d", "15m": "60d", "30m": "60d",
             "1h": "730d", "1d": "10y"}


def fetch_yahoo(symbole, timeframe="1h", periode=None, verbose=True):
    """
    Bougies OHLCV depuis Yahoo, au même format que `fetch`.

    `symbole` suit la nomenclature Yahoo : NQ=F (Nasdaq futures), ES=F (S&P),
    MNQ=F (micro), QQQ (ETF), ^NDX (indice).
    """
    import urllib.parse
    periode = periode or YAHOO_MAX.get(timeframe, "730d")
    url = (f"{YAHOO.format(sym=urllib.parse.quote(symbole))}"
           f"?interval={timeframe}&range={periode}")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)

    res = (d.get("chart") or {}).get("result")
    if not res:
        raise RuntimeError(f"Yahoo n'a rien renvoyé pour {symbole} "
                           f"({(d.get('chart') or {}).get('error')})")
    r0 = res[0]
    ts = r0.get("timestamp") or []
    q = r0["indicators"]["quote"][0]

    rows = []
    for i, t in enumerate(ts):
        o, h, l, c = (q["open"][i], q["high"][i], q["low"][i], q["close"][i])
        if None in (o, h, l, c):
            continue                      # bougie creuse (jour ferié, halte)
        rows.append({"ts": float(t) * 1000, "open": o, "high": h, "low": l,
                     "close": c, "volume": (q.get("volume") or [0])[i] or 0.0})
    rows.sort(key=lambda x: x["ts"])
    if verbose:
        print(f"[yahoo] {len(rows):,} bougies {timeframe} pour {symbole}",
              flush=True)
    return rows


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

    # Les tentatives sont BORNEES. Une boucle infinie ici a deja bloque une
    # session entiere en production (run #6 du 2026-08-08) : les threads de
    # collecte continuaient a remplir la memoire pendant que le thread
    # principal reessayait sans fin, donc plus aucun commit pendant 5 heures.
    # Mieux vaut remonter l'erreur : l'appelant garde le dernier setup connu
    # et reessaiera au prochain cycle.
    MAX_ESSAIS = 4
    essais = 0

    while since < now_ms:
        try:
            lot = ex.fetch_ohlcv(v["symbol"], timeframe, since=since, limit=720)
            essais = 0
        except Exception as e:
            essais += 1
            print(f"[candles] {type(e).__name__}: {str(e)[:120]} "
                  f"(essai {essais}/{MAX_ESSAIS})", flush=True)
            if essais >= MAX_ESSAIS:
                if rows:
                    print("[candles] abandon — on garde l'historique deja en cache",
                          flush=True)
                    break
                raise
            time.sleep(3 * essais)
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
