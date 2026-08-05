# Bot carnet d'ordres — BTC/USD

Bot de trading (paper) qui prend ses décisions sur le **carnet d'ordres** et non
sur les bougies : déséquilibre acheteur/vendeur (OBI) pour la direction, murs de
liquidité pour l'ancrage du stop.

Complémentaire aux 3 bots de `New-bot-3` (OTE, FVG-IFVG, FIBO-VOLUME), qui
travaillent eux sur la structure de prix en 5 minutes.

---

## Pourquoi trois phases et pas un bot direct

Aucune bourse ne publie d'historique de carnet d'ordres. Contrairement aux bots
bougies, **il est impossible de backtester avant d'avoir collecté soi-même les
données**. D'où le découpage :

| Phase | Quoi | Durée | Fichier |
|---|---|---|---|
| 1 | Collecte des snapshots de carnet | 1 à 2 semaines | `collector.py` |
| 2 | Calibration des seuils sur ces données | quelques heures | `backtest.py` |
| 3 | Paper trading avec les seuils validés | en continu | `run_bot.py` |

**Les valeurs de `config.py` sont des points de départ plausibles, pas des
paramètres validés.** Tant que la phase 2 n'a pas tourné, ce bot n'a aucune
espérance démontrée. C'est la différence honnête avec tes bots bougies, validés
sur 3 ans.

---

## Le problème des frais, à lire avant de toucher aux seuils

Kraken prend **0.26 % par côté** en taker (0.40 % au palier de base), soit
**52 bps d'aller-retour**. C'est la contrainte qui dimensionne toute la
stratégie :

- un scalp carnet classique vise 20-40 bps → **structurellement perdant ici** ;
- pour que le trade paie, `config.py` exige un TP ≥ 2× les frais, soit 104 bps ;
- avec RR = 2.0, ça impose un stop ≥ 52 bps, donc un mur situé entre 52 et
  120 bps du prix.

**Conséquence : ce n'est pas du scalping à la seconde, c'est du swing intraday
déclenché par le carnet.** Quelques trades par jour au maximum, tenus de
plusieurs minutes à quelques heures. Si le backtest de la phase 2 montre trop
peu de trades, la réponse n'est pas de baisser `MIN_TP_BPS` — ce serait se
mentir sur les frais — mais de passer en ordres limites (maker) ou de changer
de plateforme.

---

## Mise en route

### Phase 1 — lancer la collecte (à faire maintenant)

1. Créer un repo GitHub **public** (le dashboard lit les fichiers en raw).
2. Pousser ce dossier dedans.
3. Onglet **Actions** → activer les workflows → **Collecte carnet** →
   `Run workflow`.

Le workflow se relance seul toutes les 5 heures (sessions de 4 h 55, limite
GitHub à 6 h) et commite dans `data/` toutes les 10 minutes.

Format : `data/AAAA-MM-JJ/HH.csv.gz`, un snapshot toutes les 2 secondes,
≈ 2 Mo par jour compressés. Compter ~30 Mo pour deux semaines — un repo git
absorbe ça sans problème.

**Vérifier au bout d'une heure** que `data/` se remplit. Si le dossier reste
vide, regarder les logs du job : Kraken limite les appels publics et un
`RateLimitExceeded` répété demanderait d'augmenter `SNAPSHOT_INTERVAL`.

### Phase 2 — calibrer (dans 1 à 2 semaines)

```bash
git pull
python backtest.py
```

Sortie type : nombre de trades, winrate, PnL, profit factor, drawdown max, et
surtout **la ventilation des rejets** — quel filtre bloque le plus. C'est ça
qui guide la calibration.

Balayage des seuils principaux (OBI_ENTRY × OBI_MIN_HOLD × RR) :

```bash
python backtest.py --sweep
```

Test d'un jeu précis sans toucher au fichier :

```bash
python backtest.py --set OBI_ENTRY=0.45 --set WALL_MIN_MULT=6 --csv trades.csv
```

Validation hors échantillon — calibrer sur la première semaine, vérifier sur la
seconde :

```bash
python backtest.py --from 2026-08-06 --to 2026-08-12    # calibration
python backtest.py --from 2026-08-13 --to 2026-08-19    # contrôle
```

Un jeu de paramètres qui s'effondre sur la seconde période est du
surapprentissage, pas une stratégie.

### Phase 3 — passer en paper trading

1. Reporter les seuils retenus dans `config.py`.
2. **Désactiver** le workflow *Collecte carnet* (`run_bot.py` collecte aussi,
   deux processus se marcheraient dessus sur `data/`).
3. Décommenter le bloc `schedule` dans `.github/workflows/bot-live.yml`.

L'état vit dans `state/obi_walls.json`, les trades dans
`trades/obi_walls.csv`. `docs/index.html` les affiche.

---

## Architecture

```
config.py        tous les paramètres, un seul endroit
features.py      carnet brut → ligne de features (OBI, profondeur, murs)
strategy.py      features → signal {side, entry, sl, tp}
paper_engine.py  signal → position, SL/TP, PnL en dollars réels
collector.py     phase 1 : snapshot → data/
backtest.py      phase 2 : data/ → statistiques
run_bot.py       phase 3 : snapshot → data/ + stratégie + paper trading
docs/index.html  dashboard
```

Le backtest et le live appellent **le même `strategy.py` et le même
`paper_engine.py`**. Aucune logique n'est réimplémentée d'un côté ou de
l'autre : une divergence entre backtest et live ne peut venir que des données,
jamais du code.

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

L'OBI est calculé **par bande de prix** et non par nombre de niveaux : il ne
dépend donc ni de la granularité du carnet, ni du nombre d'ordres empilés sur
un même prix.

> Modifier `DEPTH_BANDS_BPS` ou la détection des murs change les colonnes des
> CSV et rend les données déjà collectées inexploitables. À figer avant de
> lancer la phase 1.

---

## Logique du signal

Entrée longue (short symétrique) :

1. OBI lissé (EMA) au-dessus de `OBI_ENTRY` ;
2. **de façon soutenue** pendant `OBI_MIN_HOLD` snapshots — un pic d'une
   seconde est du bruit ou du spoofing ;
3. un mur d'achat entre `WALL_MIN_DIST_BPS` et `WALL_MAX_DIST_BPS` sous le
   prix : c'est lui qui donne l'invalidation ;
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
  qui s'échange réellement. Absorption et CVD (delta de volume cumulé)
  demanderaient `fetch_trades` en parallèle. À ajouter si la phase 2 montre que
  l'OBI seul manque de tranchant.
- **Snapshots à 2 s, pas de WebSocket.** On rate ce qui se passe entre deux
  snapshots. Suffisant pour des trades tenus plusieurs minutes, insuffisant
  pour du market making.
- **Spoofing.** Un mur peut disparaître à l'approche du prix. Le filtre
  `OBI_MIN_HOLD` limite les faux départs sur l'OBI mais **ne protège pas** le
  stop ancré sur un mur retiré. Une détection de retrait de mur (le mur
  disparaît → sortie immédiate) est le premier ajout à envisager.
- **Slippage non modélisé.** Le backtest suppose une exécution au best
  bid/ask affiché. Sur 1000 $ de notionnel en BTC/USD c'est réaliste ; ça ne
  le serait plus à 6 chiffres.
- **Le repo grossit.** ~2 Mo/jour. Au-delà de quelques mois, archiver les
  vieux jours ou passer `SNAPSHOT_INTERVAL` à 5 s.
