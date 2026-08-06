# Bot carnet d'ordres — BTC

Bot de trading (paper) qui décide sur le **carnet d'ordres** et non sur les
bougies : déséquilibre acheteur/vendeur (OBI) pour la direction, murs de
liquidité pour l'ancrage du stop.

Complémentaire aux 3 bots de `New-bot-3` (OTE, FVG-IFVG, FIBO-VOLUME), qui
travaillent sur la structure de prix en 5 minutes.

---

## Pourquoi trois phases et pas un bot direct

Aucune bourse ne publie d'historique de carnet d'ordres. Contrairement aux bots
bougies, **il est impossible de backtester avant d'avoir collecté soi-même les
données**.

| Phase | Quoi | Durée | Fichier |
|---|---|---|---|
| 1 | Collecte des snapshots, Kraken + Binance | 1 à 2 semaines | `collector.py` |
| 2 | Calibration des seuils + choix de la plateforme | quelques heures | `backtest.py` |
| 3 | Paper trading avec les seuils validés | en continu | `run_bot.py` |

**Les valeurs de `config.py` sont des points de départ, pas des paramètres
validés.** Tant que la phase 2 n'a pas tourné, ce bot n'a aucune espérance
démontrée. C'est la différence honnête avec tes bots bougies, validés sur 3 ans.

---

## Le problème des frais — à lire avant de toucher aux seuils

C'est la contrainte qui dimensionne toute la stratégie. Les frais sont
proportionnels au **notionnel**, le risque à la largeur du **stop** :

```
frais / risque = frais_aller_retour_bps / stop_bps
```

Un stop serré force un gros notionnel, donc des frais qui écrasent le R.
Mesuré sur le moteur réel, avec un stop à 75 bps :

| Plateforme | Frais AR | Frais / R | Winrate d'équilibre (RR 2) |
|---|---|---|---|
| Kraken taker 0.40 % (palier de base) | 80 bps | 107 % | **68,9 %** |
| Kraken taker 0.26 % | 52 bps | 69 % | **56,4 %** |
| Binance taker 0.10 % | 20 bps | 27 % | 42,2 % |

Un RR de 2.0 sans frais est à l'équilibre à **33,3 %**. Sur Kraken au palier de
base, il faudrait donc doubler le winrate juste pour payer le courtier — ça
n'existe pas sur du signal de carnet.

**D'où le choix de conception central : les seuils ne sont pas écrits en dur,
ils sont dérivés des frais** (`config.use_venue`). `MAX_FEE_FRACTION_OF_RISK`
plafonne la part du risque que les frais peuvent manger, ce qui fixe
mécaniquement la distance minimale du mur :

| Plateforme | Frais AR | Fenêtre de murs | TP min | Frais / R |
|---|---|---|---|---|
| Kraken | 52 bps | [149, 170] bps | 104 bps | 35 % |
| Binance | 20 bps | [57, 100] bps | 40 bps | 35 % |

Avec cette géométrie, le winrate d'équilibre retombe à **43,9 %**, et un TP
rapporte +1,69R net au lieu de +1,31R.

Conséquence à accepter : **ce n'est pas du scalping à la seconde, c'est du
swing intraday déclenché par le carnet.** Quelques trades par jour au maximum,
tenus de plusieurs minutes à quelques heures. Si la phase 2 montre trop peu de
trades, la réponse n'est pas de baisser `MAX_FEE_FRACTION_OF_RISK` — ce serait
se mentir sur les frais.

---

## Ce que les carnets réels permettent (mesuré, août 2026, BTC ~64 000 $)

La fenêtre de murs n'est utile que si le carnet renvoyé s'étend jusque-là.
C'est ce qui a éliminé plusieurs plateformes pourtant moins chères :

| Plateforme | Profondeur max | Étendue couverte | Frais AR | Stop mini requis | Verdict |
|---|---|---|---|---|---|
| **Kraken** | 500 | **192 / 174 bps** | 52 bps | 149 bps | retenue |
| **Binance** | 5000 | **132 / 106 bps** | 20 bps | 57 bps | retenue |
| Binance | 500 | 13,9 / 9,0 bps | 20 bps | 57 bps | trop court |
| Bybit | 200 (max) | 20,9 / 16,7 bps | 11 bps | 31 bps | trop court |
| OKX | 400 (max) | 26,5 / 17,3 bps | 20 bps | 57 bps | trop court |
| Coinbase | 1000 | 210 / 182 bps | 120 bps | 343 bps | frais rédhibitoires |

