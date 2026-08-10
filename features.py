"""
features.py — Transforme un carnet d'ordres brut en une ligne de features.

C'est le seul endroit où le carnet brut est lu. Le collecteur, le backtest et
le bot live passent tous par `compute()` : impossible que le backtest et le
live divergent sur la définition d'un indicateur.

Le schéma retourné par `compute()` définit les colonnes des CSV de données.
Le modifier invalide les données déjà collectées → voir COLUMNS en bas.
"""

from statistics import median

import config as C


def _depth_notional(levels, mid, band_bps, side):
    """Notionnel cumulé (en $) des niveaux situés à moins de `band_bps` du mid."""
    limit = mid * (1 - band_bps / 10_000) if side == "bid" else mid * (1 + band_bps / 10_000)
    total = 0.0
    # Les niveaux ccxt peuvent porter un 3e champ (timestamp chez Kraken) :
    # on indexe au lieu de dépaqueter.
    for lvl in levels:
        price, amount = lvl[0], lvl[1]
        if (side == "bid" and price < limit) or (side == "ask" and price > limit):
            break                      # carnet trié : on peut sortir tôt
        total += price * amount
    return total


def _find_wall(levels, mid, side, band_bps):
    """
    Cherche le plus gros ordre passif anormal dans la bande `band_bps`.

    "Anormal" = son notionnel dépasse WALL_MIN_MULT fois la médiane des niveaux
    de la bande, et un plancher absolu en $. La médiane (et non la moyenne)
    évite qu'un mur unique se masque lui-même en gonflant la référence.

    Retourne (prix, notionnel, distance_bps) ou (0.0, 0.0, 0.0) si aucun mur.
    """
    limit = (mid * (1 - band_bps / 10_000) if side == "bid"
             else mid * (1 + band_bps / 10_000))

    band = []
    for lvl in levels:
        price, amount = lvl[0], lvl[1]
        if (side == "bid" and price < limit) or (side == "ask" and price > limit):
            break
        band.append((price, price * amount))

    if len(band) < 5:                  # bande trop maigre pour parler de médiane
        return 0.0, 0.0, 0.0

    ref = median(n for _, n in band)
    if ref <= 0:
        return 0.0, 0.0, 0.0

    price, notional = max(band, key=lambda x: x[1])
    if notional < C.WALL_MIN_MULT * ref or notional < C.WALL_MIN_NOTIONAL:
        return 0.0, 0.0, 0.0

    dist_bps = abs(mid - price) / mid * 10_000
    return price, notional, dist_bps


def agreger_trades(trades, gros_notionnel=50_000.0):
    """
    Résume une salve de transactions : qui a frappé, et pour combien.

    Sur un flux public, `side` désigne l'AGRESSEUR — celui qui est venu
    prendre le prix affiché. C'est la seule information qui dise qui gagne le
    bras de fer, et elle est absente du carnet : le carnet montre les
    intentions, les transactions montrent les décisions.

    Les grosses transactions sont comptées à part : un ordre de 200 000 $ ne
    porte pas la même information qu'un de 500.
    """
    r = {"vol_achat": 0.0, "vol_vente": 0.0, "n_trades": 0,
         "gros_achat": 0.0, "gros_vente": 0.0}
    for t in trades or ():
        qte = t.get("amount") or 0.0
        prix = t.get("price") or 0.0
        if qte <= 0 or prix <= 0:
            continue
        r["n_trades"] += 1
        gros = (prix * qte) >= gros_notionnel
        if t.get("side") == "sell":
            r["vol_vente"] += qte
            if gros:
                r["gros_vente"] += qte
        else:
            r["vol_achat"] += qte
            if gros:
                r["gros_achat"] += qte
    return r


def compute(book, ts, wall_band_bps=None, trades=None):
    """
    book          : dict ccxt {"bids": [[px, amt], ...], "asks": [...]}
                    bids triés décroissant, asks triés croissant.
    ts            : timestamp epoch en secondes (float).
    wall_band_bps : étendue de recherche des murs. Passé explicitement par le
                    collecteur, qui interroge plusieurs plateformes en
                    parallèle : lire C.WALL_BAND_BPS ici rendrait la fonction
                    dépendante d'un global que les threads s'écraseraient.

    Retourne un dict plat, ou None si le carnet est inexploitable.
    """
    if wall_band_bps is None:
        wall_band_bps = C.WALL_BAND_BPS
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if len(bids) < 5 or len(asks) < 5:
        return None

    best_bid, bid_sz = bids[0][0], bids[0][1]
    best_ask, ask_sz = asks[0][0], asks[0][1]
    if best_bid <= 0 or best_ask <= best_bid:
        return None                    # carnet croisé ou corrompu

    mid = (best_bid + best_ask) / 2

    # Microprice : mid pondéré par le déséquilibre du meilleur niveau. Penche
    # vers le côté le plus mince, celui qui cédera en premier.
    tot_sz = bid_sz + ask_sz
    microprice = ((best_bid * ask_sz + best_ask * bid_sz) / tot_sz) if tot_sz > 0 else mid

    row = {
        "ts":         round(ts, 2),
        "best_bid":   best_bid,
        "best_ask":   best_ask,
        "mid":        round(mid, 2),
        "microprice": round(microprice, 4),
        "spread_bps": round((best_ask - best_bid) / mid * 10_000, 3),
    }

    for band in C.DEPTH_BANDS_BPS:
        b = _depth_notional(bids, mid, band, "bid")
        a = _depth_notional(asks, mid, band, "ask")
        row[f"bid_{band}"] = round(b, 1)
        row[f"ask_{band}"] = round(a, 1)
        # OBI dans [-1, +1] : +1 = que des acheteurs, -1 = que des vendeurs
        row[f"obi_{band}"] = round((b - a) / (b + a), 4) if (b + a) > 0 else 0.0

    # Flux des transactions survenues depuis le snapshot précédent. Écrit à
    # zéro quand il n'est pas collecté : les fichiers restent lisibles, et le
    # marqueur de provenance dit ce qui est réel.
    r_tr = agreger_trades(trades)
    row.update(r_tr)

    for side, levels in (("bid", bids), ("ask", asks)):
        px, notional, dist = _find_wall(levels, mid, side, wall_band_bps)
        row[f"{side}_wall_px"]  = round(px, 2)
        row[f"{side}_wall_sz"]  = round(notional, 1)
        row[f"{side}_wall_bps"] = round(dist, 3)

    return row


# Ordre des colonnes des CSV — dérivé de la config pour rester cohérent.
COLUMNS = (
    ["ts", "best_bid", "best_ask", "mid", "microprice", "spread_bps"]
    + [f"{p}_{b}" for b in C.DEPTH_BANDS_BPS for p in ("bid", "ask", "obi")]
    + ["vol_achat", "vol_vente", "n_trades", "gros_achat", "gros_vente"]
    + [f"{s}_wall_{f}" for s in ("bid", "ask") for f in ("px", "sz", "bps")]
)

# Colonnes ajoutees apres le debut de la collecte : les fichiers anterieurs
# ne les contiennent pas. Les lecteurs les completent a zero plutot que
# d'echouer, et l'analyse ecarte la periode ou elles manquent.
COLONNES_FLUX = ["vol_achat", "vol_vente", "n_trades",
                 "gros_achat", "gros_vente"]
