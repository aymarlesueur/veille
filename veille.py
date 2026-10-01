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
    par_rubrique = {}
    for cle, _ in RUBRIQUES:
        evs = [e for e in evenements if e["rubrique"] == cle]
        recoupes = sorted((e for e in evs if e["independantes"] >= 2), key=lambda e: -e["score"])
        seuls = sorted(
            (e for e in evs if e["independantes"] < 2 and e["fiabilite"] >= reglages["fiabilite_min_non_recoupe"]),
            key=lambda e: (-e["fiabilite"], -e["ts"]),
        )
        par_rubrique[cle] = (
            recoupes[: reglages["max_sujets_par_rubrique"]],
            seuls[: reglages["max_non_recoupes"]],
        )
    return par_rubrique


# ── Rendu HTML ──────────────────────────────────────────────────────

def esc(s):
    return html.escape(s or "")


def couper(texte, n=320):
    return texte if len(texte) <= n else texte[:n].rsplit(" ", 1)[0] + "…"


def carte(ev, non_recoupe=False):
    pct = int(ev["fiabilite"] * 10)
    camps = "".join(
        f'<span class="camp camp-{c}">{lbl}</span>'
        for c, lbl in (("G", "gauche"), ("C", "centre"), ("D", "droite")) if c in ev["camps"]
    )
    badges = ""
    if ev["agences"]:
        badges += f'<span class="badge">dépêche {esc(", ".join(ev["agences"]))}</span>'
    if ev["officiel"]:
        badges += '<span class="badge">source officielle</span>'
    if non_recoupe:
        badges += '<span class="badge badge-warn">non recoupé</span>'
    liens = " · ".join(
        f'<a href="{esc(a["lien"])}" target="_blank" rel="noopener">{esc(a["media"])}'
        f'<span class="note">{a["source"]["fiabilite"]}</span></a>'
        for a in ev["medias"]
    )
    nb = ev["independantes"]
    return f"""
<article class="ev">
  <h3>{esc(ev["titre"])}</h3>
  <div class="meta">
    <span class="jauge" title="Fiabilité moyenne des sources : {ev["fiabilite"]:.1f}/10"><span style="width:{pct}%"></span></span>
    <span>{ev["fiabilite"]:.1f}/10</span>
    <span>· {nb} source{"s" if nb > 1 else ""} indépendante{"s" if nb > 1 else ""}</span>
    {camps}{badges}
  </div>
  {f'<p>{esc(couper(ev["resume"]))}</p>' if ev["resume"] else ""}
  <div class="sources">{liens}</div>
</article>"""


def rendre(par_rubrique, nb_articles, nb_sources, erreurs):
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
            "septembre", "octobre", "novembre", "décembre"]
    now = datetime.now()
    date = f"{jours[now.weekday()].capitalize()} {now.day}{'er' if now.day == 1 else ''} {mois[now.month - 1]} {now.year}"
    total = sum(len(r) + len(s) for r, s in par_rubrique.values())

    sections = ""
    for cle, nom in RUBRIQUES:
        recoupes, seuls = par_rubrique[cle]
        if not recoupes and not seuls:
            continue
        sections += f'<section><h2>{nom}</h2>'
        sections += "".join(carte(e) for e in recoupes)
        if seuls:
            sections += '<h4>Une seule source, mais fiable</h4>'
            sections += "".join(carte(e, non_recoupe=True) for e in seuls)
        sections += "</section>"

    err = ""
    if erreurs:
        err = "<p>Sources indisponibles aujourd'hui : " + ", ".join(esc(n) for n, _ in erreurs) + "</p>"

    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Veille">
<meta name="theme-color" content="#faf8f4" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#16150f" media="(prefers-color-scheme: dark)">
<title>Veille du {now:%d/%m/%Y}</title>
<style>
:root {{ --bg:#faf8f4; --fg:#1d1b18; --muted:#6b665e; --line:#e4dfd5; --card:#fff;
  --accent:#2f5d50; --warn:#a0522d; --G:#b4413a; --C:#6b665e; --D:#2b5c9e; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#16150f; --fg:#ece8df; --muted:#9a948a;
  --line:#2c2a24; --card:#1f1d18; --accent:#7fb8a4; --warn:#e09a6b; --G:#e0837c; --C:#9a948a; --D:#82aee8; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--fg);
  font:16px/1.55 Charter, "Iowan Old Style", Georgia, serif; }}
main {{ max-width:720px; margin:0 auto; padding:32px 16px 64px }}
header h1 {{ font-size:28px; margin:0 }}
header p {{ color:var(--muted); margin:4px 0 0; font-family:-apple-system, system-ui, sans-serif; font-size:14px }}
h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.12em; color:var(--accent);
  border-bottom:1px solid var(--line); padding-bottom:6px; margin:40px 0 8px;
  font-family:-apple-system, system-ui, sans-serif }}
h4 {{ font:600 12px -apple-system, system-ui, sans-serif; color:var(--muted); margin:24px 0 4px }}
.ev {{ padding:16px 0; border-bottom:1px solid var(--line) }}
.ev h3 {{ font-size:19px; line-height:1.3; margin:0 0 6px }}
.ev p {{ margin:8px 0; }}
.meta, .sources {{ font:13px -apple-system, system-ui, sans-serif; color:var(--muted);
  display:flex; flex-wrap:wrap; gap:6px; align-items:center }}
.jauge {{ width:70px; height:6px; background:var(--line); border-radius:3px; overflow:hidden }}
.jauge span {{ display:block; height:100%; background:var(--accent) }}
.camp {{ padding:1px 7px; border-radius:9px; border:1px solid currentColor; font-size:11px }}
.camp-G {{ color:var(--G) }} .camp-C {{ color:var(--C) }} .camp-D {{ color:var(--D) }}
.badge {{ padding:1px 7px; border-radius:9px; background:var(--line); font-size:11px }}
.badge-warn {{ color:var(--warn) }}
.sources a {{ color:var(--fg); text-decoration:none; border-bottom:1px solid var(--line) }}
.sources a:hover {{ border-color:var(--accent) }}
.note {{ font-size:10px; color:var(--muted); margin-left:3px; vertical-align:super }}
footer {{ margin-top:48px; color:var(--muted); font:13px -apple-system, system-ui, sans-serif }}
</style></head><body><main>
<header>
  <h1>{date}</h1>
  <p>{total} sujets, c'est tout. Tirés de {nb_articles} articles publiés par {nb_sources} sources.</p>
</header>
{sections}
<footer>
  <p>Score = sources indépendantes × fiabilité moyenne, avec un bonus si des médias de camps différents
  confirment le même fait. Plusieurs reprises d'une même dépêche d'agence ne comptent que pour une source.
  Les notes de fiabilité se modifient dans <code>sources.yaml</code>.</p>
  {err}
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
    par_rubrique = selectionner(evenements, reglages)

    sortie = ROOT / "digests" / f"{datetime.now():%Y-%m-%d}.html"
    sortie.parent.mkdir(exist_ok=True)
    nb_sources = len(config["sources"]) - len(erreurs)
    sortie.write_text(rendre(par_rubrique, len(articles), nb_sources, erreurs))

    recoupes = sum(1 for e in evenements if e["independantes"] >= 2)
    print(f"{len(articles)} articles · {len(groupes)} groupes · {recoupes} recoupés · "
          f"{len(erreurs)} source(s) en erreur → {sortie}")
    for nom, err in erreurs:
        print(f"  ⚠ {nom} : {err}")
    if "--no-open" not in sys.argv:
        subprocess.run(["open", str(sortie)])


if __name__ == "__main__":
    main()