Les plateformes les moins chères ont les carnets récupérables les plus étroits.
Seule Binance à `limit=5000` s'en sort — au prix d'un poids API de 250 par
appel (budget 6000/min), d'où sa cadence de 5 s contre 2 s pour Kraken.

---

## Couche analyse technique — et pourquoi la méthode appelle des futures

`technical.py` implémente la méthode : MM 20/50/200 (golden/death cross), zones
S/R par regroupement de pivots, retracements Fibonacci. Un setup n'existe que
par **confluence** — le prix revient dans la zone Fibonacci, dans le sens de la
tendance de fond, et cette zone recouvre un S/R historique.

Le module est indépendant du support et du carnet : le même code tourne sur des
bougies BTC ou MNQ. C'est ce qui a permis de le valider tout de suite sur 60
jours de bougies gratuites, sans attendre la collecte du carnet.

Mesuré sur 17 279 bougies 5 m (BTC, 60 jours) : **2,4 setups par jour**, et
surtout — c'est le chiffre décisif — un **stop médian de 21 bps** (p10 9, p90 45).

| Support | Frais AR | Frais / R au stop médian |
|---|---|---|
| Kraken taker 0,26 % | 52 bps | **248 %** |
| Binance taker 0,10 % | 20 bps | **96 %** |
| MNQ futures (~1,25 $ AR) | fixe/contrat | **1,3 %** |

**La méthode produit naturellement des stops serrés, et c'est incompatible avec
des frais proportionnels au notionnel.** Aucun réglage ne sauve la version
crypto : il faudrait des stops de 149 bps sur Kraken, soit 7× ce que la
structure de marché propose. Sur futures, la commission étant fixe par contrat,
le même stop coûte 1,3 % du risque.

C'est la raison pour laquelle les scalpeurs order flow travaillent sur futures
et pas sur crypto spot. Le support reste à trancher (accès aux données CME), et
la couche AT est prête pour les deux.

---

## Mise en route

### Phase 1 — lancer la collecte (à faire maintenant)

1. Créer un repo GitHub **public** (le dashboard lit les fichiers en raw).
2. Pousser ce dossier dedans.
3. Onglet **Actions** → activer les workflows → **Collecte carnet** →
   `Run workflow`.

Le workflow se relance seul toutes les 5 heures (sessions de 4 h 55, limite
GitHub à 6 h) et commite dans `data/` toutes les 10 minutes.

Format : `data/<plateforme>/AAAA-MM-JJ/HH.csv.gz`.
Volume mesuré : **54 o/snapshot sur Kraken, 84 o sur Binance**, soit
**~3,7 Mo/jour** pour les deux, ~52 Mo pour deux semaines. Un repo git absorbe
ça sans problème.

> **Risque connu : Binance peut échouer sur GitHub Actions.** Les runners sont
> sur des IP Azure américaines et Binance renvoie un HTTP 451 depuis les
> États-Unis. Le collecteur est conçu pour ça : après 10 échecs consécutifs, la
> plateforme se met en pause 5 minutes sans gêner l'autre thread. **Vérifie les
> logs du premier run** — si Binance ne collecte rien, la comparaison de la
> phase 2 se fera sur Kraken seul, ou il faudra héberger la collecte ailleurs.

**Vérifier au bout d'une heure** que `data/kraken/` et `data/binance/` se
remplissent tous les deux.

### Phase 2 — calibrer et choisir la plateforme (dans 1 à 2 semaines)

```bash
git pull
python backtest.py --compare
```

`--compare` rejoue la **même** stratégie sur les deux carnets, chacun avec ses
propres frais et sa propre géométrie de murs. C'est cette sortie qui tranche où
trader — sur données, pas sur intuition.

Puis, plateforme par plateforme :

```bash
python backtest.py --venue binance --sweep
```

Balaye `OBI_ENTRY` × `OBI_MIN_HOLD` × `RR`. La sortie détaillée liste aussi
**la ventilation des rejets** : quel filtre bloque le plus. C'est ça qui guide
la calibration — sur les premières données réelles, c'est `obi` qui domine, ce
qui confirme que `OBI_ENTRY = 0.35` est probablement trop haut.

Test d'un jeu précis sans toucher au fichier :

```bash
python backtest.py --venue binance --set OBI_ENTRY=0.20 --csv trades.csv
```

Validation hors échantillon — calibrer sur la première semaine, contrôler sur
la seconde :

```bash
python backtest.py --from 2026-08-06 --to 2026-08-12
python backtest.py --from 2026-08-13 --to 2026-08-19
```

Un jeu de paramètres qui s'effondre sur la seconde période est du
surapprentissage, pas une stratégie.

