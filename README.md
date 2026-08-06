# Bot carnet d'ordres — scalping intraday BTC

Bot de trading (paper) qui applique la méthode : **la structure décide, le
carnet confirme.**

- **Analyse technique** sur bougies 15 m — tendance MM 20/50/200, zones S/R,
  retracements Fibonacci. Donne la direction, la zone d'entrée et le stop.
- **Carnet d'ordres** en snapshots 2 s — déséquilibre acheteur/vendeur (OBI),
  murs bloquants. Donne le moment précis d'entrer.

C'est l'inverse d'un bot OBI classique, et c'est délibéré : sur un ladder, le
trader repère son niveau en analyse technique, puis se sert du DOM pour choisir
la seconde d'entrée.

---

## Pourquoi trois phases

Aucune bourse ne publie d'historique de carnet d'ordres — contrairement aux
bougies, qui sont gratuites et remontent à des années. **Impossible donc de
backtester la couche carnet avant de l'avoir collectée soi-même.**

| Phase | Quoi | Durée | Fichier |
|---|---|---|---|
| 1 | Collecte des snapshots de carnet | 1 à 2 semaines | `collector.py` |
| 2 | Calibration + choix de la plateforme | quelques heures | `backtest.py` |
| 3 | Paper trading | en continu | `run_bot.py` |

La couche technique, elle, est **déjà validée** sur 240 jours de bougies
réelles (`tests/test_technical.py`) : absence de lecture du futur vérifiée,
2,4 setups par jour.

---

## L'économie du trade — la contrainte qui a tout dimensionné

Les frais sont proportionnels au **notionnel**, le risque à la largeur du
**stop** :

```
frais / risque = frais_aller_retour_bps / stop_bps
```

Un stop serré force un gros notionnel, donc des frais qui écrasent le R.

**Mesuré sur 240 jours de bougies réelles**, la méthode produit un stop médian
de 21 bps en 5 m et 37 bps en 15 m. D'où cette matrice — le pourcentage est la
part du risque mangée par les frais, `*` marque ce qui passe sous 35 % :

| Support | 5m | 15m | 1h | 4h |
|---|---|---|---|---|
| Kraken spot taker 0,26 % | 252 % | 142 % | 56 % | 26 %* |
| Binance spot taker 0,10 % | 97 % | 55 % | 22 %* | 10 %* |
| **Binance futures taker 0,05 %** | 48 % | **27 %*** | 11 %* | 5 %* |
| Binance futures maker 0,02 % | 19 %* | 11 %* | 4 %* | 2 %* |

D'où les deux choix par défaut : **perpétuels Binance, structure en 15 m.**
Leur API publique est gratuite et ne demande **aucun compte** — l'ouverture
d'un compte n'intervient qu'au passage en réel.

Résultat mesuré sur le moteur (`tests/test_pipeline.py`) : frais 2,06 $ pour
10 $ risqués (21 %), un TP rapporte **+1,80R**, un SL coûte −1,21R, soit un
**winrate d'équilibre de 40 %** — contre 33 % sans frais, et 56 % sur Kraken.

> **Et les futures MNQ ?** Sur futures réglementés, la commission est fixe par
> contrat : le même stop de 21 bps coûte ~1,3 % du risque. C'est le meilleur
> support pour cette méthode, mais le DOM CME n'est pas gratuit (abonnement
> données + compte broker). La couche technique étant indépendante du support,
> elle se transpose telle quelle le jour où tu ouvres un compte.

---

## Mise en route

### Phase 1 — lancer la collecte

1. Créer un repo GitHub **public** (le dashboard lit les fichiers en raw).
2. Pousser ce dossier dedans.
3. Onglet **Actions** → activer les workflows → **Collecte carnet** →
   `Run workflow`.

Le workflow se relance seul toutes les 5 heures (sessions de 4 h 55, limite
GitHub à 6 h) et commite toutes les 10 minutes.

Format : `data/<plateforme>/AAAA-MM-JJ/HHMM.csv.gz`, une tranche de 10 minutes
par fichier. Le découpage n'est pas cosmétique : un fichier `.gz` réécrit est
restocké entièrement par git à chaque commit (les archives ne se « deltifient »
pas). Des tranches figées une fois écrites évitent une amplification mesurée à
~3,5×.

Volume : **~3,4 Mo/jour et par plateforme**. Deux plateformes sont actives par
défaut (`binance_futures` et `kraken`), soit ~7 Mo/jour, ~100 Mo pour deux
semaines. Binance spot est désactivée (`collect=False` dans `config.py`) : elle
est dominée par les perpétuels côté frais sans rien apporter de plus.

> **Risque connu : Binance peut échouer sur GitHub Actions.** Les runners sont
> sur des IP Azure américaines et Binance renvoie un HTTP 451 depuis les
> États-Unis. Le collecteur est conçu pour ça : après 10 échecs consécutifs, la
> plateforme se met en pause 5 minutes sans gêner les autres threads.
> **Vérifie les logs du premier run.** Si `binance_futures` ne collecte rien,
> il faudra héberger la collecte ailleurs — c'est la plateforme cible.

### Phase 2 — calibrer et choisir la plateforme

```bash
git pull && python backtest.py --compare
```

Rejoue la **même** stratégie sur chaque carnet collecté, chacun avec ses frais.
C'est cette sortie qui tranche où trader.

```bash
python backtest.py --venue binance_futures --sweep
```

