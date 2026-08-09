"""
chercher_signal.py — Un candidat prédit-il quoi que ce soit ? Mesurer AVANT de construire.

L'erreur de méthode de ce projet a été de bâtir un bot complet autour d'un
avantage supposé, puis de découvrir après coup qu'il n'existait pas : 616
trades pour établir une espérance brute de +0.02R, soit celle d'entrées
aléatoires.

Ce script inverse l'ordre. Il calcule une bibliothèque de candidats sur des
bougies gratuites et mesure, pour chacun, s'il prédit le rendement à venir.
On ne code un bot qu'autour de ce qui survit ici.

TROIS GARDE-FOUS, parce qu'il est très facile de se mentir à ce stade :

  1. Aucun candidat ne regarde le futur : tout est calculé sur les bougies
     closes, le rendement mesuré strictement après.
  2. Le rendement est normalisé par l'ATR. Sans ça, on mesurerait surtout la
     volatilité du moment, et les périodes agitées écraseraient le reste.
  3. Chaque mesure est refaite sur une seconde moitié jamais utilisée. Un
     candidat qui change de signe entre les deux est du bruit, quelle que
     soit sa significativité sur la première.

SEUIL DE LECTURE : sur ~40 000 points, le bruit statistique vaut ~0.005.
Une corrélation (IC) de 0.02 est réelle mais minuscule ; il faut viser 0.05
et plus pour espérer survivre aux frais.

Usage :
  python chercher_signal.py --jours 900 --tf 15m
  python chercher_signal.py --jours 900 --tf 1h --horizons 1,4,12,48
"""

import argparse
import math
import statistics
from datetime import datetime, timezone

import candles as K
import config as C


