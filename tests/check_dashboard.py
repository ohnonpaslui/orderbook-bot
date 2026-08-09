"""
Controles statiques du dashboard : syntaxe et coherence des references.
Ne remplace pas une execution, mais attrape les erreurs qui rendraient la
page blanche (accolade manquante, id inexistant, fonction non definie).
"""
import io
import os
import re

RACINE = os.path.dirname(os.path.abspath(__file__))
import sys
# Accepte un chemin en argument : le depot contient plusieurs pages.
SRC = (sys.argv[1] if len(sys.argv) > 1
       else os.path.join(RACINE, "..", "docs", "index.html"))
t = io.open(SRC, encoding="utf-8").read()

erreurs = []

# --- 1. le script est-il equilibre ? ---
script = re.search(r"<script>(.*?)</script>", t, re.S).group(1)


def depouiller(s):
    """
    Retire chaines et commentaires en parcourant les caracteres.

    Une regex ne suffit pas : les gabarits JS s'imbriquent
    (`texte ${ cond ? `interne` : "" } suite`), et une regex naive coupe au
    premier backtick interne, ce qui produit de faux desequilibres.
    """
    out, i, n = [], 0, len(s)
    pile = []          # pile de contextes : "`" (gabarit) ou "{" (interpolation)
    while i < n:
        c = s[i]
        suite = s[i + 1] if i + 1 < n else ""

        if pile and pile[-1] == "`":
            if c == "\\":
                i += 2; continue
            if c == "`":
                pile.pop(); i += 1; continue
            if c == "$" and suite == "{":
                pile.append("{"); out.append("("); i += 2; continue
            i += 1; continue

        if c == "}" and pile and pile[-1] == "{":
            pile.pop(); out.append(")"); i += 1; continue
        if c == "`":
            pile.append("`"); i += 1; continue
        if c in "'\"":
            i += 1
            while i < n and s[i] != c:
                i += 2 if s[i] == "\\" else 1
            i += 1; continue
        if c == "/" and suite == "/":
            while i < n and s[i] != "\n":
                i += 1
            continue
        if c == "/" and suite == "*":
            fin = s.find("*/", i + 2)
            i = n if fin < 0 else fin + 2
            continue
        out.append(c); i += 1
    return "".join(out), pile


net, reste = depouiller(script)
if reste:
    erreurs.append(f"chaine ou gabarit non ferme (pile restante : {reste})")
for ouvre, ferme, nom in (("{", "}", "accolades"), ("(", ")", "parentheses"),
                          ("[", "]", "crochets")):
    a, b = net.count(ouvre), net.count(ferme)
    if a != b:
        erreurs.append(f"{nom} desequilibrees : {a} ouvrantes / {b} fermantes")
print(f"structure du script : {net.count('{')} accolades, "
      f"{net.count('(')} parentheses, {net.count('[')} crochets — "
      f"{'OK' if not erreurs else 'PROBLEME'}")

# --- 2. tous les $('id') existent-ils dans le HTML ? ---
ids_html = set(re.findall(r'id="([^"]+)"', t))
ids_js = set(re.findall(r'\$\("([^"]+)"\)', script))
ids_js |= set(re.findall(r'getElementById\("([^"]+)"\)', script))
manquants = ids_js - ids_html
print(f"\nreferences DOM : {len(ids_js)} utilisees, {len(ids_html)} declarees")
if manquants:
    erreurs.append(f"id introuvables dans le HTML : {sorted(manquants)}")
else:
    print("  tous les id references existent")
inutiles = ids_html - ids_js
if inutiles:
    print(f"  (declares mais non utilises : {sorted(inutiles)})")

# --- 3. toutes les fonctions appelees sont-elles definies ? ---
# L'analyse porte sur `net`, la version SANS chaines ni commentaires. Sur le
# script brut, la prose francaise des gabarits (« ... 3 candidat(s) ... »)
# ressort comme autant d'appels a des fonctions inexistantes.
definies = set(re.findall(r"function\s+(\w+)\s*\(", net))
definies |= set(re.findall(r"const\s+(\w+)\s*=\s*(?:async\s*)?\(?[\w,\s]*\)?\s*=>",
                           net))
