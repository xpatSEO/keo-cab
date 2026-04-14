#!/usr/bin/env python3
"""
Réinjecte les balises <span> dans les 6 titres ACF de
pages_47_villes_masonry.json, en s'alignant sur le modèle
pages_cabinet_villes_masonry_V0.json.

Les 6 champs concernés :
  - header-breadcrumb.kb_header_title
  - intro-image.kb_intro_image_title
  - avantages-style2.kb_avantages_style2_title
  - carousel-metier.kb_carousel_metier_title
  - offers-new-block.kb_offers_title
  - list-simulators.kb_list_simulators_title

Pré-condition vérifiée : pour chaque ville et chaque bloc, la valeur
actuelle de V47 est exactement V0.strip(<span>). La réinjection consiste
donc à recopier telle quelle la valeur V0 dans V47.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
V0_PATH = ROOT / "pages_cabinet_villes_masonry_V0.json"
V47_PATH = ROOT / "pages_47_villes_masonry.json"

TITLE_BY_BLOCK = {
    "header-breadcrumb": "kb_header_title",
    "intro-image": "kb_intro_image_title",
    "avantages-style2": "kb_avantages_style2_title",
    "carousel-metier": "kb_carousel_metier_title",
    "offers-new-block": "kb_offers_title",
    "list-simulators": "kb_list_simulators_title",
}
BLOCK_RE = {
    b: re.compile(rf"(<!--\s*wp:acf/{re.escape(b)}\s+)(\{{.*?\}})(\s*/?-->)", re.DOTALL)
    for b in TITLE_BY_BLOCK
}


def get_title(html: str, block: str, field: str) -> str | None:
    m = BLOCK_RE[block].search(html)
    if not m:
        return None
    try:
        obj = json.loads(m.group(2))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj.get("data", {}).get(field)


def set_title(html: str, block: str, field: str, value: str) -> str:
    """Rewrite the ACF block's JSON payload in-place with value on `field`."""
    m = BLOCK_RE[block].search(html)
    if not m:
        return html
    obj = json.loads(m.group(2))
    if not isinstance(obj, dict):
        return html
    obj.setdefault("data", {})[field] = value
    new_block = m.group(1) + json.dumps(obj, ensure_ascii=False) + m.group(3)
    return html[: m.start()] + new_block + html[m.end() :]


def main() -> int:
    with open(V0_PATH) as f:
        v0 = json.load(f)
    with open(V47_PATH) as f:
        v47 = json.load(f)

    v0_by_ville = {e["ville"]: e for e in v0}

    n_patched = 0
    n_missing = 0
    n_skipped_equal = 0
    for entry in v47:
        ville = entry["ville"]
        if ville not in v0_by_ville:
            print(f"[WARN] {ville}: absent de V0", file=sys.stderr)
            continue
        html = entry["post_data"]["post_content"]
        v0_html = v0_by_ville[ville]["post_data"]["post_content"]
        for block, field in TITLE_BY_BLOCK.items():
            v0_title = get_title(v0_html, block, field)
            v47_title = get_title(html, block, field)
            if v0_title is None:
                n_missing += 1
                print(f"[WARN] {ville}/{block}: titre absent de V0", file=sys.stderr)
                continue
            if v47_title == v0_title:
                n_skipped_equal += 1
                continue
            html = set_title(html, block, field, v0_title)
            n_patched += 1
        entry["post_data"]["post_content"] = html

    with open(V47_PATH, "w", encoding="utf-8") as f:
        json.dump(v47, f, ensure_ascii=False, indent=2)

    print(f"[OK] {n_patched} champs titre patchés, {n_skipped_equal} déjà alignés, {n_missing} manquants côté V0")
    print(f"     Fichier mis à jour : {V47_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
