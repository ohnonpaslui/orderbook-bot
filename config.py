"""
config.py — Tous les paramètres du bot carnet d'ordres au même endroit.

Principe : les seuils qui dépendent des frais ne sont PAS écrits en dur, ils
sont dérivés (voir `use_venue`). Changer de plateforme recalcule la géométrie
du trade au lieu de laisser des valeurs Kraken traîner dans un backtest Binance.

Trois groupes :
  - COLLECTE  : figé dès le premier snapshot. Le changer invalide les données
                déjà collectées → ne pas y toucher pendant une campagne.
  - FEATURES  : définition des indicateurs dérivés du carnet.
  - STRATÉGIE : seuils d'entrée, à calibrer en phase 2 sur les données réelles.
"""

# ============================ PLATEFORMES ====================================
# Valeurs `depth` et `span_bps` MESURÉES sur les carnets réels (août 2026,
# BTC ~64 000 $) — voir le tableau du README. `span_bps` est l'étendue que
# couvre effectivement le carnet renvoyé : c'est elle qui plafonne la distance
# à laquelle on peut encore voir un mur.
#
# `interval` respecte les quotas :
#   Kraken  : ~1 appel/s toléré sur les endpoints publics → 2 s est sûr.
#   Binance : /depth limit=5000 coûte 250 de poids, budget 6000/min.
#             5 s = 12 appels/min = 3000/min, la moitié du budget.
VENUES = {
    # Perpétuels Binance : le seul support gratuit dont les frais passent le
    # seuil de viabilité avec les stops que produit la méthode (27 % du risque
    # en 15 m — voir le tableau du README). API publique, aucun compte requis
    # pour collecter et faire du paper trading.
    # PLATEFORME CIBLE. Perpétuel linéaire PF_XBTUSD, taker 0.05 % / maker
    # 0.02 % — identique à Binance Futures, mais Kraken est une société
    # américaine : ses endpoints ne géo-bloquent PAS les runners GitHub.
    # C'est ce qui l'a fait choisir : Binance renvoie un HTTP 451 depuis les
    # États-Unis, et collectait 0 snapshot en production (constaté le
    # 2026-08-07). Carnet mesuré : 199 bids / 219 asks dans 50 bps, 28 M$.
    "kraken_futures": dict(
        exchange="krakenfutures", symbol="BTC/USD:USD",
        depth=None, interval=2.0, fee_pct=0.05, span_bps=100.0, collect=True,
    ),
    # Fonctionne en local (Europe) mais renvoie 451 depuis GitHub Actions.
    # Conservée pour comparaison manuelle, jamais collectée en production.
    "binance_futures": dict(
        exchange="binanceusdm", symbol="BTC/USDT:USDT",
        depth=1000, interval=2.0, fee_pct=0.05, span_bps=18.0, collect=False,
    ),
    # Historique importé (import_binance.py), pas collecté. Sommet de carnet
    # uniquement : la profondeur et les murs y sont absents, d'où le marqueur
    # `_source.json` que le backtest vérifie. Mêmes frais que les perpétuels
    # Kraken, ce qui rend l'économie du trade directement comparable.
    "binance_hist": dict(
        exchange="binanceusdm", symbol="BTC/USDT:USDT",
        depth=1000, interval=2.0, fee_pct=0.05, span_bps=18.0, collect=False,
    ),
    # Profondeur ramenée de 5000 à 1000 : les murs ne servent plus d'ancrage
    # au stop, seulement à détecter un blocage proche. 1000 niveaux couvrent
    # 36 bps (mesuré), largement assez — et le poids API passe de 250 à 50,
    # ce qui permet la même cadence que les autres.
    "binance": dict(
        exchange="binance", symbol="BTC/USDT",
        depth=1000, interval=2.0, fee_pct=0.10, span_bps=36.0, collect=False,
    ),
    # Kraken reste collecté : c'est ta plateforme actuelle, et la comparaison
    # en phase 2 doit se faire sur données réelles, pas sur l'estimation de
    # frais qui l'a écartée sur le papier.
    "kraken": dict(
        exchange="kraken", symbol="BTC/USD",
        depth=500, interval=2.0, fee_pct=0.26, span_bps=170.0, collect=True,
    ),
}

