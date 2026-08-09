"""
diagnostics.py — Fait tourner la stratégie « à blanc » pendant la collecte.

Sans ça, la phase 1 est aveugle : on enregistre le carnet pendant deux semaines
sans savoir si un seul setup se serait armé, ni quel filtre bloque. On
découvrirait le problème à la phase 2, deux semaines trop tard.

L'observateur consomme les mêmes snapshots que le collecteur écrit sur disque,
les passe dans la stratégie réelle, et compte. Il n'ouvre jamais de position et
n'écrit rien d'autre que `state/diagnostics.json`, que le dashboard affiche.
"""

import json
import os
import time
from datetime import datetime, timezone

import candles as K
import config as C
import technical as T
from strategy import SetupBookStrategy


class Observateur:
    """Stratégie en lecture seule sur une plateforme, pour diagnostic."""

    def __init__(self, venue, chemin=None):
        self.venue   = venue
        self.strat   = SetupBookStrategy()
        self.debut   = time.time()
        self.tf_sec  = K.TF_MS[C.TIMEFRAME] / 1000
        self.bougies = []
        self.derniere_bougie = 0.0
        self.erreur_structure = None
        self.raison_setup = None
        self.premier_ts = None
        self.dernier_ts = None
        self.chemin = chemin or os.path.join(C.STATE_DIR, "diagnostics.json")
        self.cumul = self._charger()

    # ------------------------------------------------------------ cumul
    def empreinte(self):
        """
        Identifie la CONFIGURATION DE MESURE, pas seulement sa resolution.

        Cumuler deux distributions incompatibles produit une table de seuils
        fausse. C'est arrive le 2026-08-08 : le passage de l'OBI au microprice
        a melange 10 356 mesures centrees sur 0.05 avec des mesures centrees
        sur 0.5, parce que la garde ne regardait que la largeur des tranches.
        Le lissage compte aussi — il change la forme de la distribution.
        """
        return (f"{C.CONFIRM_SIGNAL}/ema{C.CONFIRM_EMA_SPAN}"
                f"/pas{self.strat.obi_pas}/n{len(self.strat.obi_hist)}")

    def _charger(self):
        """
        Reprend les compteurs de la session precedente.

        Une session GitHub Actions dure 4 h 55 : sans cette reprise, les
        compteurs repartiraient de zero cinq fois par jour et on ne verrait
        jamais qu'une fenetre de cinq heures. Or la question posee — le seuil
        est-il atteignable — ne se tranche que sur plusieurs jours et
        plusieurs regimes de marche.
        """
        vide = {"snapshots": 0, "signaux": 0, "rejets": {},
                "obi_hist": [0] * len(self.strat.obi_hist), "obi_max": 0.0,
                "stop_hist": [0] * len(self.strat.stop_hist),
                "depuis": None, "sessions": 0}
        try:
            with open(self.chemin, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return vide

        if d.get("empreinte") != self.empreinte():
            print(f"[diag] configuration de mesure modifiee "
                  f"({d.get('empreinte')} -> {self.empreinte()}) — "
                  f"compteurs remis a zero", flush=True)
            return vide

        return {
            "snapshots": int(d.get("snapshots", 0)),
            "signaux":   int(d.get("signaux", 0)),
            "rejets":    dict(d.get("rejets") or {}),
            "obi_hist":  list(d["obi_hist"]),
            "obi_max":   float(d.get("obi_max", 0.0)),
            # Ajoute apres coup : absent des anciens fichiers, on repart de zero
            # pour cet histogramme sans perdre le reste du cumul.
            "stop_hist": list(d.get("stop_hist")
                              or [0] * len(self.strat.stop_hist)),
            "depuis":    d.get("depuis") or d.get("maj"),
            # Pas de +1 ici : c'est `ecrire` qui incremente, sinon la session
            # serait comptee deux fois.
            "sessions":  int(d.get("sessions", 0)),
        }

    # ------------------------------------------------------------ structure
    def rafraichir(self):
        """Recharge les bougies et recalcule le setup. Tolère un échec réseau."""
        try:
            self.bougies = K.fetch(self.venue, C.TIMEFRAME, days=30, verbose=False)
            T.add_indicators(self.bougies)
            self.raison_setup = self.strat.update_candles(self.bougies)
            self.derniere_bougie = self.bougies[-1]["ts"] / 1000 if self.bougies else 0.0
            self.erreur_structure = None
        except Exception as e:
            # Une panne de l'API bougies ne doit pas arrêter la collecte :
            # on garde le dernier setup connu et on réessaiera.
            self.erreur_structure = f"{type(e).__name__}: {str(e)[:120]}"
            self.derniere_bougie = time.time()

    # ------------------------------------------------------------ snapshots
    def consommer(self, rows):
        """Passe une salve de snapshots dans la stratégie, sans jamais trader."""
        for row in rows:
            if row["ts"] >= self.derniere_bougie + 2 * self.tf_sec:
                self.rafraichir()
            self.strat.update(row)
            if self.premier_ts is None:
                self.premier_ts = row["ts"]
            self.dernier_ts = row["ts"]

    # ------------------------------------------------------------- sortie
    def ecrire(self, chemin=None):
        chemin = chemin or self.chemin
        os.makedirs(os.path.dirname(chemin) or ".", exist_ok=True)

        session = self.strat.diagnostic()

        # Les chiffres publies sont CUMULES depuis le debut de la campagne ;
        # la session en cours reste consultable dans `session`.
        c = self.cumul
        d = dict(session)
        d["snapshots"] = c["snapshots"] + session["snapshots"]
        d["signaux"]   = c["signaux"]   + session["signaux"]
        d["obi_max"]   = round(max(c["obi_max"], session["obi_max"]), 4)
        d["obi_hist"]  = [a + b for a, b in zip(c["obi_hist"], session["obi_hist"])]
        d["stop_hist"] = [a + b for a, b in zip(c["stop_hist"], session["stop_hist"])]
        d["candidats"] = sum(d["stop_hist"])
        d["rejets"]    = dict(c["rejets"])
        for k, v in session["rejets"].items():
            d["rejets"][k] = d["rejets"].get(k, 0) + v

        total = sum(d["obi_hist"]) or 1
        d["obi_seuils"] = {
            f"{s:.2f}": sum(n for i, n in enumerate(d["obi_hist"])
                            if (i + 1) * self.strat.obi_pas > s + 1e-9)
            for s in (0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.75, 0.90)
        }
        d["session"] = {
            "snapshots": session["snapshots"], "signaux": session["signaux"],
            "obi_max": session["obi_max"],
        }
        d["sessions"] = c["sessions"] + 1
        d["depuis"] = c["depuis"] or datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S")
        d.update({
            "venue":        self.venue,
            "symbole":      C.VENUES[self.venue]["symbol"],
            "timeframe":    C.TIMEFRAME,
            "mode":         "observation",     # aucune position n'est ouverte
            "frais_ar_bps": C.FEE_ROUNDTRIP_BPS,
            "stop_min_bps": round(C.MIN_STOP_BPS, 1),
            "obi_min_hold": C.CONFIRM_MIN_HOLD,
            "signal": C.CONFIRM_SIGNAL,
            "ema_span": C.CONFIRM_EMA_SPAN,
            "empreinte": self.empreinte(),
            "rr":           C.RR,
            "erreur_structure": self.erreur_structure,
            "raison_setup":     self.raison_setup,
            "premier_snapshot": self.premier_ts,
            "dernier_snapshot": self.dernier_ts,
            "maj": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        })
        with open(chemin, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        return d

    # -------------------------------------------------------------- resume
    def resume(self):
        """Une ligne pour les logs du workflow, en chiffres cumules."""
        s = self.strat.diagnostic()
        cumul_snap = self.cumul["snapshots"] + s["snapshots"]
        cumul_sig  = self.cumul["signaux"] + s["signaux"]
        obi_max    = max(self.cumul["obi_max"], s["obi_max"])
        rejets = dict(self.cumul["rejets"])
        for k, v in s["rejets"].items():
            rejets[k] = rejets.get(k, 0) + v
        pire = max(rejets.items(), key=lambda x: x[1], default=("—", 0))
        setup = s["setup"]["sens"] if s["setup"] else "aucun"
        return (f"[diag] {cumul_snap:,} snapshots cumules "
                f"({s['snapshots']:,} cette session) — setup {setup} — "
                f"{cumul_sig} signal(aux) — OBI max {obi_max:.3f} "
                f"(seuil {C.CONFIRM_ENTRY}) — blocage principal : {pire[0]}")
