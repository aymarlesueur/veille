#!/usr/bin/env python3
"""Veille info : récupère les flux RSS, regroupe les articles par événement,
note chaque événement selon la fiabilité et l'indépendance des sources,
et produit un digest HTML qui a une fin.

Usage : .venv/bin/python veille.py [--no-open]
"""
import calendar
import html
import math
import re
import ssl
import subprocess
import sys
import time
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import certifi
import feedparser
import yaml

ROOT = Path(__file__).parent
SSL = ssl.create_default_context(cafile=certifi.where())
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) veille-info/1.0"

RUBRIQUES = [
    ("france", "Actualité française"),
    ("international", "International"),
    ("eglise", "Église"),
    ("eco", "Économie & finance"),
    ("tech", "Tech, IA & startups"),
]
# En cas d'égalité, la rubrique la plus spécialisée l'emporte
SPECIFICITE = {"eglise": 0, "tech": 1, "eco": 2, "international": 3, "france": 4}
CAMP = {"gauche": "G", "centre-gauche": "G", "centre": "C", "centre-droit": "D", "droite": "D"}
AGENCES = re.compile(r"\b(AFP|Reuters|Associated Press)\b")

STOPWORDS = set("""
le la les un une des du de d l au aux et ou mais donc or ni car ce cet cette ces son sa ses leur leurs
mon ma mes ton ta tes notre nos votre vos qui que quoi dont ou est sont etre ete avoir a ont avait
il elle ils elles on nous vous je tu se sur sous dans par pour avec sans entre vers chez plus moins
tres tout tous toute toutes pas ne n y en apres avant depuis comme aussi encore deja fait faire selon
ans an jour jours lundi mardi mercredi jeudi vendredi samedi dimanche hier aujourd hui demain
contre face quand alors ainsi bien peu fois deux trois premier premiere nouveau nouvelle nouveaux
the a an of to in on for with by at from as is are was were be been has have had it its this that
these those and or but not no will would can could should may might new says said after over into
about than more most also their they them his her he she we you your our who what which when how
why all just up out one two first last year years week day today video photos live direct
janvier fevrier mars avril mai juin juillet aout septembre octobre novembre decembre mois semaine
january february march april may june july august september october november december month
""".split())


def charger_config():
    return yaml.safe_load((ROOT / "sources.yaml").read_text())


def telecharger(source):
    req = urllib.request.Request(source["rss"], headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL) as r:
            return source, feedparser.parse(r.read()), None
    except Exception as e:
        return source, None, str(e)


def nettoyer(texte):
    texte = re.sub(r"<[^>]+>", " ", texte or "")
    return re.sub(r"\s+", " ", html.unescape(texte)).strip()


def tokens(texte):
    texte = unicodedata.normalize("NFKD", texte.lower())
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    mots = []
    for m in re.findall(r"[a-z0-9]+", texte):
        if len(m) < 3 or m in STOPWORDS:
            continue
        if len(m) > 4 and m.endswith("s"):
            m = m[:-1]
        mots.append(m)
    return mots


def collecter(config):
    fenetre = config["reglages"]["fenetre_heures"] * 3600
    exclus = [m.lower() for m in config["reglages"].get("mots_exclus", [])]
    maintenant = time.time()
    articles, erreurs, vus = [], [], set()

    with ThreadPoolExecutor(max_workers=12) as pool:
        resultats = list(pool.map(telecharger, config["sources"]))

    for source, flux, err in resultats:
        if err or not flux or not flux.entries:
            erreurs.append((source["nom"], err or "flux vide"))
            continue
        for e in flux.entries:
            lien = e.get("link", "")
            titre = nettoyer(e.get("title", ""))
            if not titre or lien in vus:
                continue
            date = e.get("published_parsed") or e.get("updated_parsed")
            ts = calendar.timegm(date) if date else maintenant
            if maintenant - ts > fenetre:
                continue
            resume = nettoyer(e.get("summary", ""))
            bas = (titre + " " + resume).lower()
            if any(m in bas for m in exclus):
                continue
            vus.add(lien)
            agence = AGENCES.search(resume + " " + titre)
            articles.append({
                "titre": titre,
                "resume": resume,
                "lien": lien,
                "ts": ts,
                "source": source,
                "media": source.get("media", source["nom"]),
                "agence": agence.group(1) if agence else None,
                # le titre compte double : il résume l'événement mieux que le chapô
                "mots": tokens(titre) * 2 + tokens(resume),
            })
    return articles, erreurs