# Chaque plateforme collectée coûte ~3.4 Mo/jour. Binance spot est désactivée
# par défaut : elle est dominée par les perpétuels côté frais (20 vs 10 bps)
# sans rien apporter de plus. Passer `collect` à True pour la réactiver.

# Plateforme utilisée par run_bot.py en phase 3, et observée à blanc pendant
# la collecte.
LIVE_VENUE = "kraken_futures"

# Pas de temps de la couche analyse technique. Mesuré sur 240 jours : en 5 m
# le stop médian est de 21 bps et les frais mangent 48 % du risque même en
# futures ; en 15 m le stop passe à 37 bps et les frais à 27 %.
TIMEFRAME = "15m"

BOT_ID   = "obi_walls"          # racine des fichiers state/ et trades/
DATA_DIR = "data"               # data/<plateforme>/AAAA-MM-JJ/HH.csv.gz

# ============================ COLLECTE =======================================
# FLUSH_EVERY est calé sur la tranche de 10 min du collecteur (300 lignes à
# 2 s) : chaque fichier est ainsi écrit en une fois puis figé, ce qui évite
# que git ne stocke plusieurs versions complètes du même .gz.
FLUSH_EVERY  = 300              # lignes gardées en mémoire avant écriture
COMMIT_EVERY = 600              # secondes entre deux commits git

# ============================ FEATURES =======================================
# Bandes de profondeur autour du mid, en points de base (1 bps = 0.01 %).
# L'OBI par bande de prix est plus robuste que l'OBI par nombre de niveaux :
# il ne dépend ni de la granularité du carnet ni du nombre d'ordres empilés
# sur un même prix — ce qui permet de comparer Kraken et Binance, dont les
# pas de cotation diffèrent d'un facteur 100.
DEPTH_BANDS_BPS = (5, 10, 25, 50)

# Détection des murs de liquidité
WALL_MIN_MULT     = 4.0         # taille >= 4x la médiane des niveaux de la bande
WALL_MIN_NOTIONAL = 50_000.0    # ... et au moins 50 000 $ notionnel

# ============================ STRATÉGIE ======================================
# --- le carnet CONFIRME, il ne décide pas ---
# C'est la structure (invalidation du retracement) qui donne la direction et le
# stop ; le carnet dit seulement quand appuyer sur la détente.
#
# CHOIX DU SIGNAL — mesuré sur 18 907 snapshots réels (21,5 h, kraken_futures),
# par corrélation avec la variation de prix qui suit. Bruit statistique ±0.007 :
#
#   signal                    30 s     2 min    part du temps au-dessus du seuil
#   microprice normalisé     +0.121   +0.111    ~50 %   <- retenu
#   obi 5 bps                +0.046   +0.070    ~10 %
#   obi 10 bps               +0.049   +0.060     0.3 %  <- ancien choix
#   obi 50 bps               +0.022   +0.006    CONTRARIAN (-0.063 à 15 min)
#
# Trois enseignements contre-intuitifs : le microprice bat l'OBI d'un facteur
# deux, la bande large est activement nuisible (les gros ordres passifs
# éloignés se font absorber au lieu de pousser le prix), et le lissage DÉGRADE
# le signal — d'où un EMA très court.
#
# L'ancien réglage (obi 10 bps, seuil 0.35, lissage 30 s) ne franchissait son
# seuil que 0.28 % du temps : une confirmation soutenue sur 10 snapshots y
# était quasi impossible, ce qui explique les zéro signal des deux premiers
# jours.
CONFIRM_SIGNAL   = "mpi"        # "mpi" | "obi_5" | "obi_10" | "obi_25" | "obi_50"
CONFIRM_EMA_SPAN = 3            # lissage court : ~6 s à 2 s/snapshot
CONFIRM_ENTRY    = 0.50         # |signal lissé| requis pour confirmer
CONFIRM_MIN_HOLD = 10           # snapshots consécutifs au-dessus du seuil
# Avec ces valeurs : 204 épisodes de confirmation soutenue sur 21,5 h,
# soit ~10 par heure. À calibrer en phase 2, mais l'ordre de grandeur permet
# enfin au bot de se déclencher.
# Un mur adverse entre l'entrée et l'objectif bloque le chemin : inutile de
# viser un TP derrière 3 M$ d'ordres passifs.
BLOQUANT_MIN_NOTIONAL = 1_000_000.0

