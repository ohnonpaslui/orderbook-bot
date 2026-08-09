"""
technical.py — Couche analyse technique : tendance, S/R, Fibonacci.

Volontairement indépendante du carnet ET du support. Le même code tourne sur
des bougies BTC/USD Kraken ou sur des bougies MNQ : c'est ce qui permet de
valider la logique maintenant, sur des données gratuites, et de trancher le
support ensuite.

Trois briques, dans l'ordre où la méthode les utilise :

  1. TENDANCE — MM 20 / 50 / 200. Le croisement MM50 × MM200 (golden cross /
     death cross) donne le biais de fond, la MM20 le momentum court terme.
  2. SUPPORTS & RÉSISTANCES — pivots fractals regroupés en zones. Une zone
     touchée plusieurs fois compte davantage qu'un plus-haut isolé.
  3. FIBONACCI — retracements de la dernière jambe d'impulsion.

Le setup naît de la CONFLUENCE : le prix revient dans la zone Fibonacci, dans
le sens de la tendance, et cette zone coïncide avec un S/R historique. Sans
confluence, on ne fait rien — c'est ce filtre qui distingue la méthode d'un
simple "acheter le retracement".

Le carnet d'ordres n'intervient qu'après, pour le déclenchement.
"""

from statistics import median

# ----------------------------- Paramètres ------------------------------------
MM_COURT, MM_MOYEN, MM_LONG = 20, 50, 200
ATR_PERIODE = 14
PIVOT_N     = 5           # fractal : extrême sur 2N+1 bougies

# Regroupement des pivots en zones S/R
SR_TOLERANCE_ATR = 0.5    # deux pivots à moins de 0.5 ATR = même zone
SR_MIN_TOUCHES   = 2      # une zone crédible a été touchée au moins 2 fois
SR_MAX_ZONES     = 12

# Profondeur d'historique prise en compte pour les pivots, en bougies.
#
# Ce n'est pas un réglage de confort, c'est une condition de cohérence. Sans
# fenêtre fixe, le nombre de pivots dépend de la quantité de bougies chargée :
# l'observateur live en charge 30 jours, un backtest peut en charger 900. Les
# zones S/R — donc les setups — différeraient entre les deux, alors que tout
# le projet repose sur le fait qu'ils exécutent le même code sur les mêmes
# règles. Une fenêtre bornée les aligne, et accessoirement rend le calcul
# linéaire au lieu de quadratique.
#
# 2000 bougies de 15 m = ~21 jours : au-delà, un ancien plus-haut n'est plus
# un niveau que le marché regarde.
SR_LOOKBACK = 2000

# Bougies nécessaires AVANT le premier instant évalué pour que tout soit défini :
# la MM200, la fenêtre de pivots, et le décalage de confirmation. En charger
# moins ne produit pas d'erreur — ça produit silencieusement zéro setup, ce qui
# est bien pire. Constaté sur un backtest chargé avec 4 jours d'amont au lieu
# de 23 : la couche technique trouvait 3 setups, le backtest zéro.
BOUGIES_REQUISES = MM_LONG + SR_LOOKBACK + PIVOT_N + 10

# Retracements Fibonacci
FIB_NIVEAUX  = (0.382, 0.5, 0.618, 0.786)
FIB_ZONE     = (0.5, 0.786)   # zone d'intérêt : le "golden pocket" élargi
FIB_MIN_LEG_ATR = 3.0         # une jambe sous 3 ATR n'est pas une impulsion


# ============================ 1. INDICATEURS =================================
def _sma(valeurs, periode):
    """Moyenne mobile simple ; None tant qu'il n'y a pas assez d'historique."""
    out, cumul = [], 0.0
    for i, v in enumerate(valeurs):
        cumul += v
        if i >= periode:
            cumul -= valeurs[i - periode]
        out.append(cumul / periode if i >= periode - 1 else None)
    return out


def _atr(candles, periode=ATR_PERIODE):
    """ATR de Wilder, en lissage exponentiel."""
    out, atr = [], None
    for i, c in enumerate(candles):
        if i == 0:
            tr = c["high"] - c["low"]
        else:
            pc = candles[i - 1]["close"]
            tr = max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc))
        atr = tr if atr is None else atr + (tr - atr) / periode
        out.append(atr)
    return out


