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

    def __init__(self, venue):
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
        chemin = chemin or os.path.join(C.STATE_DIR, "diagnostics.json")
        os.makedirs(os.path.dirname(chemin) or ".", exist_ok=True)

        d = self.strat.diagnostic()
        d.update({
            "venue":        self.venue,
            "symbole":      C.VENUES[self.venue]["symbol"],
            "timeframe":    C.TIMEFRAME,
            "mode":         "observation",     # aucune position n'est ouverte
            "frais_ar_bps": C.FEE_ROUNDTRIP_BPS,
            "stop_min_bps": round(C.MIN_STOP_BPS, 1),
            "obi_min_hold": C.OBI_MIN_HOLD,
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
        """Une ligne pour les logs du workflow."""
        d = self.strat.diagnostic()
        pire = max(d["rejets"].items(), key=lambda x: x[1], default=("—", 0))
        setup = d["setup"]["sens"] if d["setup"] else "aucun"
        return (f"[diag] {d['snapshots']:,} snapshots — setup {setup} — "
                f"{d['signaux']} signal(aux) — OBI max {d['obi_max']:.2f} "
                f"(seuil {C.OBI_ENTRY}) — blocage principal : {pire[0]}")