def vectoriser(articles):
    df = Counter()
    for a in articles:
        df.update(set(a["mots"]))
    n = len(articles)
    for a in articles:
        tf = Counter(a["mots"])
        v = {m: c * math.log(n / df[m]) for m, c in tf.items() if df[m] > 1}
        norme = math.sqrt(sum(x * x for x in v.values())) or 1
        a["vec"] = {m: x / norme for m, x in v.items()}


def regrouper(articles, seuil):
    """Regroupement incrémental : chaque article rejoint le groupe dont le
    centroïde lui ressemble le plus (cosinus), s'il dépasse le seuil."""
    groupes = []  # {"articles": [...], "somme": {mot: poids}, "norme": float}
    index = defaultdict(set)  # mot -> groupes qui le contiennent
    for a in sorted(articles, key=lambda a: a["ts"]):
        communs = Counter()
        for m in a["vec"]:
            if not m.isdigit():  # « 2027 » seul ne prouve pas qu'on parle du même sujet
                communs.update(index[m])
        meilleur, sim_max = None, seuil
        # au moins deux mots en commun : un seul mot (« city », « apple »…) ne suffit pas
        for gi in (gi for gi, n in communs.items() if n >= 2):
            g = groupes[gi]
            sim = sum(x * g["somme"].get(m, 0) for m, x in a["vec"].items()) / g["norme"]
            # rapprocher deux articles isolés est plus risqué : on exige plus de ressemblance
            if len(g["articles"]) == 1:
                sim /= 1.25
            if sim > sim_max:
                meilleur, sim_max = gi, sim
        if meilleur is None:
            groupes.append({"articles": [], "somme": {}, "norme": 1})
            meilleur = len(groupes) - 1
        g = groupes[meilleur]
        g["articles"].append(a)
        for m, x in a["vec"].items():
            g["somme"][m] = g["somme"].get(m, 0) + x
            index[m].add(meilleur)
        g["norme"] = math.sqrt(sum(x * x for x in g["somme"].values())) or 1
    return fusionner(groupes, seuil)


