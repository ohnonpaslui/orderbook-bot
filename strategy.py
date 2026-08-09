"""
strategy.py — Setup technique + confirmation par le carnet d'ordres.

Répartition des rôles, calquée sur la méthode :

  LA STRUCTURE DÉCIDE     (technical.py, bougies 15 m)
      direction  : empilement MM 20/50/200
      zone       : retracement Fibonacci en confluence avec un S/R
      stop       : invalidation du retracement (départ de la jambe)

  LE CARNET CONFIRME      (features.py, snapshots 2 s)
      pression   : OBI lissé dans le sens du setup, soutenu
      chemin     : aucun mur adverse entre l'entrée et l'objectif

C'est l'inverse d'un bot OBI classique, et c'est délibéré. Deux raisons :

1. Fidélité à la méthode — sur un ladder, le trader repère son niveau en
   analyse technique, puis se sert du DOM pour choisir la seconde d'entrée.
2. Contrainte mesurée — ancrer le stop sur un mur exigeait un carnet couvrant
   50 à 150 bps. Les perpétuels n'en renvoient que 19. En prenant le stop dans
   la structure, l'OBI suffit sur les 10 premiers bps, que tout le monde
   fournit.

La classe est stateful et alimentée snapshot par snapshot : le backtest la
rejoue exactement comme le live la consomme.
"""

import config as C
import technical as T


