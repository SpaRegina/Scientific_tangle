# -*- coding: utf-8 -*-
"""
Построение графа знаний из папки с документами.
Запуск: python -m kg.build --src "Источники информации" --db data/kg.db [--limit 200] [--llm]
"""
import argparse
import logging
import os
import time
from collections import Counter, defaultdict

from .extract import (
    extract_page,
    facilities_from_filename,
    find_experiment_markers,
    find_facilities,
    find_labs,
    find_persons,
    guess_year,
    persons_from_filename,
)
from .graphdb import GraphDB
from .ingest import EXTRACTORS, extract
from .ontology import DOMESTIC_LOCATIONS

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("kg.build")


INTERNAL_EXPERIMENT_CATEGORIES = {"Доклады", "Материалы конференций", "Статьи"}


def classify_geography(loc_mentions):
    dom = any(l in DOMESTIC_LOCATIONS for l in loc_mentions)
    frn = any(l not in DOMESTIC_LOCATIONS for l in loc_mentions)
    if dom and frn:
        return "RU+Мир"
    if dom:
        return "RU"
    if frn:
        return "Мир"
    return "н/д"


def category_of(path, src_root):
    rel = os.path.relpath(path, src_root)
    parts = rel.split(os.sep)
    return parts[0] if len(parts) > 1 else "Прочее"


def iter_files(src, limit=None):
    """Репрезентативная выборка: чередуем категории, мелкие файлы раньше."""
    by_cat = defaultdict(list)
    for root, _, files in os.walk(src):
        for fname in files:
            path = os.path.join(root, fname)
            if os.path.splitext(fname)[1].lower() in EXTRACTORS:
                by_cat[category_of(path, src)].append(path)
    for cat in by_cat:
        by_cat[cat].sort(key=lambda p: os.path.getsize(p))
    out = []
    while any(by_cat.values()):
        for cat in list(by_cat):
            if by_cat[cat]:
                out.append(by_cat[cat].pop(0))
    return out[:limit] if limit else out


def _canonical_facility(raw):
    value = (raw or "").strip()
    if not value:
        return None
    value = " ".join(value.split())
    return value[:120]


def _experiment_name(title, markers, category, n_params):
    if markers:
        lead = sorted(markers)[0]
        return f"Эксперимент: {title[:96]} ({lead})"
    if category in INTERNAL_EXPERIMENT_CATEGORIES and n_params >= 3:
        return f"Эксперимент: {title[:110]}"
    return None


def _top_entities(entity_counts, etype, limit=3):
    items = [(canon, cnt) for (canon, kind), cnt in entity_counts.items() if kind == etype]
    items.sort(key=lambda x: (-x[1], x[0]))
    return [canon for canon, _ in items[:limit]]


