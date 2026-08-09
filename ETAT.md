# État des connaissances

Ce que ce projet a établi, ce qu'il a réfuté, et ce qui reste ouvert.
À lire avant de proposer une piste : plusieurs ont déjà été testées et
éliminées, et les re-tester ne ferait que gonfler le compteur d'essais —
donc relever la barre de significativité pour les suivantes.

---

## Réfuté

| Hypothèse | Données | Résultat |
|---|---|---|
| MM 20/50/200 + Fibonacci + S/R | 616 trades, 900 j | espérance **brute** +0,02R — celle d'entrées au hasard |
| Le carnet en confirmation d'un setup | 950 k instantanés, 22 j | n'apporte rien : −0,55R sans, −0,67R avec, −1,03R inversé |
| 22 candidats OHLCV sur BTC | 15 m et 1 h, 900 j | aucun ne garde son signe hors échantillon |
| 22 candidats OHLCV sur NQ | 1 h, 873 j | idem ; seul `atr_relatif` est stable, mais non directionnel |
| Signal composite sur NQ 5 m | contrôle hors éch. | t = +0,58 au mieux, signes inversés aux seuils bas |
| Effet de séance sur NQ | 873 j, sans recouvrement | 1 heure sur 24 à t=2,78 — exactement ce que produit le hasard |
| 19 indicateurs de flux d'ordres | 179 j, 309 M transactions | aucun stable ; le seul à t>2 rapporte l'inverse au décile |

**Le fil commun** : sur données propres et mesure honnête, rien de ce qui est
gratuitement accessible sur BTC ou NQ ne prédit la direction à un horizon
exploitable.

---

## Trois pièges rencontrés, chacun ayant produit un faux résultat

**1. Échantillons qui se recouvrent.** Un rendement à 2 h mesuré toutes les
5 min se répète 24 fois. La t-statistique est gonflée d'un facteur √24 ≈ 4,9.
Un effet de séance à t = −4,91 est devenu t = −0,36 une fois corrigé.

**2. Un signal qui ne vaut que sur une période.** `delta_cumule_12b` affichait
IC +0,051. Par quart : −0,019 / +0,062 / **+0,300** / −0,020. Tout venait d'un
seul épisode.

**3. Des frais modélisés sur le risque au lieu du notionnel.** L'erreur des
trois bots de `New-bot-3` : frais comptés à 0,01 $ par trade au lieu de ~50 $.
Elle transformait −100 % en +337 %.

Ces trois pièges sont désormais bloqués par `laboratoire.py`.

---

## Ce qui reste ouvert

**Le carnet au tick sur futures CME.** Jamais testé : la licence coûte
290 $/mois et aucun historique n'est fourni. C'est la seule pièce que les
données gratuites ne permettent pas d'atteindre.

**Les horizons très courts** (moins de 5 minutes) sur crypto. Le flux a été
agrégé en barres de 5 min ; en dessous, il faudrait travailler au tick.
Réserve importante : à ces horizons, les frais crypto (70 $ par BTC en
aller-retour) exigent un signal dix fois plus fort que tout ce qui a été
mesuré.

---

## Ce qui est acquis et réutilisable

**Le DOM temps réel** (`docs/dom.html`) — profondeur par niveau, murs
surlignés, transactions colorées selon l'agresseur, delta et delta cumulé.
Flux public, sans compte ni frais.

**Le protocole** (`laboratoire.py`) — registre des essais avec barre qui
monte, réserve verrouillée sur les 30 % les plus récents, évaluation sans
recouvrement, contrôle par quart, traduction en dollars frais compris.
Vérifié sur quatre cas de contrôle dans `tests/test_laboratoire.py`.

**Les données** — importateurs pour le carnet historique Binance
(`import_binance.py`), le flux d'ordres (`import_flux.py`), les bougies crypto
et futures (`candles.py`, source ccxt ou Yahoo).

**La machinerie de trading** — collecte auto-réparante, moteur de paper
trading avec frais réels, backtest partageant exactement le code du live,
diagnostic MFE/MAE, dashboard. Elle attend une stratégie ; elle n'en dépend
pas.

---

## Protocole pour la suite

1. **Formuler l'hypothèse avant de la tester.** `labo.enregistrer("...")`.
   Une hypothèse écrite après coup n'est pas une hypothèse, c'est une
   description de ce qu'on vient de voir.

2. **Ne jamais toucher la réserve.** `Labo.decouper()` isole les 30 % les
   plus récents. On ne les ouvre qu'une fois, sur un candidat final. Une
   réserve consultée dix fois n'en est plus une.

3. **Trois conditions cumulatives pour retenir un candidat** :
   |t| au-dessus du seuil corrigé, signe stable sur les quatre quarts, et
   résultat net positif après frais au décile extrême.

4. **Un candidat retenu n'est pas une stratégie.** Il faut ensuite le
   backtester avec le moteur complet, en paper, puis en réel — chaque étape
   pouvant encore l'éliminer.