def add_indicators(candles):
    """
    Enrichit chaque bougie de mm20/mm50/mm200, atr et marqueurs de pivot.

    Un pivot n'est confirmé que PIVOT_N bougies après coup : le champ
    `pivot_high` est posé sur la bougie de l'extrême, mais il ne devient
    connaissable qu'à l'indice i + PIVOT_N. Toute lecture doit en tenir
    compte, sinon on lit le futur (voir `_pivots_confirmes`).
    """
    closes = [c["close"] for c in candles]
    mm20, mm50, mm200 = (_sma(closes, MM_COURT), _sma(closes, MM_MOYEN),
                         _sma(closes, MM_LONG))
    atrs = _atr(candles)

    n = PIVOT_N
    for i, c in enumerate(candles):
        c["mm20"], c["mm50"], c["mm200"] = mm20[i], mm50[i], mm200[i]
        c["atr"] = atrs[i]
        c["pivot_high"] = c["pivot_low"] = None

    for i in range(n, len(candles) - n):
        fenetre = candles[i - n:i + n + 1]
        if candles[i]["high"] == max(x["high"] for x in fenetre):
            candles[i]["pivot_high"] = candles[i]["high"]
        if candles[i]["low"] == min(x["low"] for x in fenetre):
            candles[i]["pivot_low"] = candles[i]["low"]
    return candles


# ============================ 2. TENDANCE ====================================
def trend(candle):
    """
    Biais de fond à partir de l'empilement des moyennes mobiles.

    Retourne (direction, force) :
      direction : +1 haussier, -1 baissier, 0 indéterminé
      force     : 2 = empilement complet (prix > MM20 > MM50 > MM200)
                  1 = MM50 au-dessus de la MM200 (golden cross) sans plus
                  0 = pas de tendance exploitable
    """
    mm20, mm50, mm200 = candle["mm20"], candle["mm50"], candle["mm200"]
    if mm20 is None or mm50 is None or mm200 is None:
        return 0, 0
    prix = candle["close"]

    if mm50 > mm200:
        if prix > mm20 > mm50:
            return 1, 2
        return 1, 1
    if mm50 < mm200:
        if prix < mm20 < mm50:
            return -1, 2
        return -1, 1
    return 0, 0


def croisement(candles, i):
    """Détecte un golden/death cross MM50 × MM200 sur la bougie i."""
    if i == 0:
        return None
    a, b = candles[i - 1], candles[i]
    if None in (a["mm50"], a["mm200"], b["mm50"], b["mm200"]):
        return None
    if a["mm50"] <= a["mm200"] and b["mm50"] > b["mm200"]:
        return "golden"
    if a["mm50"] >= a["mm200"] and b["mm50"] < b["mm200"]:
        return "death"
    return None


# ============================ 3. SUPPORTS & RÉSISTANCES ======================
def _pivots_confirmes(candles, i):
    """
    Pivots réellement connus à l'instant i, sur la fenêtre SR_LOOKBACK.

    Deux garanties :
      - un pivot posé sur la bougie j n'est confirmé qu'en j + PIVOT_N ;
        filtrer là-dessus est ce qui empêche le backtest de lire le futur ;
      - la fenêtre est bornée, donc le résultat ne dépend pas de la quantité
        d'historique chargée (voir SR_LOOKBACK).
    """
    fin = max(0, i - PIVOT_N) + 1
    debut = max(0, fin - SR_LOOKBACK)
    hauts, bas = [], []
    for j in range(debut, fin):
        c = candles[j]
        if c["pivot_high"] is not None:
            hauts.append((j, c["pivot_high"]))
        if c["pivot_low"] is not None:
            bas.append((j, c["pivot_low"]))
    return hauts, bas


def sr_zones(candles, i, max_zones=SR_MAX_ZONES):
    """
    Regroupe les pivots connus en zones S/R.

    Retourne une liste de dicts {"prix", "touches", "type"} triée par nombre
    de touches décroissant. Deux pivots distants de moins de
    SR_TOLERANCE_ATR × ATR appartiennent à la même zone.
    """
    atr = candles[i]["atr"] or 0.0
    if atr <= 0:
        return []
    tol = SR_TOLERANCE_ATR * atr

    hauts, bas = _pivots_confirmes(candles, i)
    niveaux = [(p, "resistance") for _, p in hauts] + [(p, "support") for _, p in bas]
    if not niveaux:
        return []

    # Regroupement par ancre, pas par voisin. Comparer chaque pivot au
    # DERNIER ajouté ferait chaîner le groupe de proche en proche : sur un
    # marché dense, un seul groupe finit par absorber tout le carnet de
    # pivots (mesuré : 1573 « touches » dans une zone). L'ancre borne la
    # largeur de chaque zone à `tol`.
    niveaux.sort(key=lambda x: x[0])
    groupes, courant, ancre = [], [niveaux[0]], niveaux[0][0]
    for prix, typ in niveaux[1:]:
        if prix - ancre <= tol:
            courant.append((prix, typ))
        else:
            groupes.append(courant)
            courant, ancre = [(prix, typ)], prix
    groupes.append(courant)

    zones = []
    for g in groupes:
        if len(g) < SR_MIN_TOUCHES:
            continue
        prix = median(p for p, _ in g)
        n_res = sum(1 for _, t in g if t == "resistance")
        zones.append({
            "prix": prix,
            "touches": len(g),
            # Une zone touchée des deux côtés (ancienne résistance devenue
            # support) est la plus solide : on la marque "pivot".
            "type": ("pivot" if 0 < n_res < len(g)
                     else "resistance" if n_res else "support"),
        })
    zones.sort(key=lambda z: -z["touches"])
    return zones[:max_zones]


