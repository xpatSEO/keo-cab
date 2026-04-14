#!/usr/bin/env python3
"""
Régénère proprement les 3 articles masonry pour chaque ville du fichier
input-47-villes.csv, puis réinjecte le résultat dans pages_47_villes_masonry.json.

Objectif : faire correspondre la structure des 25 villes "originales" :
  - 1 titre court en texte brut (~40-90 car.), sans " ; ", sans balise HTML
  - 1 corps en texte brut (~300-2500 car.), sans markdown, sans balise HTML

Usage :
  export ANTHROPIC_API_KEY=sk-ant-...
  python3 generate_masonry.py             # génère dans clean/
  python3 generate_masonry.py --apply     # régénère + applique au JSON final
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

import anthropic

ROOT = Path(__file__).parent
CSV_IN = ROOT / "input-47-villes.csv"
JSON_IN = ROOT / "pages_47_villes_masonry.json"
CLEAN_DIR = ROOT / "clean"
CSV_OUT = CLEAN_DIR / "input-47-villes.clean.csv"
JSON_OUT = CLEAN_DIR / "per-ville"  # un fichier json par ville (cache)
MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """Tu es un rédacteur SEO spécialisé en expertise comptable pour le cabinet Keobiz.

Ta mission : reformater un brouillon de contenu en 3 articles propres pour un bloc masonry WordPress. Tu dois produire une structure éditoriale cohérente, claire et fidèle au ton du cabinet.