# --- économie du trade : c'est ici que tout se joue ---
# Les frais sont proportionnels au NOTIONNEL, le risque à la largeur du STOP.
# À risque fixe (1 % du capital) :
#       frais / risque = frais_AR_bps / stop_bps
# Un stop serré force un gros notionnel, donc des frais qui écrasent le R.
# Mesuré : stop 75 bps sur Kraken → les frais valent 69 % du risque, et il
# faudrait 56 % de winrate pour être à l'équilibre à RR 2 (contre 33 % sans
# frais). D'où ce plafond. Le stop venant maintenant de la STRUCTURE et non du
# carnet, on ne peut plus le choisir : ce plafond sert donc de FILTRE — un
# setup dont l'invalidation est trop proche est écarté, pas élargi.
MAX_FEE_FRACTION_OF_RISK = 0.35
MIN_TP_MULT_FEES         = 2.0  # plancher supplémentaire sur le TP

RR             = 2.0            # relevé de 1.5 à 2.0 : à ces frais, 1.5 ne paie plus
SL_BUFFER_ATR  = 0.25           # stop posé au-delà de l'invalidation, en ATR
MAX_SPREAD_BPS = 3.0            # au-delà, marché trop dégradé pour entrer
COOLDOWN_SEC   = 600            # pause après une clôture de position
MAX_HOLD_SEC   = 14400          # sortie forcée au bout de 4 h

# ============================ PAPER TRADING ==================================
START_CAPITAL  = 1000.0
RISK_PER_TRADE = 1.0            # % du capital risqué entre entrée et stop
# Un stop serré gonfle mécaniquement la taille de position. Ce plafond empêche
# un stop anormalement serré de créer un levier absurde.
MAX_NOTIONAL_MULT = 3.0
STATE_DIR  = "state"
TRADES_DIR = "trades"


# ============================ DÉRIVÉ =========================================
# Renseigné par use_venue() — ne pas écrire ces valeurs à la main.
VENUE = EXCHANGE = SYMBOL = None
BOOK_DEPTH = SNAPSHOT_INTERVAL = None
FEE_PCT_PER_SIDE = FEE_ROUNDTRIP_BPS = MIN_TP_BPS = MIN_STOP_BPS = None
WALL_BAND_BPS = None


def use_venue(name):
    """
    Active une plateforme et recalcule toute la géométrie qui dépend d'elle.

    À appeler avant d'instancier la stratégie ou le moteur — le reste du code
    lit ces valeurs au niveau module.
    """
    global VENUE, EXCHANGE, SYMBOL, BOOK_DEPTH, SNAPSHOT_INTERVAL
    global FEE_PCT_PER_SIDE, FEE_ROUNDTRIP_BPS, MIN_TP_BPS, MIN_STOP_BPS
    global WALL_BAND_BPS

    v = VENUES[name]
    VENUE, EXCHANGE, SYMBOL = name, v["exchange"], v["symbol"]
    BOOK_DEPTH, SNAPSHOT_INTERVAL = v["depth"], v["interval"]

    FEE_PCT_PER_SIDE  = v["fee_pct"]
    FEE_ROUNDTRIP_BPS = FEE_PCT_PER_SIDE * 100 * 2

    # Stop minimal acceptable : en-deçà, les frais dépassent la fraction
    # tolérée du risque et le trade est perdant d'avance. Sert de filtre sur
    # les setups, le stop lui-même venant de la structure.
    MIN_STOP_BPS  = FEE_ROUNDTRIP_BPS / MAX_FEE_FRACTION_OF_RISK
    MIN_TP_BPS    = MIN_TP_MULT_FEES * FEE_ROUNDTRIP_BPS
    # Bande de recherche des murs : tout ce que le carnet renvoie. Les murs ne
    # servent plus d'ancrage au stop, seulement à repérer un blocage sur le
    # chemin de l'objectif — un carnet court suffit donc.
    WALL_BAND_BPS = v["span_bps"]


use_venue(LIVE_VENUE)           # valeurs par défaut au chargement du module