# ============================ OUTILS =========================================
def sma(v, n):
    out, s = [], 0.0
    for i, x in enumerate(v):
        s += x
        if i >= n:
            s -= v[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def ecart_type(v, n):
    out = []
    for i in range(len(v)):
        if i < n - 1:
            out.append(None)
        else:
            out.append(statistics.pstdev(v[i - n + 1:i + 1]))
    return out


def atr(bougies, n=14):
    out, a = [], None
    for i, c in enumerate(bougies):
        tr = (c["high"] - c["low"] if i == 0 else
              max(c["high"] - c["low"],
                  abs(c["high"] - bougies[i-1]["close"]),
                  abs(c["low"] - bougies[i-1]["close"])))
        a = tr if a is None else a + (tr - a) / n
        out.append(a)
    return out


def rsi(closes, n=14):
    out, ag, ap = [None], None, None
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        g, p = max(d, 0.0), max(-d, 0.0)
        ag = g if ag is None else ag + (g - ag) / n
        ap = p if ap is None else ap + (p - ap) / n
        out.append(100 - 100 / (1 + ag / ap) if ap > 0 else 100.0)
    return out


def correlation(x, y):
    n = len(x)
    if n < 30:
        return 0.0, 0.0
    mx, my = statistics.fmean(x), statistics.fmean(y)
    num = sum((a-mx)*(b-my) for a, b in zip(x, y))
    dx = math.sqrt(sum((a-mx)**2 for a in x))
    dy = math.sqrt(sum((b-my)**2 for b in y))
    if dx == 0 or dy == 0:
        return 0.0, 0.0
    r = num / (dx*dy)
    t = r * math.sqrt(max(n-2, 1) / max(1e-12, 1 - r*r))
    return r, t


# ============================ CANDIDATS ======================================
def construire(bougies):
    """
    Bibliothèque de candidats, tous calculés sur des bougies CLOSES.

    Chaque famille teste une hypothèse différente sur le comportement du prix :
    suivi de tendance, retour à la moyenne, régime de volatilité, pression du
    volume, position dans la structure, effet d'horaire.
    """
    n = len(bougies)
    c = [b["close"] for b in bougies]
    h = [b["high"] for b in bougies]
    l = [b["low"] for b in bougies]
    v = [b.get("volume", 0.0) for b in bougies]
    a = atr(bougies)
    r14 = rsi(c)

    mm20, mm50, mm200 = sma(c, 20), sma(c, 50), sma(c, 200)
    vol20 = sma(v, 20)
    et20 = ecart_type(c, 20)
    at50 = sma(a, 50)

    F = {}

    def pose(nom, f):
        F[nom] = [None] * n
        for i in range(n):
            try:
                x = f(i)
            except (TypeError, ZeroDivisionError, IndexError):
                x = None
            F[nom][i] = x

    # --- suivi de tendance : le passé récent continue-t-il ? ---
    for k in (1, 4, 12, 48, 96):
        pose(f"momentum_{k}b", lambda i, k=k:
             (c[i] - c[i-k]) / a[i] if i >= k and a[i] else None)
    pose("dist_mm50_atr", lambda i: (c[i] - mm50[i]) / a[i])
    pose("dist_mm200_atr", lambda i: (c[i] - mm200[i]) / a[i])
    pose("pente_mm50", lambda i: (mm50[i] - mm50[i-20]) / a[i] if i >= 20 else None)

    # --- retour à la moyenne : l'excès se corrige-t-il ? ---
    pose("zscore_20", lambda i: (c[i] - mm20[i]) / et20[i] if et20[i] else None)
    pose("rsi_14", lambda i: r14[i] - 50 if r14[i] is not None else None)
    pose("etirement_mm20", lambda i: (c[i] - mm20[i]) / a[i])
    pose("bougies_consecutives", lambda i: sum(
        1 if c[j] > c[j-1] else -1 for j in range(max(1, i-4), i+1)))

    # --- régime de volatilité ---
    pose("atr_relatif", lambda i: a[i] / c[i] * 10_000)
    pose("expansion_vol", lambda i: a[i] / at50[i] if at50[i] else None)
    pose("amplitude_bougie", lambda i: (h[i] - l[i]) / a[i])

    # --- volume : la pression est-elle informative ? ---
    pose("volume_relatif", lambda i: v[i] / vol20[i] if vol20[i] else None)
    pose("pression_volume", lambda i:
         ((c[i] - l[i]) - (h[i] - c[i])) / (h[i] - l[i]) * (v[i] / vol20[i])
         if h[i] > l[i] and vol20[i] else None)
    pose("volume_signe_5b", lambda i: sum(
        (1 if c[j] > c[j-1] else -1) * v[j] for j in range(max(1, i-4), i+1))
        / (vol20[i] * 5) if vol20[i] else None)

    # --- position dans la structure récente ---
    for k in (48, 192):
        pose(f"position_range_{k}b", lambda i, k=k:
             (c[i] - min(l[i-k+1:i+1])) / (max(h[i-k+1:i+1]) - min(l[i-k+1:i+1])) - 0.5
             if i >= k and max(h[i-k+1:i+1]) > min(l[i-k+1:i+1]) else None)

    # --- clôture dans sa propre bougie : qui a gagné la barre ? ---
    pose("cloture_dans_barre", lambda i:
         ((c[i] - l[i]) / (h[i] - l[i]) - 0.5) if h[i] > l[i] else None)

    # --- horaire : effet de séance ---
    pose("heure_utc", lambda i:
         datetime.fromtimestamp(bougies[i]["ts"]/1000, timezone.utc).hour)
    return F, a


def rendements_futurs(bougies, a, horizon):
    """Rendement à `horizon` bougies, normalisé par l'ATR du moment."""
    c = [b["close"] for b in bougies]
    out = [None] * len(bougies)
    for i in range(len(bougies) - horizon):
        if a[i]:
            out[i] = (c[i + horizon] - c[i]) / a[i]
    return out


# ============================ ANALYSE ========================================
def deciles(x, y):
    """Écart de rendement entre le décile haut et le décile bas du candidat."""
    paires = sorted(zip(x, y))
    k = max(1, len(paires) // 10)
    bas = statistics.fmean(b for _, b in paires[:k])
    haut = statistics.fmean(b for _, b in paires[-k:])
    return haut - bas


def analyser(F, a, bougies, horizons):
    lignes = []
    milieu = len(bougies) // 2
    for nom, serie in F.items():
        entree = {"nom": nom}
        for hz in horizons:
            fut = rendements_futurs(bougies, a, hz)
            paires = [(s, f) for s, f in zip(serie, fut)
                      if s is not None and f is not None]
            if len(paires) < 500:
                continue
            x = [p[0] for p in paires]
            y = [p[1] for p in paires]
            ic, t = correlation(x, y)
            # Hors échantillon : seconde moitié, jamais regardée pour choisir
            p2 = [(s, f) for k, (s, f) in enumerate(zip(serie, fut))
                  if k >= milieu and s is not None and f is not None]
            ic2 = correlation([p[0] for p in p2], [p[1] for p in p2])[0] if len(p2) > 500 else 0.0
            entree[hz] = {"ic": ic, "t": t, "ic2": ic2, "n": len(paires),
                          "dec": deciles(x, y)}
        if len(entree) > 1:
            lignes.append(entree)
    return lignes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="binance_hist")
    ap.add_argument("--jours", type=int, default=900)
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--horizons", default="1,4,12,48")
    ap.add_argument("--yahoo", metavar="SYMBOLE",
                    help="source Yahoo au lieu de ccxt : NQ=F, ES=F, QQQ...")
    args = ap.parse_args()

    horizons = [int(x) for x in args.horizons.split(",")]
    if args.yahoo:
        # Les futures ne passent pas par ccxt. Yahoo permet de tester NQ avant
        # de payer la licence CME (290 $/mois) pour le carnet en API.
        b = K.fetch_yahoo(args.yahoo, args.tf, verbose=False)
        print(f"source Yahoo — {args.yahoo}")
    else:
        C.use_venue(args.venue)
        b = K.fetch(args.venue, args.tf, days=args.jours, verbose=False)
    d0 = datetime.fromtimestamp(b[0]["ts"]/1000, timezone.utc)
    d1 = datetime.fromtimestamp(b[-1]["ts"]/1000, timezone.utc)
    print(f"{len(b):,} bougies {args.tf} — {d0:%Y-%m-%d} -> {d1:%Y-%m-%d}")
    print(f"bruit statistique attendu : ~{1/math.sqrt(len(b)):.4f}\n")

    F, a = construire(b)
    print(f"{len(F)} candidats, horizons {horizons} bougies\n")
    lignes = analyser(F, a, b, horizons)

    entete = f"{'candidat':<22}"
    for hz in horizons:
        entete += f"{'IC@'+str(hz):>9}"
    entete += f"{'meilleur':>10}{'hors ech.':>11}{'decile':>9}"
    print(entete)
    print("─" * len(entete))

    classement = []
    for e in lignes:
        best_hz = max((hz for hz in horizons if hz in e),
                      key=lambda hz: abs(e[hz]["ic"]), default=None)
        if best_hz is None:
            continue
        ligne = f"{e['nom']:<22}"
        for hz in horizons:
            ligne += f"{e[hz]['ic']:>9.4f}" if hz in e else f"{'—':>9}"
        m = e[best_hz]
        # Un candidat qui change de signe hors echantillon est du bruit.
        stable = (m["ic"] * m["ic2"]) > 0
        ligne += f"{m['ic']:>+10.4f}{m['ic2']:>+11.4f}{m['dec']:>+9.3f}"
        if abs(m["ic"]) >= 0.05 and stable:
            ligne += "  <<<"
        elif abs(m["ic"]) >= 0.03 and stable:
            ligne += "  <"
        print(ligne)
        classement.append((abs(m["ic"]) if stable else 0.0, e["nom"], m, best_hz))

    print("\n" + "=" * 72)
    classement.sort(reverse=True)
    print("CANDIDATS LES PLUS PROMETTEURS (stables hors echantillon)")
    for score, nom, m, hz in classement[:8]:
        if score == 0:
            continue
        print(f"  {nom:<22} IC {m['ic']:+.4f} a {hz} bougies  "
              f"(hors ech. {m['ic2']:+.4f}, t={m['t']:.1f}, "
              f"ecart deciles {m['dec']:+.3f} ATR)")
    meilleur = classement[0][0] if classement else 0.0
    print(f"\nMeilleur IC stable : {meilleur:.4f}")
    if meilleur < 0.03:
        print("Rien d'exploitable : aucun candidat ne depasse le bruit de facon stable.")
    elif meilleur < 0.05:
        print("Faible. Reel mais probablement insuffisant apres frais — a creuser")
        print("en combinant plusieurs candidats plutot qu'en isolant celui-ci.")
    else:
        print("Assez fort pour construire dessus. Verifier la robustesse par")
        print("regime avant d'ecrire la moindre ligne de bot.")


if __name__ == "__main__":
    main()