Balaye `OBI_ENTRY` × `OBI_MIN_HOLD` × `RR`. La sortie détaillée liste la
**ventilation des rejets** : quel filtre bloque le plus. C'est ça qui guide la
calibration.

Validation hors échantillon — calibrer sur la première semaine, contrôler sur
la seconde. Un jeu de paramètres qui s'effondre sur la seconde période est du
surapprentissage :

```bash
python backtest.py --from 2026-08-06 --to 2026-08-12
python backtest.py --from 2026-08-13 --to 2026-08-19
```

### Phase 3 — paper trading

1. Reporter les seuils retenus dans `config.py`, fixer `LIVE_VENUE`.
2. **Désactiver** le workflow *Collecte carnet* (`run_bot.py` collecte aussi).
3. Décommenter le bloc `schedule` dans `.github/workflows/bot-live.yml`.

État dans `state/obi_walls.json`, trades dans `trades/obi_walls.csv`,
dashboard dans `docs/index.html` (y renseigner `CFG.user` et `CFG.repo`).

---

## Architecture

```
config.py        paramètres ; les seuils dépendant des frais sont DÉRIVÉS
candles.py       bougies OHLCV + cache incrémental
technical.py     bougies → tendance, S/R, Fibonacci → setup
features.py      carnet brut → OBI, profondeur, murs
strategy.py      setup + confirmation carnet → signal
paper_engine.py  signal → position, SL/TP, PnL en dollars réels
collector.py     phase 1 : un thread par plateforme → data/
backtest.py      phase 2 : data/ + bougies → statistiques
run_bot.py       phase 3 : collecte + stratégie + paper trading
tests/           trois suites, toutes rejouables hors ligne sauf le réseau
```

Backtest et live appellent **le même `strategy.py` et le même
`paper_engine.py`**. Une divergence ne peut venir que des données.

**Un thread par plateforme, pas par élégance :** mesuré sur les vraies API, un
appel Binance à 5000 niveaux bloque 2,5 s et un appel Kraken 1,2 s (bridage
ccxt — la latence réseau réelle n'est que de 24 ms). En séquentiel, Binance
affamait Kraken et faisait tomber sa cadence de 2 s à 7,1 s. En parallèle,
chacune tient exactement la sienne (mesuré : 2,00 s).

### Logique du signal

Entrée longue (short symétrique) :

1. **Structure** — MM50 > MM200, prix dans la zone Fibonacci d'un retracement
   de la dernière jambe haussière, et cette zone recouvre un S/R historique
   (confluence obligatoire) ;
2. **Stop** — au-delà du départ de la jambe, plus `SL_BUFFER_ATR` × ATR. Il
   vient de la structure, pas du carnet : on ne l'élargit jamais pour faire
   rentrer un trade, on écarte le setup ;
3. **Filtre frais** — stop ≥ `MIN_STOP_BPS` (dérivé des frais de la
   plateforme) et TP ≥ `MIN_TP_BPS` ;
4. **Carnet** — OBI lissé au-dessus de `OBI_ENTRY` de façon soutenue pendant
   `OBI_MIN_HOLD` snapshots (un pic d'une seconde est du bruit ou du spoofing),
   spread normal, et **aucun mur adverse entre l'entrée et l'objectif**.

Une zone S/R est un regroupement de pivots par **ancre** — comparer chaque
pivot au précédent ferait chaîner les groupes de proche en proche jusqu'à une
zone unique absorbant tout (mesuré : 1573 « touches » avant correction).

### Ce que contient une ligne de données

| Colonne | Sens |
|---|---|
| `ts` | epoch secondes UTC |
| `best_bid`, `best_ask`, `mid` | meilleurs prix |
| `microprice` | mid pondéré par le déséquilibre du meilleur niveau |
| `spread_bps` | spread en points de base |
| `bid_N`, `ask_N` | notionnel ($) dans les N bps autour du mid |
| `obi_N` | `(bid_N − ask_N) / (bid_N + ask_N)`, dans [−1, +1] |
| `{bid,ask}_wall_px/sz/bps` | mur détecté : prix, notionnel, distance au mid |

L'OBI est calculé **par bande de prix** et non par nombre de niveaux : c'est ce
qui rend les plateformes comparables, leurs pas de cotation différant d'un
facteur 100.

> Modifier `DEPTH_BANDS_BPS` change les colonnes des CSV et rend les données
> déjà collectées inexploitables. À figer avant la phase 1.

---

## Limites connues

- **Pas de flux de trades.** Le carnet montre les intentions passives, pas ce
  qui s'échange. L'absorption et le CVD demanderaient `fetch_trades` en
  parallèle. Premier ajout à envisager si l'OBI manque de tranchant.
- **Snapshots à 2 s, pas de WebSocket.** On rate ce qui se passe entre deux
  snapshots. Suffisant pour des trades tenus des minutes, pas pour du market
  making.
- **Spoofing.** `OBI_MIN_HOLD` limite les faux départs, mais rien ne détecte
  encore le retrait brutal d'un mur.
- **Slippage non modélisé.** Exécution supposée au best bid/ask affiché.
  Réaliste sur ~2 000 $ de notionnel, plus du tout à 6 chiffres.
- **Funding non modélisé.** Sur perpétuels, une position tenue au-delà du
  cycle de funding paie ou reçoit. Négligeable sur des trades de quelques
  heures, à intégrer si `MAX_HOLD_SEC` est allongé.