def fusionner(groupes, seuil):
    """Second passage : un gros sujet (ex. le budget) peut avoir été coupé en
    plusieurs groupes. On fusionne les groupes dont les centroïdes se ressemblent."""
    multi = [g for g in groupes if len(g["articles"]) >= 2]
    parent = list(range(len(multi)))

    def racine(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, gi in enumerate(multi):
        for j in range(i + 1, len(multi)):
            gj = multi[j]
            dot = sum(x * gj["somme"].get(m, 0) for m, x in gi["somme"].items())
            if dot / (gi["norme"] * gj["norme"]) > seuil:
                parent[racine(j)] = racine(i)

    fusion = defaultdict(list)
    for i, g in enumerate(multi):
        fusion[racine(i)].extend(g["articles"])
    return list(fusion.values()) + [g["articles"] for g in groupes if len(g["articles"]) < 2]


def evaluer(arts):
    """Calcule le score d'un événement à partir de ses sources."""
    par_media = {}
    for a in arts:
        if a["media"] not in par_media or a["source"]["fiabilite"] > par_media[a["media"]]["source"]["fiabilite"]:
            par_media[a["media"]] = a
    medias = list(par_media.values())

    # Plusieurs médias qui reprennent la même dépêche ne comptent que pour une source
    agences = {a["agence"] for a in medias if a["agence"]}
    independantes = sum(1 for a in medias if not a["agence"]) + len(agences)

    fiab = sum(a["source"]["fiabilite"] for a in medias) / len(medias)
    camps = {CAMP.get(a["source"]["orientation"], "C") for a in medias}
    officiel = any(a["source"]["type"] == "officiel" for a in medias)

    rubriques = Counter(a["source"]["rubrique"] for a in arts)
    rubrique = min(rubriques, key=lambda r: (-rubriques[r], SPECIFICITE[r]))

    principal = max(medias, key=lambda a: (a["source"]["fiabilite"], len(a["resume"])))
    score = independantes * fiab / 10 + 0.5 * (len(camps) - 1) + (0.3 if officiel else 0)
    return {
        "titre": principal["titre"],
        "resume": principal["resume"],
        "medias": sorted(medias, key=lambda a: -a["source"]["fiabilite"]),
        "independantes": independantes,
        "agences": sorted(agences),
        "fiabilite": fiab,
        "camps": camps,
        "officiel": officiel,
        "rubrique": rubrique,
        "score": score,
        "ts": max(a["ts"] for a in arts),
    }


def selectionner(evenements, reglages):
    """Renvoie le sujet à la une (le plus recoupé) puis, par rubrique,
    les sujets recoupés et quelques sujets non recoupés de sources fiables."""
    recoupes = sorted((e for e in evenements if e["independantes"] >= 2), key=lambda e: -e["score"])
    une = recoupes[0] if recoupes else None
    par_rubrique = {}
    for cle, _ in RUBRIQUES:
        evs = [e for e in evenements if e["rubrique"] == cle and e is not une]
        rec = [e for e in recoupes if e["rubrique"] == cle and e is not une]
        seuls = sorted(
            (e for e in evs if e["independantes"] < 2 and e["fiabilite"] >= reglages["fiabilite_min_non_recoupe"]),
            key=lambda e: (-e["fiabilite"], -e["ts"]),
        )
        par_rubrique[cle] = (
            rec[: reglages["max_sujets_par_rubrique"]],
            seuls[: reglages["max_non_recoupes"]],
        )
    return une, par_rubrique


# ── Rendu HTML ──────────────────────────────────────────────────────

ONGLETS = {"france": "France", "international": "Monde", "eglise": "Église", "eco": "Éco", "tech": "Tech"}


def esc(s):
    return html.escape(s or "")


def virgule(x):
    return f"{x:.1f}".replace(".", ",")


def recoupement(ev, non_recoupe):
    """La rangée de pastilles : une par média, colorée selon son camp."""
    if non_recoupe:
        m = ev["medias"][0]
        return (f'<div class="recoup"><span class="pastilles"><i class="p seul"></i></span>'
                f'<span>Pas encore recoupé, {esc(m["media"])} ({m["source"]["fiabilite"]}/10)</span></div>')
    pastilles = "".join(
        f'<i class="p {CAMP.get(a["source"]["orientation"], "C")}" title="{esc(a["media"])}, {a["source"]["fiabilite"]}/10"></i>'
        for a in ev["medias"]
    )
    n, nm = ev["independantes"], len(ev["medias"])
    texte = f"{n} sources" if n == nm else f"{nm} médias, {n} sources indépendantes"
    if ev["agences"]:
        texte += f" (dépêche {', '.join(ev['agences'])})"
    texte += f", fiabilité {virgule(ev['fiabilite'])}"
    return f'<div class="recoup"><span class="pastilles">{pastilles}</span><span>{esc(texte)}</span></div>'


def sujet(ev, non_recoupe=False, une=False):
    principal = ev["medias"][0]
    sources = "".join(
        f'<li><a href="{esc(a["lien"])}" target="_blank" rel="noopener">'
        f'<b class="{CAMP.get(a["source"]["orientation"], "C")}">{esc(a["media"])}</b> {esc(a["titre"])}</a></li>'
        for a in ev["medias"]
    )
    details = "" if non_recoupe else (
        f'<details><summary>Lire les {len(ev["medias"])} articles</summary><ul>{sources}</ul></details>'
    )
    resume = f'<p class="resume">{esc(ev["resume"])}</p>' if ev["resume"] else ""
    titre = f'<a href="{esc(principal["lien"])}" target="_blank" rel="noopener">{esc(ev["titre"])}</a>'
    return f"""<article class="sujet{' une' if une else ''}">
  {recoupement(ev, non_recoupe)}
  <h3>{titre}</h3>
  {resume}
  {details}
</article>"""


def rendre(une, par_rubrique, nb_articles, nb_sources, erreurs):
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
            "septembre", "octobre", "novembre", "décembre"]
    now = datetime.now()
    jour = f"{jours[now.weekday()].capitalize()} {now.day}{'er' if now.day == 1 else ''} {mois[now.month - 1]}"
    total = sum(len(r) + len(s) for r, s in par_rubrique.values()) + (1 if une else 0)

    onglets, sections = "", ""
    for cle, nom in RUBRIQUES:
        recoupes, seuls = par_rubrique[cle]
        if not recoupes and not seuls:
            continue
        onglets += f'<a href="#{cle}">{ONGLETS[cle]}</a>'
        sections += f'<section id="{cle}"><h2>{nom}</h2>'
        sections += "".join(sujet(e) for e in recoupes)
        sections += "".join(sujet(e, non_recoupe=True) for e in seuls)
        sections += "</section>"

    bloc_une = ""
    if une:
        bloc_une = f'<p class="intro-une">Le sujet le plus recoupé ce matin</p>{sujet(une, une=True)}'

    err = ""
    if erreurs:
        err = f"<p>Indisponibles aujourd'hui : {esc(', '.join(n for n, _ in erreurs))}.</p>"

    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Veille">