def process_file(db, path, src_root, use_llm=False, max_pages=120, rel=None, category=None):
    rel = rel or os.path.relpath(path, src_root)
    if db.con.execute("SELECT 1 FROM documents WHERE path=?", (rel,)).fetchone():
        return False
    pages = extract(path)
    if not pages:
        return False

    title = os.path.splitext(os.path.basename(rel.replace("::", os.sep)))[0]
    category = category or category_of(path, src_root)
    year = guess_year(rel, pages[0][1] if pages else "")
    doc_id, is_new = db.add_document(rel, title, category, year, "н/д", len(pages))
    if not is_new:
        return False

    all_locs = set()
    all_facilities = set(facilities_from_filename(path))
    all_experiment_markers = set()
    persons, labs = set(), set()
    entity_counts = Counter()
    total_params = 0

    fp, fl = persons_from_filename(path)
    persons |= fp
    labs |= fl

    for page_no, text in pages[:max_pages]:
        db.add_chunk(doc_id, page_no, text[:20000])
        entities, relations, params = extract_page(text)
        entity_counts.update(entities)
        total_params += len(params)
        all_experiment_markers |= find_experiment_markers(text)
        all_facilities |= find_facilities(text)

        for (canon, etype), cnt in entities.items():
            nid = db.node_id(canon, etype)
            db.add_mention(nid, doc_id, cnt)
            if etype == "Location":
                all_locs.add(canon)
        for (c1, t1, c2, t2, rel_name), snippets in relations.items():
            db.add_edge(db.node_id(c1, t1), db.node_id(c2, t2), rel_name, doc_id, page_no, snippets)
        for canon_ent, raw, qual, val, val2, unit, snippet in params:
            row = db.get_node(canon_ent)
            if row:
                db.add_param(row["id"], doc_id, page_no, raw, qual, val, val2, unit, snippet)
        if page_no <= 3:
            persons |= find_persons(text)
            labs |= find_labs(text)

    pub_id = db.node_id(title[:120], "Publication")
    db.add_mention(pub_id, doc_id, 1)

    for person in list(persons)[:12]:
        eid = db.node_id(person, "Expert")
        db.add_mention(eid, doc_id, 1)
        db.add_edge(eid, pub_id, "author_of", doc_id, 1, [], status="auto")
        for lab in labs:
            db.add_edge(eid, db.node_id(lab, "Lab"), "member_of", doc_id, 1, [], status="auto")
    for lab in labs:
        lid = db.node_id(lab, "Lab")
        db.add_mention(lid, doc_id, 1)

    experiment_name = _experiment_name(title, all_experiment_markers, category, total_params)
    exp_id = None
    if experiment_name:
        exp_id = db.node_id(experiment_name, "Experiment")
        db.add_mention(exp_id, doc_id, 1)
        db.add_edge(exp_id, pub_id, "described_in", doc_id, 1, [title[:300]], status="auto")
        db.add_edge(pub_id, exp_id, "validated_by", doc_id, 1, [title[:300]], status="auto")
        for canon in _top_entities(entity_counts, "Material"):
            db.add_edge(exp_id, db.node_id(canon, "Material"), "uses_material", doc_id, 1, [], status="auto")
        for canon in _top_entities(entity_counts, "Process"):
            db.add_edge(exp_id, db.node_id(canon, "Process"), "studies_process", doc_id, 1, [], status="auto")
        for canon in _top_entities(entity_counts, "Equipment"):
            db.add_edge(exp_id, db.node_id(canon, "Equipment"), "performed_in", doc_id, 1, [], status="auto")
        for canon in _top_entities(entity_counts, "Property"):
            db.add_edge(exp_id, db.node_id(canon, "Property"), "produces_output", doc_id, 1, [], status="auto")

    for raw_facility in sorted(all_facilities)[:10]:
        facility = _canonical_facility(raw_facility)
        if not facility:
            continue
        fid = db.node_id(facility, "Facility")
        db.add_mention(fid, doc_id, 1)
        db.add_edge(fid, pub_id, "described_in", doc_id, 1, [facility], status="auto")
        if exp_id:
            db.add_edge(exp_id, fid, "performed_in", doc_id, 1, [facility], status="auto")
        for loc in sorted(all_locs)[:3]:
            db.add_edge(fid, db.node_id(loc, "Location"), "located_in", doc_id, 1, [facility], status="auto")

    geo = classify_geography(all_locs)
    db.con.execute("UPDATE documents SET geography=? WHERE id=?", (geo, doc_id))

    if use_llm:
        try:
            from .llm import enrich_document

            enrich_document(db, doc_id, pages)
        except Exception as exc:
            log.warning("LLM-обогащение пропущено (%s): %s", title, exc)

    db.commit()
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="папка с документами")
    ap.add_argument("--db", default="data/kg.db")
    ap.add_argument("--limit", type=int, default=None, help="максимум документов")
    ap.add_argument("--max-size-mb", type=float, default=60, help="пропускать файлы крупнее")
    ap.add_argument("--llm", action="store_true", help="обогащение через LLM (routerai.ru)")
    args = ap.parse_args()

    db = GraphDB(args.db)
    files = iter_files(args.src, args.limit)
    start = time.time()
    ok = 0
    for i, path in enumerate(files):
        if os.path.getsize(path) > args.max_size_mb * 1024 * 1024:
            continue
        try:
            if process_file(db, path, args.src, use_llm=args.llm):
                ok += 1
                log.info("[%d/%d] + %s", i + 1, len(files), os.path.basename(path)[:80])
        except KeyboardInterrupt:
            break
        except Exception as exc:
            log.warning("[%d/%d] ! %s: %s", i + 1, len(files), os.path.basename(path)[:60], exc)
    db.commit()
    log.info("Готово: %d документов за %.1f с", ok, time.time() - start)


if __name__ == "__main__":
    main()
