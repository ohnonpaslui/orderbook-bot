"""
config.py — Tous les paramètres du bot carnet d'ordres au même endroit.

Trois groupes :
  - COLLECTE  : figé dès le premier snapshot. Le changer invalide les données
                déjà collectées (colonnes différentes) → ne pas y toucher
                pendant une campagne de collecte.
  - FEATURES  : définition des indicateurs dérivés du carnet.
  - STRATÉGIE : seuils d'entrée. Ce sont EUX qu'on calibrera sur les données
                collectées ; les valeurs ci-dessous sont des points de départ
                plausibles, pas des valeurs validées.
"""

# ============================ MARCHÉ =========================================
EXCHANGE = "kraken"
SYMBOL   = "BTC/USD"
BOT_ID   = "obi_walls"          # nom des fichiers state/ et trades/

# ============================ COLLECTE =======================================
BOOK_DEPTH        = 100         # niveaux demandés par côté (Kraken max 500)
SNAPSHOT_INTERVAL = 2.0         # secondes entre deux snapshots
FLUSH_EVERY       = 150         # lignes gardées en mémoire avant écriture
COMMIT_EVERY      = 600         # secondes entre deux commits git
DATA_DIR          = "data"

# ============================ FEATURES =======================================
# Bandes de profondeur autour du mid, en points de base (1 bps = 0.01 %).
# L'OBI par bande est plus robuste que l'OBI par nombre de niveaux : il ne
# dépend pas de la granularité du carnet ni du nombre d'ordres empilés.
DEPTH_BANDS_BPS = (5, 10, 25, 50)

# Détection des murs de liquidité
# La bande est large (150 bps) parce que le stop s'ancre derrière le mur : un
# mur collé au prix donne un stop que les frais rendent impossible à rentabiliser
# (voir MIN_TP_BPS plus bas). On cherche donc des murs structurels, pas le
# premier gros ordre venu au meilleur niveau.
WALL_BAND_BPS     = 150.0       # on ne cherche des murs que dans ±150 bps du mid
WALL_MIN_MULT     = 4.0         # taille >= 4x la médiane des niveaux de la bande
WALL_MIN_NOTIONAL = 50_000.0    # ... et au moins 50 000 $ notionnel

# ============================ STRATÉGIE ======================================
# --- signal OBI ---
OBI_BAND      = 10              # bande utilisée comme signal principal (bps)
OBI_EMA_SPAN  = 15              # lissage ~30 s à 2 s/snapshot
OBI_ENTRY     = 0.35            # |OBI lissé| requis pour armer un signal
OBI_MIN_HOLD  = 10              # snapshots consécutifs au-dessus du seuil (~20 s)

# --- mur de liquidité servant d'ancrage au stop ---
# La fenêtre [52, 120] bps n'est pas arbitraire : sous 52 bps, RR x risque ne
# couvre pas MIN_TP_BPS et le signal serait rejeté systématiquement ; au-delà
# de 120 bps le stop devient si large que le TP (2x) exige un mouvement de
# 2.4 % — trop rare en intraday pour alimenter le bot.
WALL_MIN_DIST_BPS = 52.0        # mur trop collé au prix = trade mangé par les frais
WALL_MAX_DIST_BPS = 120.0       # mur trop loin = TP hors de portée
SL_BUFFER_BPS     = 5.0         # stop placé juste derrière le mur

# --- gestion du trade ---
RR             = 2.0            # relevé à 2.0 : à 52 bps de frais, RR 1.5 ne paie plus
MAX_SPREAD_BPS = 3.0            # au-delà, marché trop dégradé pour entrer
COOLDOWN_SEC   = 600            # pause après une clôture de position
MAX_HOLD_SEC   = 14400          # sortie forcée au bout de 4 h

# --- garde-fou frais ---
# Kraken taker : 0.40 % au palier de base, 0.26 % dès 50 k$ de volume 30 j.
# Un aller-retour coûte donc 52 à 80 bps. Un TP qui ne couvre pas largement
# ce coût est perdant d'avance, quelle que soit la qualité du signal.
#
# Conséquence directe à garder en tête : avec 52 bps de frais, exiger un TP
# à 3x les frais impose une cible de ~156 bps (1.56 %) sur BTC. Ce n'est plus
# du scalping à la seconde, c'est du swing intraday déclenché par le carnet.
# C'est le seul régime rentable tant qu'on paie du taker sur Kraken.
FEE_PCT_PER_SIDE  = 0.26
FEE_ROUNDTRIP_BPS = FEE_PCT_PER_SIDE * 100 * 2      # 52 bps
MIN_TP_MULT_FEES  = 2.0                             # marge exigée sur les frais
MIN_TP_BPS        = MIN_TP_MULT_FEES * FEE_ROUNDTRIP_BPS

# ============================ PAPER TRADING ==================================
START_CAPITAL  = 1000.0
RISK_PER_TRADE = 1.0            # % du capital risqué entre entrée et stop
# Un stop serré gonfle mécaniquement la taille de position : 1 % de risque sur
# un stop à 52 bps représente déjà ~1.9x le capital en notionnel. Ce plafond
# empêche un stop anormalement serré de créer un levier absurde.
MAX_NOTIONAL_MULT = 3.0
STATE_DIR      = "state"
TRADES_DIR     = "trades"