<meta name="theme-color" content="#eef1f5" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0e1524" media="(prefers-color-scheme: dark)">
<title>Veille · {now:%d/%m}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wdth,wght@12..96,75..100,400..800&display=swap" rel="stylesheet">
<style>
:root {{
  --fond:#eef1f5; --encre:#15213b; --doux:#5d6a7e; --trait:#d5dbe4; --survol:#e3e8ef;
  --G:#d1495b; --C:#8b95a7; --D:#2f6fc0; --seul:#b7791f;
}}
@media (prefers-color-scheme: dark) {{ :root {{
  --fond:#0e1524; --encre:#e6eaf1; --doux:#8e99ab; --trait:#243049; --survol:#172238;
  --G:#f07b8b; --C:#9ba6b9; --D:#70a4ea; --seul:#e0a94f;
}} }}
* {{ box-sizing:border-box }}
html {{ scroll-behavior:smooth; scroll-padding-top:64px }}
@media (prefers-reduced-motion: reduce) {{ html {{ scroll-behavior:auto }} }}
body {{ margin:0; background:var(--fond); color:var(--encre);
  font-family:"Bricolage Grotesque", system-ui, sans-serif; font-optical-sizing:auto;
  font-size:16px; line-height:1.5; -webkit-text-size-adjust:100% }}
a {{ color:inherit; text-decoration:none }}
a:focus-visible, summary:focus-visible {{ outline:2px solid var(--D); outline-offset:3px; border-radius:2px }}
main {{ max-width:640px; margin:0 auto; padding:0 16px calc(56px + env(safe-area-inset-bottom)) }}

header {{ padding:48px 0 20px }}
header h1 {{ font-size:clamp(40px, 11vw, 64px); line-height:.95; font-weight:800;
  font-variation-settings:"wdth" 78; letter-spacing:-.02em; margin:0 }}
header p {{ color:var(--doux); margin:12px 0 0 }}

nav {{ position:sticky; top:0; z-index:1; background:var(--fond); margin:0 -16px;
  padding:10px 16px; display:flex; gap:6px; overflow-x:auto; scrollbar-width:none;
  border-bottom:1px solid var(--trait) }}
nav::-webkit-scrollbar {{ display:none }}
nav a {{ flex:none; padding:6px 14px; border-radius:99px; background:var(--survol);
  font-weight:600; font-size:14px }}

.intro-une {{ color:var(--doux); font-size:14px; margin:32px 0 -8px }}
h2 {{ font-size:28px; font-weight:800; font-variation-settings:"wdth" 78;
  letter-spacing:-.01em; margin:48px 0 4px }}