### Phase 3 — passer en paper trading

1. Reporter les seuils retenus dans `config.py` et fixer `LIVE_VENUE`.
2. **Désactiver** le workflow *Collecte carnet* (`run_bot.py` collecte aussi,
   deux processus se marcheraient dessus sur `data/`).
3. Décommenter le bloc `schedule` dans `.github/workflows/bot-live.yml`.

L'état vit dans `state/obi_walls.json`, les trades dans
`trades/obi_walls.csv`. `docs/index.html` les affiche — y renseigner `CFG.user`
et `CFG.repo`.

---

## Architecture

```
config.py        paramètres + dérivation des seuils depuis les frais
features.py      carnet brut → ligne de features (OBI, profondeur, murs)
strategy.py      features → signal {side, entry, sl, tp}
paper_engine.py  signal → position, SL/TP, PnL en dollars réels
collector.py     phase 1 : un thread par plateforme → data/
backtest.py      phase 2 : data/ → statistiques, --sweep, --compare
run_bot.py       phase 3 : snapshot → data/ + stratégie + paper trading
docs/index.html  dashboard
```

Le backtest et le live appellent **le même `strategy.py` et le même
`paper_engine.py`**. Aucune logique n'est réimplémentée : une divergence entre
backtest et live ne peut venir que des données, jamais du code.

**Un thread par plateforme, pas par élégance :** mesuré sur les vraies API, un
appel Binance à 5000 niveaux bloque 2,5 s et un appel Kraken 1,2 s (bridage
ccxt — la latence réseau réelle n'est que de 24 ms). En séquentiel, Binance
affamait Kraken et faisait tomber sa cadence de 2 s à 7,1 s. En parallèle,
chacune tient exactement la sienne (mesuré : 2,00 s et 5,01 s).

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

L'OBI est calculé **par bande de prix** et non par nombre de niveaux. C'est ce
qui rend Kraken et Binance comparables : leurs pas de cotation diffèrent d'un
facteur 100, donc « 10 niveaux » n'y veut pas dire la même chose, alors que
« 10 bps » si.

> Modifier `DEPTH_BANDS_BPS` ou la détection des murs change les colonnes des
> CSV et rend les données déjà collectées inexploitables. À figer avant la
> phase 1.

---

## Logique du signal

Entrée longue (short symétrique) :

1. OBI lissé (EMA) au-dessus de `OBI_ENTRY` ;
2. **de façon soutenue** pendant `OBI_MIN_HOLD` snapshots — un pic d'une
   seconde est du bruit ou du spoofing ;
3. un mur d'achat dans la fenêtre `[WALL_MIN_DIST_BPS, WALL_MAX_DIST_BPS]`
   sous le prix : c'est lui qui donne l'invalidation ;
4. spread sous `MAX_SPREAD_BPS` ;
5. TP résultant ≥ `MIN_TP_BPS`.

Un mur est un niveau dont le notionnel dépasse `WALL_MIN_MULT` × la **médiane**
des niveaux de la bande (médiane et non moyenne : un mur unique ne doit pas se
masquer lui-même en gonflant sa propre référence), avec un plancher absolu en
dollars.

Entrée en taker, stop juste derrière le mur, TP à `RR` × le risque. Sortie
forcée après `MAX_HOLD_SEC`, cooldown de `COOLDOWN_SEC` après chaque clôture.

---

## Limites connues

- **Pas de flux de trades.** Le carnet montre les intentions passives, pas ce
  qui s'échange. Absorption et CVD (delta de volume cumulé) demanderaient
  `fetch_trades` en parallèle. À ajouter si la phase 2 montre que l'OBI seul
  manque de tranchant.
- **Snapshots à 2 s / 5 s, pas de WebSocket.** On rate ce qui se passe entre
  deux snapshots. Suffisant pour des trades tenus plusieurs minutes,
  insuffisant pour du market making.
- **Spoofing.** Un mur peut disparaître à l'approche du prix. `OBI_MIN_HOLD`
  limite les faux départs sur l'OBI mais **ne protège pas** le stop ancré sur
  un mur retiré. Une détection de retrait de mur (le mur disparaît → sortie
  immédiate) est le premier ajout à envisager.
- **Slippage non modélisé.** Le backtest suppose une exécution au best bid/ask
  affiché. Sur ~2 000 $ de notionnel en BTC c'est réaliste ; ça ne le serait
  plus à 6 chiffres.
- **Le repo grossit** d'environ 3,7 Mo/jour. Au-delà de quelques mois, archiver
  les vieux jours ou allonger les intervalles.