class SetupBookStrategy:

    # Ordre des filtres, du plus large au plus fin. Sert à afficher un
    # entonnoir lisible : on veut savoir OÙ ça bloque, pas seulement que
    # ça bloque.
    FILTRES = ("warmup", "cooldown", "pas de setup technique",
               "prix sorti de la zone", "carnet ne confirme pas",
               "confirmation trop breve", "spread", "stop trop serre",
               "TP sous les frais", "mur adverse sur le chemin",
               "invalidation du mauvais cote")

    def __init__(self):
        self.obi_ema      = None
        self.n_updates    = 0
        self.streak       = 0        # snapshots consécutifs dans le même sens
        self.streak_side  = None
        self.cooldown_til = 0.0
        self.setup        = None     # setup technique courant, ou None
        self.setup_ts     = 0.0
        self.last_reject  = None

        # --- diagnostic ---
        # Compteurs par filtre, et histogramme de l'OBI lissé. Sans eux on ne
        # peut pas savoir si CONFIRM_ENTRY est atteignable : c'est la donnée qui
        # guide la calibration.
        #
        # Tranches de 0.02 et non 0.05 : mesuré en production sur 2 109
        # snapshots, l'OBI lissé plafonne à 0.192 et 51 % des relevés tiennent
        # dans la première tranche. À 0.05 tout s'écrasait sur quatre barres,
        # trop grossier pour choisir un seuil.
        self.rejets   = {f: 0 for f in self.FILTRES}
        self.n_signal = 0
        self.obi_pas  = 0.02
        self.obi_hist = [0] * 50     # |obi_ema| de 0 à 1.0
        self.obi_max  = 0.0

        # Largeur du stop des candidats parvenus jusqu'à la géométrie du trade.
        # Sans cette mesure, « stop trop serré » ne dit pas DE COMBIEN il a
        # manqué : 27 bps pour un minimum à 28.6 n'appelle pas la même décision
        # que 12 bps. Tranches de 5 bps jusqu'à 150.
        self.stop_pas  = 5.0
        self.stop_hist = [0] * 30

    # ---------------------------------------------------------------- état
    def notify_close(self, ts):
        """Appelé par le runner à la clôture d'une position : arme le cooldown."""
        self.cooldown_til = ts + C.COOLDOWN_SEC
        self.streak, self.streak_side = 0, None

    def update_candles(self, candles, i=None):
        """
        Recalcule le setup technique. À appeler à chaque clôture de bougie —
        pas à chaque snapshot : la structure ne bouge qu'au rythme des bougies.
        """
        if i is None:
            i = len(candles) - 1
        if i < T.MM_LONG + T.PIVOT_N:
            self.setup = None
            return None
        setup, raison = T.setup(candles, i)
        self.setup = setup
        self.setup_ts = candles[i]["ts"] / 1000.0
        return raison

    # ------------------------------------------------------------- signal
    @staticmethod
    def valeur_confirmation(row):
        """
        Valeur brute du signal de confirmation, bornée dans [-1, +1].

        Le microprice est en dollars : normalisé par la demi-fourchette il
        devient comparable à un OBI, ce qui garde les seuils lisibles et le
        balayage de la phase 2 homogène entre les deux familles de signaux.
        Le microprice étant toujours compris entre le meilleur bid et le
        meilleur ask, le rapport tient naturellement dans [-1, +1].
        """
        if C.CONFIRM_SIGNAL != "mpi":
            return row[C.CONFIRM_SIGNAL]
        demi = (row["best_ask"] - row["best_bid"]) / 2
        if demi <= 0:
            return 0.0
        return max(-1.0, min(1.0, (row["microprice"] - row["mid"]) / demi))

    def update(self, row):
        """
        Consomme un snapshot de carnet et retourne un signal dict ou None.
        Doit être appelé sur CHAQUE snapshot, même position ouverte, pour que
        le lissage du signal reste continu.
        """
        obi = self.valeur_confirmation(row)
        alpha = 2 / (C.CONFIRM_EMA_SPAN + 1)
        self.obi_ema = obi if self.obi_ema is None else alpha * obi + (1 - alpha) * self.obi_ema
        self.n_updates += 1

        amplitude = abs(self.obi_ema)
        self.obi_max = max(self.obi_max, amplitude)
        self.obi_hist[min(int(amplitude / self.obi_pas),
                          len(self.obi_hist) - 1)] += 1

        side = ("buy"  if self.obi_ema >=  C.CONFIRM_ENTRY else
                "sell" if self.obi_ema <= -C.CONFIRM_ENTRY else None)
        if side and side == self.streak_side:
            self.streak += 1
        elif side:
            self.streak, self.streak_side = 1, side
        else:
            self.streak, self.streak_side = 0, None

        if self.n_updates < C.CONFIRM_EMA_SPAN:
            return self._reject("warmup")
        if row["ts"] < self.cooldown_til:
            return self._reject("cooldown")

        # --- 1. la structure doit avoir armé un setup ---
        s = self.setup
        if s is None:
            return self._reject("pas de setup technique")

        attendu = "buy" if s["direction"] > 0 else "sell"
        prix = row["mid"]
        bas, haut = s["zone"]
        if not (bas <= prix <= haut):
            return self._reject("prix sorti de la zone")

        # --- 2. le carnet doit confirmer dans le sens du setup ---
        if side != attendu:
            return self._reject("carnet ne confirme pas")
        if self.streak < C.CONFIRM_MIN_HOLD:
            return self._reject("confirmation trop breve")
        if row["spread_bps"] > C.MAX_SPREAD_BPS:
            return self._reject("spread")

        # --- 3. géométrie : stop structurel, entrée en taker ---
        buf = C.SL_BUFFER_ATR * (s["atr"] or 0.0)
        if attendu == "buy":
            entry = row["best_ask"]
            sl    = s["invalidation"] - buf
            risk  = entry - sl
            tp    = entry + C.RR * risk
        else:
            entry = row["best_bid"]
            sl    = s["invalidation"] + buf
            risk  = sl - entry
            tp    = entry - C.RR * risk

        if risk <= 0:
            return self._reject("invalidation du mauvais cote")

        stop_bps = risk / entry * 10_000
        self.stop_hist[min(int(stop_bps / self.stop_pas),
                           len(self.stop_hist) - 1)] += 1
        if stop_bps < C.MIN_STOP_BPS:
            # Le stop vient de la structure : on ne l'élargit pas pour faire
            # rentrer le trade, on écarte le setup.
            return self._reject(f"stop {stop_bps:.0f} bps < {C.MIN_STOP_BPS:.0f}")

        tp_bps = abs(tp - entry) / entry * 10_000
        if tp_bps < C.MIN_TP_BPS:
            return self._reject(f"TP {tp_bps:.0f} bps < frais")

        # --- 4. aucun mur adverse ne doit barrer le chemin de l'objectif ---
        if self._chemin_bloque(row, attendu, entry, tp):
            return self._reject("mur adverse sur le chemin")

        self.streak, self.streak_side = 0, None      # pas de re-signal en boucle
        self.last_reject = None
        self.n_signal += 1
        return {
            "side":      attendu,
            "entry":     round(entry, 2),
            "sl":        round(sl, 2),
            "tp":        round(tp, 2),
            "obi_ema":   round(self.obi_ema, 4),
            "stop_bps":  round(stop_bps, 1),
            "force":     s["force"],
            "sr":        round(s["sr"]["prix"], 2),
            "touches":   s["sr"]["touches"],
        }

    def _chemin_bloque(self, row, side, entry, tp):
        """
        Un gros mur passif entre l'entrée et l'objectif rend le TP improbable.
        Pour un achat c'est un mur à la vente (ask) situé sous le TP.
        """
        cote = "ask" if side == "buy" else "bid"
        px   = row[f"{cote}_wall_px"]
        if px <= 0 or row[f"{cote}_wall_sz"] < C.BLOQUANT_MIN_NOTIONAL:
            return False
        return entry < px < tp if side == "buy" else tp < px < entry

    def _reject(self, reason):
        self.last_reject = reason
        # Les raisons chiffrées ("stop 12 bps < 29") sont regroupées sous leur
        # famille, sinon l'entonnoir se disperse en centaines de lignes uniques.
        cle = reason
        if reason.startswith("stop "):
            cle = "stop trop serre"
        elif reason.startswith("TP "):
            cle = "TP sous les frais"
        self.rejets[cle] = self.rejets.get(cle, 0) + 1
        return None

    def diagnostic(self):
        """Instantané chiffré, destiné au dashboard et aux logs."""
        s = self.setup
        return {
            "snapshots":  self.n_updates,
            "signaux":    self.n_signal,
            "rejets":     dict(self.rejets),
            "obi_hist":   list(self.obi_hist),
            "obi_pas":    self.obi_pas,      # largeur d'une tranche
            "obi_max":    round(self.obi_max, 4),
            "stop_hist":  list(self.stop_hist),
            "stop_pas":   self.stop_pas,
            "candidats":  sum(self.stop_hist),   # ayant atteint la géométrie
            # Combien de fois chaque seuil candidat aurait été franchi. C'est
            # la table qui sert à choisir CONFIRM_ENTRY, sans rien modifier en
            # production ni attendre la phase 2.
            "obi_seuils": {
                f"{s:.2f}": sum(n for i, n in enumerate(self.obi_hist)
                                if (i + 1) * self.obi_pas > s + 1e-9)
                for s in (0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.75, 0.90)
            },
            "obi_ema":    round(self.obi_ema, 4) if self.obi_ema is not None else None,
            "obi_entry":  C.CONFIRM_ENTRY,
            "setup": None if s is None else {
                "sens":         "LONG" if s["direction"] > 0 else "SHORT",
                "force":        s["force"],
                "zone":         [round(s["zone"][0], 2), round(s["zone"][1], 2)],
                "invalidation": round(s["invalidation"], 2),
                "sr":           round(s["sr"]["prix"], 2),
                "touches":      s["sr"]["touches"],
            },
        }