.sujet {{ padding:18px 0; border-bottom:1px solid var(--trait) }}
.sujet:last-child {{ border-bottom:0 }}
.sujet h3 {{ font-size:19px; line-height:1.25; font-weight:650; margin:8px 0 0 }}
.sujet h3 a:hover {{ text-decoration:underline; text-decoration-thickness:1px; text-underline-offset:3px }}
.une {{ border-bottom:0; padding-bottom:8px }}
.une h3 {{ font-size:clamp(26px, 7vw, 34px); line-height:1.1; font-weight:750;
  font-variation-settings:"wdth" 85; letter-spacing:-.01em }}
.une .p {{ width:12px; height:12px }}

.resume {{ color:var(--doux); margin:6px 0 0; display:-webkit-box; -webkit-box-orient:vertical;
  -webkit-line-clamp:2; overflow:hidden }}
.une .resume {{ -webkit-line-clamp:3; font-size:17px }}

.recoup {{ display:flex; align-items:center; gap:10px; font-size:13px; color:var(--doux) }}
.pastilles {{ display:flex; gap:3px; flex:none }}
.p {{ display:block; width:9px; height:9px; border-radius:50% }}
.p.G {{ background:var(--G) }} .p.C {{ background:var(--C) }} .p.D {{ background:var(--D) }}
.p.seul {{ border:1.5px solid var(--seul) }}

details {{ margin-top:8px; font-size:14px }}
summary {{ cursor:pointer; color:var(--doux); list-style:none; display:inline-block }}
summary::-webkit-details-marker {{ display:none }}
summary::before {{ content:"+"; display:inline-block; width:1em; font-weight:700 }}
details[open] summary::before {{ content:"−" }}
details ul {{ list-style:none; margin:8px 0 0; padding:0 }}
details li a {{ display:block; padding:7px 10px; border-radius:8px; line-height:1.35 }}
details li a:hover {{ background:var(--survol) }}
details b {{ font-weight:700; margin-right:4px }}
b.G {{ color:var(--G) }} b.D {{ color:var(--D) }}

footer {{ margin-top:56px; padding-top:20px; border-top:1px solid var(--trait);
  color:var(--doux); font-size:13px }}
.legende {{ display:flex; flex-wrap:wrap; gap:14px; margin-bottom:10px }}
.legende span {{ display:flex; align-items:center; gap:6px }}
</style></head><body><main>
<header>
  <h1>{jour}</h1>
  <p>{total} sujets ce matin, tirés de {nb_articles} articles et {nb_sources} sources.</p>
</header>
<nav aria-label="Rubriques">{onglets}</nav>
{bloc_une}
{sections}
<footer>
  <div class="legende">
    <span><i class="p G"></i> gauche</span><span><i class="p C"></i> centre</span>
    <span><i class="p D"></i> droite</span><span><i class="p seul"></i> une seule source</span>
  </div>
  <p>Une pastille par média qui couvre le sujet. Plusieurs reprises d'une même dépêche d'agence
  ne comptent que pour une source. Le classement favorise les sujets confirmés par des sources
  nombreuses, fiables et de camps différents.</p>
  {err}
  <p>Mis à jour à {now:%H h %M}.</p>
</footer>
</main></body></html>"""


def main():
    config = charger_config()
    reglages = config["reglages"]
    articles, erreurs = collecter(config)
    if not articles:
        sys.exit("Aucun article récupéré. Vérifie ta connexion.")
    vectoriser(articles)
    groupes = regrouper(articles, reglages["seuil_similarite"])
    evenements = [evaluer(g) for g in groupes]
    une, par_rubrique = selectionner(evenements, reglages)

    sortie = ROOT / "digests" / f"{datetime.now():%Y-%m-%d}.html"
    sortie.parent.mkdir(exist_ok=True)
    nb_sources = len(config["sources"]) - len(erreurs)
    sortie.write_text(rendre(une, par_rubrique, len(articles), nb_sources, erreurs))

    recoupes = sum(1 for e in evenements if e["independantes"] >= 2)
    print(f"{len(articles)} articles · {len(groupes)} groupes · {recoupes} recoupés · "
          f"{len(erreurs)} source(s) en erreur → {sortie}")
    for nom, err in erreurs:
        print(f"  ⚠ {nom} : {err}")
    if "--no-open" not in sys.argv:
        subprocess.run(["open", str(sortie)])


if __name__ == "__main__":
    main()