def zone_proche(zones, prix, tolerance):
    """La zone S/R la plus proche de `prix`, ou None au-delà de `tolerance`."""
    if not zones:
        return None
    z = min(zones, key=lambda z: abs(z["prix"] - prix))
    return z if abs(z["prix"] - prix) <= tolerance else None


# ============================ 4. FIBONACCI ===================================
def derniere_jambe(candles, i):
    """
    Dernière jambe d'impulsion connue à l'instant i, à partir des deux
    derniers pivots confirmés de sens opposé.

    Retourne {"sens", "depart", "arrivee", "amplitude"} ou None.
    """
    hauts, bas = _pivots_confirmes(candles, i)
    if not hauts or not bas:
        return None

    j_haut, prix_haut = hauts[-1]
    j_bas,  prix_bas  = bas[-1]
    amplitude = prix_haut - prix_bas
    atr = candles[i]["atr"] or 0.0
    if amplitude <= 0 or atr <= 0 or amplitude < FIB_MIN_LEG_ATR * atr:
        return None

    # Le pivot le plus récent est l'arrivée de la jambe.
    if j_bas < j_haut:
        return {"sens": 1, "depart": prix_bas, "arrivee": prix_haut,
                "amplitude": amplitude}
    return {"sens": -1, "depart": prix_haut, "arrivee": prix_bas,
            "amplitude": amplitude}


def fib_niveaux(jambe):
    """Prix de chaque retracement de la jambe, depuis son arrivée."""
    return {r: jambe["arrivee"] - jambe["sens"] * r * jambe["amplitude"]
            for r in FIB_NIVEAUX}


def fib_zone(jambe):
    """Bornes (basse, haute) de la zone d'intérêt FIB_ZONE."""
    a = jambe["arrivee"] - jambe["sens"] * FIB_ZONE[0] * jambe["amplitude"]
    b = jambe["arrivee"] - jambe["sens"] * FIB_ZONE[1] * jambe["amplitude"]
    return (min(a, b), max(a, b))


# ============================ 5. SETUP =======================================
def setup(candles, i):
    """
    Cherche un setup complet sur la bougie i.

    Retourne un dict ou None. Le champ `raison` documente le rejet, ce qui
    permet au backtest de dire QUEL filtre bloque plutôt que de renvoyer un
    silence inexploitable.
    """
    c = candles[i]
    dir_tendance, force = trend(c)
    if dir_tendance == 0:
        return None, "pas de tendance"

    jambe = derniere_jambe(candles, i)
    if jambe is None:
        return None, "pas de jambe d'impulsion"
    # On n'achète les retracements que dans le sens de la tendance de fond.
    if jambe["sens"] != dir_tendance:
        return None, "jambe contre-tendance"

    bas, haut = fib_zone(jambe)
    if not (bas <= c["close"] <= haut):
        return None, "prix hors zone fibo"

    # Confluence : la zone Fibonacci doit recouvrir un S/R historique.
    zones = sr_zones(candles, i)
    milieu = (bas + haut) / 2
    sr = zone_proche(zones, milieu, (haut - bas) / 2 + (c["atr"] or 0) * 0.5)
    if sr is None:
        return None, "pas de confluence S/R"

    # Invalidation structurelle : au-delà du départ de la jambe, le scénario
    # de retracement est mort. C'est un stop large — et c'est exactement ce
    # qu'exige l'économie des frais.
    invalidation = jambe["depart"]
    return {
        "direction":    dir_tendance,
        "force":        force,
        "zone":         (bas, haut),
        "fib":          fib_niveaux(jambe),
        "jambe":        jambe,
        "sr":           sr,
        "invalidation": invalidation,
        "atr":          c["atr"],
    }, None