# Le lookbehind ecarte les APPELS DE METHODE : `m.get(p)` sur une Map n'est
# pas un appel a une fonction `get` non definie.
appelees = set(re.findall(r"(?<![.\w$])(\w+)\s*\(", net))
NATIF = {"if","for","while","switch","catch","return","function","fetch","Number",
         "String","Object","Array","Math","JSON","parseFloat","parseInt","setInterval",
         "setTimeout","console","await","typeof","map","join","filter","reduce",
         "split","slice","reverse","toFixed","replace","sort","entries","from",
         "fromEntries","min","max","abs","toLocaleString","trim","indexOf","push",
         "async","Promise","all","json","text","ok","catch","then","forEach"}
# On ne compare plus a une liste ecrite en dur — elle se desynchronisait a
# chaque nouvelle page. Toute fonction appelee sans etre definie ni native
# est signalee, ce qui vaut pour n'importe quel fichier du depot.
inconnues = {f for f in appelees - definies - NATIF
             if f.islower() and not f.startswith("_")}
print(f"\nfonctions : {len(definies)} definies")
if inconnues:
    erreurs.append(f"fonctions appelees mais non definies : {sorted(inconnues)}")
else:
    print("  toutes les fonctions appelees sont definies")

# --- 4. balises HTML fermees ? ---
for balise in ("div", "table", "tbody", "thead", "tr", "svg", "script", "style"):
    o = len(re.findall(rf"<{balise}[\s>]", t))
    f = len(re.findall(rf"</{balise}>", t))
    if o != f:
        erreurs.append(f"<{balise}> : {o} ouvertes / {f} fermees")
print(f"\nbalises HTML : {'equilibrees' if not any('<' in e for e in erreurs) else 'PROBLEME'}")

# --- 5. cles JSON attendues, cote python et cote js ---
# Derivees du code source plutot qu'ecrites en dur : une liste figee se
# desynchronise a la premiere cle ajoutee, et le controle devient un faux
# positif qu'on finit par ignorer.
def cles_produites(fichier, marqueur):
    """Cles litterales d'un dict construit dans un fichier Python."""
    src = io.open(os.path.join(RACINE, "..", fichier), encoding="utf-8").read()
    bloc = src.split(marqueur, 1)[1] if marqueur in src else src
    return set(re.findall(r'"(\w+)"\s*:', bloc))


# Ce controle ne vaut que pour une page qui lit diagnostics.json. Sur le DOM
# temps reel, `d` designe un message WebSocket : d.p, d.q, d.m sont des champs
# Binance, pas des cles de diagnostic. L'appliquer partout produirait des faux
# positifs qu'on finirait par ignorer, ce qui viderait le controle de son sens.
if "diagnostics.json" in t:
    attendues = (cles_produites("strategy.py", "def diagnostic")
                 | cles_produites("diagnostics.py", "d.update"))
    utilisees = set(re.findall(r"\bd(?:iag)?\.(\w+)", script))
    absentes = utilisees - attendues - {"setup", "rejets"}
    print(f"\ncles du diagnostic : {len(utilisees)} lues, "
          f"{len(attendues)} produites par le code")
    if absentes:
        erreurs.append(f"cles lues mais jamais ecrites par diagnostics.py : "
                       f"{sorted(absentes)}")
    else:
        print("  toutes correspondent a ce que diagnostics.py produit")
else:
    print("\ncles du diagnostic : sans objet (cette page ne lit pas "
          "diagnostics.json)")

print("\n" + "=" * 58)
if erreurs:
    print("PROBLEMES DETECTES :")
    for e in erreurs:
        print("  -", e)
    raise SystemExit(1)
print("AUCUN PROBLEME STATIQUE DETECTE")