RÈGLES STRICTES DE FORMAT (non négociables) :
1. Tu produis exactement 3 articles.
2. Chaque article a un unique champ "title" (texte brut, 40-90 caractères, SANS point-virgule " ; ", SANS balise HTML, SANS markdown).
3. Chaque article a un unique champ "body" (texte brut, 300-2500 caractères, SANS balise HTML, SANS markdown — pas de #, pas de **, pas de <h3>, pas de <strong>, pas de <a>).
4. Le corps est un texte fluide en paragraphes naturels. Les énumérations sont intégrées dans le texte via des phrases ou des deux-points (ex. "Nos services incluent : la tenue comptable, la gestion de la paie, le conseil fiscal.").
5. Zéro auto-référence au format (pas de "voici 3 articles", pas de "# titre").
6. Le ton : professionnel, chaleureux, ancré localement (mentionner la ville fournie).
7. Les titres évitent les doubles questions et ne contiennent pas de " ; ".
8. Tu ne peux pas recopier mot pour mot le brouillon : tu dois le restructurer, condenser, et enlever le pseudo-markdown (les # initiaux, les puces, les " ; " dans les titres).

STRUCTURE ÉDITORIALE CIBLE (inspirée du format Keobiz standard) :
  - Article 0 : présentation des services d'expertise comptable pour la ville (offre globale, 360°, TPE/PME/indépendants).
  - Article 1 : un axe métier concret (gestion comptable et fiscale, ou création d'entreprise, ou conseil stratégique).
  - Article 2 : un autre axe (gestion sociale / paie, ou FAQ pratiques, ou pourquoi choisir Keobiz).

FORMAT DE SORTIE (obligatoire) :
Tu DOIS répondre avec un unique objet JSON valide, sans texte autour, sans balise ```, exactement de la forme :

{"articles":[
  {"title":"...","body":"..."},
  {"title":"...","body":"..."},
  {"title":"...","body":"..."}
]}
"""

USER_TEMPLATE = """VILLE : {ville}

Voici le brouillon brut (3 titres et 3 corps concaténés) à restructurer en 3 articles propres pour cette ville. Le brouillon contient des titres concaténés avec " ; " et du pseudo-markdown qu'il faut éliminer.

--- BROUILLON ---
[Titre 0]
{t0}

[Corps 0]
{b0}

[Titre 1]
{t1}

[Corps 1]
{b1}

[Titre 2]
{t2}

[Corps 2]
{b2}
--- FIN BROUILLON ---

Produis maintenant l'objet JSON {{"articles":[...]}} conforme aux règles strictes."""


def validate(obj: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(obj, dict) or "articles" not in obj:
        return ["missing 'articles' key"]
    arts = obj["articles"]
    if not isinstance(arts, list) or len(arts) != 3:
        return [f"expected 3 articles, got {len(arts) if isinstance(arts, list) else type(arts).__name__}"]
    for i, a in enumerate(arts):
        if not isinstance(a, dict):
            errors.append(f"article {i}: not a dict")
            continue
        title = a.get("title", "")
        body = a.get("body", "")
        if not isinstance(title, str) or not isinstance(body, str):
            errors.append(f"article {i}: title/body not string")
            continue
        if " ; " in title:
            errors.append(f"article {i}: title contains ' ; '")
        if re.search(r"<[a-z]+", title) or re.search(r"<[a-z]+", body):
            errors.append(f"article {i}: contains HTML tag")
        if re.search(r"(?m)^\s*#{1,6}\s", body) or "**" in body:
            errors.append(f"article {i}: contains markdown")
        if len(title) < 30 or len(title) > 120:
            errors.append(f"article {i}: title length {len(title)} out of [30,120]")
        if len(body) < 250 or len(body) > 3500:
            errors.append(f"article {i}: body length {len(body)} out of [250,3500]")
    return errors


def extract_json(text: str) -> dict | None:
    # retire les éventuels backticks
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # trouve le premier objet JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def generate_for_ville(client: anthropic.Anthropic, row: dict, max_retries: int = 3) -> dict:
    ville = row["ville"].strip()
    user = USER_TEMPLATE.format(
        ville=ville,
        t0=row["kb_masonry_articles_0_kb_masonry_title"],
        b0=row["kb_masonry_articles_0_kb_masonry_txt"],
        t1=row["kb_masonry_articles_1_kb_masonry_title"],
        b1=row["kb_masonry_articles_1_kb_masonry_txt"],
        t2=row["kb_masonry_articles_2_kb_masonry_title"],
        b2=row["kb_masonry_articles_2_kb_masonry_txt"],
    )

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
            obj = extract_json(text)
            if obj is None:
                last_err = "could not parse JSON from response"
            else:
                errs = validate(obj)
                if not errs:
                    return obj
                last_err = "validation: " + "; ".join(errs)
            # feedback loop : reprompt avec les erreurs
            user_retry = user + f"\n\nTa réponse précédente a échoué à la validation ({last_err}). Corrige et renvoie uniquement le JSON valide."
            user = user_retry
        except anthropic.APIError as e:
            last_err = f"API error: {e}"
            time.sleep(2 * attempt)
    raise RuntimeError(f"[{ville}] échec après {max_retries} tentatives : {last_err}")


def inject_into_json(all_clean: dict[str, dict]) -> None:
    """Injecte les articles propres dans pages_47_villes_masonry.json."""
    with open(JSON_IN) as f:
        data = json.load(f)

    # normalisation clé : la CSV a "Vitry Sur Seine" mais le JSON a "Vitry-sur-Seine",
    # et le JSON peut avoir des accents absents de la CSV ("Saint-Étienne" vs "Saint Etienne").
    import unicodedata

    def norm(v: str) -> str:
        stripped = "".join(
            c for c in unicodedata.normalize("NFD", v)
            if unicodedata.category(c) != "Mn"
        )
        return re.sub(r"[\s\-]+", "", stripped).lower()

    clean_by_norm = {norm(k): v for k, v in all_clean.items()}

    for entry in data:
        ville = entry["ville"]
        key = norm(ville)
        if key not in clean_by_norm:
            print(f"[WARN] pas de données propres pour {ville}", file=sys.stderr)
            continue
        clean = clean_by_norm[key]
        html = entry["post_data"]["post_content"]
        m = re.search(r"(<!--\s*wp:acf/masonry-blocks\s*)(\{.*?\})(\s*/?-->)", html, flags=re.DOTALL)
        if not m:
            print(f"[WARN] bloc masonry introuvable pour {ville}", file=sys.stderr)
            continue
        block_json = json.loads(m.group(2))
        for i, art in enumerate(clean["articles"]):
            block_json["data"][f"kb_masonry_articles_{i}_kb_masonry_title"] = art["title"]
            block_json["data"][f"kb_masonry_articles_{i}_kb_masonry_txt"] = art["body"]
        new_block = m.group(1) + json.dumps(block_json, ensure_ascii=False) + m.group(3)
        entry["post_data"]["post_content"] = html[: m.start()] + new_block + html[m.end():]

    with open(JSON_IN, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[OK] JSON mis à jour : {JSON_IN}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Injecter les articles propres dans le JSON final")
    parser.add_argument("--limit", type=int, default=None, help="Limite le nombre de villes traitées (debug)")
    parser.add_argument("--ville", type=str, default=None, help="Traite uniquement la ville donnée (debug)")
    parser.add_argument("--resume", action="store_true", help="Saute les villes déjà présentes dans le cache")
    args = parser.parse_args()

    CLEAN_DIR.mkdir(exist_ok=True)
    JSON_OUT.mkdir(parents=True, exist_ok=True)

    with open(CSV_IN, newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))

    if args.ville:
        rows = [r for r in rows if r["ville"].strip().lower() == args.ville.lower()]
    if args.limit:
        rows = rows[: args.limit]

    # client API instancié paresseusement : inutile si tout est en cache
    client: anthropic.Anthropic | None = None

    all_clean: dict[str, dict] = {}
    for i, row in enumerate(rows, 1):
        ville = row["ville"].strip()
        cache_path = JSON_OUT / f"{re.sub(r'[^a-zA-Z0-9]', '_', ville)}.json"
        if args.resume and cache_path.exists():
            with open(cache_path) as f:
                all_clean[ville] = json.load(f)
            print(f"[{i}/{len(rows)}] {ville}: cache hit")
            continue
        print(f"[{i}/{len(rows)}] {ville}: génération...", flush=True)
        if client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                print("ERREUR: ANTHROPIC_API_KEY non définie (nécessaire pour générer).", file=sys.stderr)
                return 2
            client = anthropic.Anthropic()
        try:
            obj = generate_for_ville(client, row)
        except Exception as e:
            print(f"  ERREUR : {e}", file=sys.stderr)
            continue
        all_clean[ville] = obj
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)

    # écrire CSV propre
    with open(CSV_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writerow([
            "ville",
            "kb_masonry_articles_0_kb_masonry_title",
            "kb_masonry_articles_0_kb_masonry_txt",
            "kb_masonry_articles_1_kb_masonry_title",
            "kb_masonry_articles_1_kb_masonry_txt",
            "kb_masonry_articles_2_kb_masonry_title",
            "kb_masonry_articles_2_kb_masonry_txt",
        ])
        for ville, obj in all_clean.items():
            arts = obj["articles"]
            w.writerow([
                ville,
                arts[0]["title"], arts[0]["body"],
                arts[1]["title"], arts[1]["body"],
                arts[2]["title"], arts[2]["body"],
            ])
    print(f"[OK] CSV propre écrit : {CSV_OUT} ({len(all_clean)} villes)")

    if args.apply:
        inject_into_json(all_clean)

    return 0


if __name__ == "__main__":
    sys.exit(main())
