"""
chercher_scalp.py — Recherche d'un avantage aux horizons du scalping.

POURQUOI ICI ET PAS AILLEURS

Le calcul d'economie a 1 000 EUR est sans appel : viser 5 bps coute 200 % de
frais en crypto (impossible) contre 4 % sur micro futures (52,1 % de winrate
suffit). Chercher un signal de scalping en crypto serait donc perdre son
temps meme en le trouvant. Toute la recherche se fait sur futures.

CE QUI N'A PAS ENCORE ETE TESTE

Les analyses precedentes portaient sur des horizons de 1 a 2 heures. Le
scalping, c'est 5 a 30 minutes. C'est un angle mort reel de ce projet.

PROTOCOLE

Chaque candidat passe par `laboratoire.Labo` : echantillonnage sans
recouvrement, stabilite exigee sur les quatre quarts, seuil de |t| releve a
proportion du nombre d'essais deja menes, et traduction en dollars par
contrat micro. Les 30 % les plus recents sont mis en reserve et ne sont PAS
regardes ici.

Usage :
  python chercher_scalp.py
  python chercher_scalp.py --symbole ES=F --contrat MES
"""

import argparse

import candles as K
import chercher_signal as S
from laboratoire import Labo

CONTRATS = {
    "MNQ": {"point": 2.0, "commission": 1.24, "sym": "NQ=F"},
    "MES": {"point": 5.0, "commission": 1.24, "sym": "ES=F"},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contrat", default="MNQ", choices=list(CONTRATS))
    ap.add_argument("--symbole", default=None)
    ap.add_argument("--tf", default="5m")
    ap.add_argument("--horizons", default="1,2,3,6",
                    help="en barres : a 5m, 1=5min ... 6=30min")
    ap.add_argument("--enregistrer", action="store_true")
    args = ap.parse_args()

    spec = CONTRATS[args.contrat]
    sym = args.symbole or spec["sym"]
    horizons = [int(x) for x in args.horizons.split(",")]

    labo = Labo()
    if args.enregistrer:
        labo.enregistrer(
            f"signaux OHLCV a horizon de scalping sur {sym} en {args.tf} "
            f"(horizons {args.horizons})",
            "zone non exploree : les analyses precedentes portaient sur 1-2 h")
    print(labo.resume())

    b = K.fetch_yahoo(sym, args.tf, verbose=False)
    # La reserve reste fermee : on cherche sur la partie travail uniquement.
    travail, reserve = Labo.decouper(b)
    print(f"\n{len(b):,} barres {args.tf} sur {sym}")
    print(f"  travail  {len(travail):,} barres")
    print(f"  reserve  {len(reserve):,} barres — VERROUILLEE, non consultee ici\n")

    F, a = S.construire(travail)
    print(f"{len(F)} candidats x {len(horizons)} horizons = "
          f"{len(F)*len(horizons)} mesures\n")

    prix = travail[-1]["close"]
    atr_moy = sum(x for x in a if x) / sum(1 for x in a if x)
    print(f"prix {prix:,.0f} — ATR {atr_moy:.1f} points = "
          f"{atr_moy*spec['point']:.1f} $ par {args.contrat}")
    print(f"commission {spec['commission']:.2f} $ = "
          f"{spec['commission']/(atr_moy*spec['point'])*100:.1f} % d'un ATR\n")

    seuil = labo.seuil_t()
    print(f"{'candidat':<22}{'hz':>4}{'n':>7}{'IC':>9}{'t':>7}"
          f"{'stable':>8}{'net $':>9}")
    print("─" * 66)

    retenus = []
    for hz in horizons:
        fut = S.rendements_futurs(travail, a, hz)
        for nom, serie in F.items():
            if nom == "heure_utc":
                continue
            r = labo.evaluer(serie, fut, horizon=hz, nom=nom, prix=prix,
                             # frais exprimes en bps du prix, pour que la
                             # traduction en dollars reste homogene
                             frais_bps=spec["commission"] / (prix * spec["point"])
                             * 10_000,
                             atr=atr_moy * spec["point"])
            if r.get("erreur"):
                continue
            interessant = abs(r["t"]) >= seuil and r["stable"]
            if interessant or abs(r["t"]) >= seuil * 0.8:
                print(f"{nom:<22}{hz:>4}{r['n']:>7,}{r['ic']:>+9.4f}"
                      f"{r['t']:>+7.2f}{'oui' if r['stable'] else 'NON':>8}"
                      f"{r.get('net', 0):>+9.2f}")
            if interessant and r.get("net", -1) > 0:
                retenus.append((hz, r))

    print("\n" + "=" * 66)
    if not retenus:
        print("Aucun candidat ne franchit les trois conditions.")
        print(f"(|t| >= {seuil:.2f}, signe stable sur 4 quarts, net positif)")
        return

    print(f"{len(retenus)} CANDIDAT(S) RETENU(S)\n")
    for hz, r in sorted(retenus, key=lambda x: -abs(x[1]["t"])):
        print(labo.verdict(r))
        print(f"    horizon {hz} barres = {hz*5} min, "
              f"{r['occasions']} occasions sur {r['n']} "
              f"soit ~{r['occasions']/(len(travail)*5/60/24):.1f} par jour\n")
    print("PROCHAINE ETAPE : ne PAS ajuster ces candidats. Les porter tels")
    print("quels sur la reserve, une seule fois. Tout ajustement supplementaire")
    print("sur la partie travail transformerait la reserve en simple 2e essai.")


if __name__ == "__main__":
    main()
