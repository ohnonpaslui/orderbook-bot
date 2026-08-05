"""
strategy.py — Détecteur de signal « OBI + mur de liquidité ».

Idée : l'OBI dit dans quel sens la pression s'exerce, le mur dit où placer le
stop. L'un sans l'autre ne suffit pas — un OBI fort sans mur donne un trade
sans invalidation propre, un mur sans OBI donne un support sans catalyseur.

Conditions d'entrée (long ; short symétrique) :
  1. OBI lissé (EMA) au-dessus de OBI_ENTRY ...
  2. ... de façon soutenue pendant OBI_MIN_HOLD snapshots consécutifs
     — un pic d'une seconde est du bruit ou du spoofing ;
  3. un mur d'achat existe entre WALL_MIN_DIST_BPS et WALL_MAX_DIST_BPS
     sous le prix : c'est l'ancrage du stop ;
  4. le spread est normal (MAX_SPREAD_BPS) ;
  5. le TP résultant couvre MIN_TP_BPS, sinon les frais mangent le trade.

La classe est volontairement stateful et alimentée snapshot par snapshot :
le backtest la rejoue exactement comme le live la consomme.
"""

import config as C


class ObiWallsStrategy:

    def __init__(self):
        self.obi_ema      = None
        self.n_updates    = 0
        self.streak       = 0        # snapshots consécutifs dans le même sens
        self.streak_side  = None
        self.cooldown_til = 0.0
        self.last_reject  = None     # dernière raison de rejet, pour le debug

    # ---------------------------------------------------------------- état
    def notify_close(self, ts):
        """Appelé par le runner à la clôture d'une position : arme le cooldown."""
        self.cooldown_til = ts + C.COOLDOWN_SEC
        self.streak, self.streak_side = 0, None

    # ------------------------------------------------------------- signal
    def update(self, row):
        """
        Consomme un snapshot (dict au format features.compute) et retourne
        un signal dict ou None. Doit être appelé sur CHAQUE snapshot, même
        quand une position est ouverte, pour que l'EMA reste continue.
        """
        obi = row[f"obi_{C.OBI_BAND}"]

        alpha = 2 / (C.OBI_EMA_SPAN + 1)
        self.obi_ema = obi if self.obi_ema is None else alpha * obi + (1 - alpha) * self.obi_ema
        self.n_updates += 1

        # --- entretien du streak directionnel ---
        side = ("buy"  if self.obi_ema >=  C.OBI_ENTRY else
                "sell" if self.obi_ema <= -C.OBI_ENTRY else None)
        if side and side == self.streak_side:
            self.streak += 1
        elif side:
            self.streak, self.streak_side = 1, side
        else:
            self.streak, self.streak_side = 0, None

        # --- filtres de recevabilité ---
        if self.n_updates < C.OBI_EMA_SPAN:
            return self._reject("warmup")
        if row["ts"] < self.cooldown_til:
            return self._reject("cooldown")
        if side is None or self.streak < C.OBI_MIN_HOLD:
            return self._reject("obi")
        if row["spread_bps"] > C.MAX_SPREAD_BPS:
            return self._reject("spread")

        # --- le mur du côté du signal sert d'ancrage au stop ---
        wall_side = "bid" if side == "buy" else "ask"
        wall_px  = row[f"{wall_side}_wall_px"]
        wall_bps = row[f"{wall_side}_wall_bps"]
        if wall_px <= 0:
            return self._reject("pas de mur")
        if not (C.WALL_MIN_DIST_BPS <= wall_bps <= C.WALL_MAX_DIST_BPS):
            return self._reject(f"mur a {wall_bps:.0f} bps hors fenetre")

        # --- géométrie du trade : entrée en taker, stop derrière le mur ---
        buf = C.SL_BUFFER_BPS / 10_000
        if side == "buy":
            entry = row["best_ask"]                  # on paie l'offre
            sl    = wall_px * (1 - buf)
            risk  = entry - sl
            tp    = entry + C.RR * risk
        else:
            entry = row["best_bid"]                  # on vend sur la demande
            sl    = wall_px * (1 + buf)
            risk  = sl - entry
            tp    = entry - C.RR * risk

        if risk <= 0:
            return self._reject("risque nul")

        tp_bps = abs(tp - entry) / entry * 10_000
        if tp_bps < C.MIN_TP_BPS:
            return self._reject(f"TP {tp_bps:.0f} bps < frais")

        self.streak, self.streak_side = 0, None      # ne pas re-signaler en boucle
        self.last_reject = None
        return {
            "side":     side,
            "entry":    round(entry, 2),
            "sl":       round(sl, 2),
            "tp":       round(tp, 2),
            "wall_px":  wall_px,
            "wall_sz":  row[f"{wall_side}_wall_sz"],
            "obi_ema":  round(self.obi_ema, 4),
            "spread":   row["spread_bps"],
        }

    def _reject(self, reason):
        self.last_reject = reason
        return None
