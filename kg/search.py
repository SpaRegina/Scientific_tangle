# -*- coding: utf-8 -*-
"""
Гибридный поисково-аналитический движок:
NL-запрос -> (LLM или правила) -> термины/фильтры -> FTS + обход графа -> синтез ответа.
"""
import json
import re
from collections import defaultdict

from . import llm
from .extract import find_entities, find_parameters
from .ontology import TERMS

CURRENT_YEAR = 2026


def _syn_words(canon):
    """Слова для FTS-расширения запроса."""
    words = {canon.lower()}
    for syn in TERMS.get(canon, (None, []))[1]:
        syn = re.sub(r"\[.*?\]|[?*+()]", "", syn).strip()
        if len(syn) >= 3:
            words.add(syn.lower())
    return words


def parse_query_rules(question):
    """Rule-based разбор запроса."""
    q = question.lower()
    terms = sorted({canon for canon, _etype, _s, _e in find_entities(question)})
    numeric = []
    for raw, qual, val, val2, unit, _pos in find_parameters(question):
        op = {
            "≤": "<=",
            "не более": "<=",
            "до": "<=",
            "<": "<",
            "≥": ">=",
            "не менее": ">=",
            "от": ">=",
            ">": ">",
        }.get(qual, "~")
        numeric.append({"op": op, "value": val, "value2": val2, "unit": unit, "raw": raw})

    geography = None
    if re.search(r"отечествен|росси|в рф", q):
        geography = "RU"
    if re.search(r"зарубеж|миров(ой|ая|ые)? практик|за рубежом|world", q):
        geography = "Мир" if geography is None else None

    year_from = None
    m = re.search(r"последн\w+\s+(\d{1,2})\s+(?:лет|год)", q)
    if m:
        year_from = CURRENT_YEAR - int(m.group(1))
    m = re.search(r"(?:с|после)\s+(20[0-2]\d)", q)
    if m:
        year_from = int(m.group(1))

    keywords = [
        w for w in re.findall(r"[а-яёa-z]{4,}", q) if not any(w.startswith(t[:4].lower()) for t in terms)
    ][:6]
    return {
        "terms": terms,
        "keywords": keywords,
        "geography": geography,
        "year_from": year_from,
        "numeric": numeric,
    }


def parse_query(question):
    if llm.available():
        try:
            parsed = llm.parse_query(question, TERMS.keys())
            parsed.setdefault("numeric", [])
            parsed.setdefault("keywords", [])
            parsed["terms"] = [t for t in parsed.get("terms", []) if t in TERMS]
            parsed["_engine"] = "llm"
            return parsed
        except Exception:
            pass
    parsed = parse_query_rules(question)
    parsed["_engine"] = "rules"
    return parsed


def _fts_expr(parsed):
    parts = []
    for term in parsed["terms"]:
        words = []
        for word in _syn_words(term):
            word = word.replace('"', "")
            if " " in word:
                words.append(f'"{word}"')
            else:
                words.append(f'"{word}"*' if re.search(r"[а-яё]$", word) else f'"{word}"')
        parts.append("(" + " OR ".join(words) + ")")
    kw = [f'"{k.replace(chr(34), "")}"*' for k in parsed.get("keywords", [])[:5]]
    if parts and kw:
        return " AND ".join(parts) + " OR (" + " AND ".join(parts + ["(" + " OR ".join(kw) + ")"]) + ")"
    if parts:
        return " AND ".join(parts)
    if kw:
        return " AND ".join(kw)
    return None


def _filter_docs(rows, parsed):
    out = []
    for row in rows:
        if parsed.get("geography") and parsed["geography"] not in (row["geography"] or ""):
            if not (parsed["geography"] == "RU" and "RU" in (row["geography"] or "")):
                continue
        if parsed.get("year_from") and row["year"] and row["year"] < parsed["year_from"]:
            continue
        out.append(row)
    return out


def _norm_unit(unit):
    return re.sub(r"\s+", "", (unit or "").lower()).replace("dm3", "дм3")


def _value_matches(op, left, right, query_value, query_value2):
    low = min(left, right) if right is not None else left
    high = max(left, right) if right is not None else left
    if op == "<":
        return low < query_value
    if op == "<=":
        return low <= query_value or high <= query_value
    if op == ">":
        return high > query_value
    if op == ">=":
        return high >= query_value or low >= query_value
    if query_value2 is not None:
        qlow, qhigh = min(query_value, query_value2), max(query_value, query_value2)
        return not (high < qlow or low > qhigh)
    return low <= query_value <= high


def _match_params(db, parsed, node_canons):
    """Параметры графа, удовлетворяющие числовым ограничениям запроса."""
    hits = []
    seen = set()
    for canon in node_canons:
        node = db.get_node(canon)
        if not node:
            continue
        numeric = parsed.get("numeric", [])
        for p in db.node_params(node["id"]):
            if numeric:
                ok = False
                for constraint in numeric:
                    if constraint.get("unit") and _norm_unit(constraint["unit"]) != _norm_unit(p["unit"]):
                        continue
                    if _value_matches(
                        constraint.get("op", "~"),
                        p["val"],
                        p["val2"],
                        constraint["value"],
                        constraint.get("value2"),
                    ):
                        ok = True
                        break
                if not ok:
                    continue
            key = (canon, p["raw"], p["title"])
            if key in seen:
                continue
            seen.add(key)
            hits.append(
                {
                    "entity": canon,
                    "raw": p["raw"],
                    "unit": p["unit"],
                    "val": p["val"],
                    "val2": p["val2"],
                    "doc": p["title"],
                    "doc_id": p["doc_id"],
                    "year": p["year"],
                    "snippet": p["snippet"][:250],
                }
            )
        if len(hits) > 25:
            break
    return hits[:25]


def subgraph(db, canons, depth_limit=18):
    """Подграф вокруг терминов запроса для визуализации."""
    nodes, edges, seen = [], [], {}

    def add_node(node_id, canon, etype, root=False):
        if node_id not in seen:
            seen[node_id] = True
            nodes.append({"id": node_id, "label": canon, "type": etype, "root": root})

    for canon in canons:
        node = db.get_node(canon)
        if not node:
            continue
        add_node(node["id"], node["canon"], node["type"], root=True)
        for nb in db.neighbors(node["id"], limit=depth_limit):
            add_node(nb["id"], nb["canon"], nb["type"])
            edge = {"from": node["id"], "to": nb["id"], "rel": nb["rel"], "weight": nb["weight"]}
            if nb["dir"] == "in":
                edge["from"], edge["to"] = nb["id"], node["id"]
            edges.append(edge)

    roots = [n for n in nodes if n["root"]]
    for i in range(len(roots)):
        for j in range(i + 1, len(roots)):
            for edge in db.edge_between(roots[i]["id"], roots[j]["id"]):
                edges.append(
                    {
                        "from": edge["src"],
                        "to": edge["dst"],
                        "rel": edge["rel"],
                        "weight": edge["weight"],
                        "status": edge["status"],
                        "evidence": json.loads(edge["evidence"] or "[]"),
                    }
                )
    return {"nodes": nodes, "edges": edges}


def find_gaps(db, canons):
    gaps = []
    for i in range(len(canons)):
        for j in range(i + 1, len(canons)):
            n1, n2 = db.get_node(canons[i]), db.get_node(canons[j])
            if n1 and n2 and not db.edge_between(n1["id"], n2["id"]):
                gaps.append(
                    f"Нет данных, связывающих «{canons[i]}» и «{canons[j]}» - потенциальный пробел в базе знаний"
                )
    return gaps


def find_experts(db, doc_ids, limit=8):
    if not doc_ids:
        return []
    qmarks = ",".join("?" * len(doc_ids))
    rows = db.con.execute(
        f"""
        SELECT n.canon, n.type, COUNT(DISTINCT m.doc_id) AS docs
        FROM mentions m JOIN nodes n ON n.id = m.node_id
        WHERE m.doc_id IN ({qmarks}) AND n.type IN ('Expert','Lab')
        GROUP BY n.id ORDER BY docs DESC LIMIT ?
        """,
        (*doc_ids, limit),
    ).fetchall()
    return [{"name": r["canon"], "type": r["type"], "docs": r["docs"]} for r in rows]


def extractive_summary(question, contexts, parsed, gaps, param_hits):
    """Fallback-синтез без LLM."""

    def clean(text):
        text = re.sub(r"</?mark>", "", text)
        return re.sub(r"\s+", " ", text).strip(" …")

    by_doc = defaultdict(list)
    for ctx in contexts:
        by_doc[(ctx["title"], ctx.get("year"), ctx.get("geography"), ctx.get("category"))].append(ctx)
    docs = sorted(by_doc.items(), key=lambda kv: -(kv[0][1] or 0))
    n_ru = sum(1 for (_t, _y, g, _cat), _ in docs if "RU" in (g or ""))
    n_world = sum(1 for (_t, _y, g, _cat), _ in docs if "Мир" in (g or ""))
    years = [y for (_t, y, _g, _cat), _ in docs if y]

    lines = [
        f"КРАТКИЙ ВЫВОД. По запросу найдено {len(by_doc)} источников ({len(contexts)} фрагментов); "
        f"термины: {', '.join(parsed['terms']) or '-'}. "
        f"География: отечественная практика - {n_ru} ист., мировая - {n_world} ист."
        + (f" Диапазон лет: {min(years)}-{max(years)}." if years else ""),
        "",
        "НАЙДЕННЫЕ ИСТОЧНИКИ И ФАКТЫ:",
    ]
    if len(docs) > 10:
        lines.append(f"(показаны первые 10 из {len(docs)}; полный список - в колонке «Источники»)")
    for i, ((title, year, geo, cat), chunks) in enumerate(docs[:10], 1):
        frag = clean(chunks[0]["text"])[:350]
        lines.append(f"[{i}] {title} - {cat or ''}, {year or 'год н/д'}, {geo or 'н/д'}")
        lines.append(f'     "{frag}..."')

    if param_hits:
        lines.extend(["", "ЧИСЛОВЫЕ РЕЖИМЫ И ПАРАМЕТРЫ:"])
        for hit in param_hits[:8]:
            lines.append(f"• {hit['entity']}: {hit['raw']} - {hit['doc']} ({hit['year'] or 'н/д'})")

    if gaps:
        lines.extend(["", "ПРОБЕЛЫ В ДАННЫХ:"])
        lines.extend("⚠ " + gap for gap in gaps[:6])

    if llm.available():
        lines.extend(
            [
                "",
                "Сводка собрана rule-based fallback, потому что LLM-синтез не сработал. Для стабильной демо-работы внешний LLM отключен по умолчанию.",
            ]
        )
    return "\n".join(lines)


def answer(db, question):
    parsed = parse_query(question)
    expr = _fts_expr(parsed)
    contexts, doc_ids = [], []

    if expr:
        try:
            rows = _filter_docs(db.fts(expr, limit=16), parsed)
        except Exception:
            rows = []
        if not rows and parsed["terms"]:
            try:
                loose = " OR ".join(
                    "(" + " OR ".join(f'"{w}"*' for w in _syn_words(term)) + ")" for term in parsed["terms"]
                )
                rows = _filter_docs(db.fts(loose, limit=16), parsed)
            except Exception:
                rows = []
        for row in rows:
            contexts.append(
                {
                    "title": row["title"],
                    "path": row["path"],
                    "page": row["page"],
                    "year": row["year"],
                    "geography": row["geography"],
                    "category": row["category"],
                    "text": row["snip"],
                    "doc_id": row["doc_id"],
                }
            )
            if row["doc_id"] not in doc_ids:
                doc_ids.append(row["doc_id"])

    param_hits = _match_params(db, parsed, parsed["terms"])
    if parsed.get("numeric") and param_hits:
        matched_doc_ids = {p["doc_id"] for p in param_hits if p.get("doc_id")}
        contexts = [c for c in contexts if c["doc_id"] in matched_doc_ids][:12] or contexts[:12]
        doc_ids = [doc_id for doc_id in doc_ids if doc_id in matched_doc_ids] or doc_ids[:10]
    else:
        contexts = contexts[:12]
        doc_ids = doc_ids[:10]

    graph = subgraph(db, parsed["terms"])
    gaps = find_gaps(db, parsed["terms"])
    experts = find_experts(db, doc_ids)

    engine = parsed.pop("_engine", "rules")
    if llm.available() and contexts:
        try:
            text = llm.synthesize_answer(question, contexts)
            engine += "+llm-synthesis"
        except Exception:
            text = extractive_summary(question, contexts, parsed, gaps, param_hits)
    else:
        text = extractive_summary(question, contexts, parsed, gaps, param_hits)

    return {
        "question": question,
        "parsed": parsed,
        "engine": engine,
        "answer": text,
        "llm_error": llm.LAST_ERROR if llm.available() else None,
        "sources": contexts[:20],
        "graph": graph,
        "gaps": gaps,
        "experts": experts,
        "params": param_hits,
    }


def gap_matrix(db, type1="Material", type2="Process", top=12):
    """Матрица покрытия: сколько источников связывают пары сущностей."""
    t1 = db.con.execute(
        "SELECT n.id, n.canon FROM nodes n JOIN mentions m ON m.node_id=n.id WHERE n.type=? "
        "GROUP BY n.id ORDER BY SUM(m.cnt) DESC LIMIT ?",
        (type1, top),
    ).fetchall()
    t2 = db.con.execute(
        "SELECT n.id, n.canon FROM nodes n JOIN mentions m ON m.node_id=n.id WHERE n.type=? "
        "GROUP BY n.id ORDER BY SUM(m.cnt) DESC LIMIT ?",
        (type2, top),
    ).fetchall()
    matrix = []
    for r1 in t1:
        row = []
        for r2 in t2:
            edges = db.edge_between(r1["id"], r2["id"])
            row.append(sum(edge["weight"] for edge in edges) if edges else 0)
        matrix.append(row)
    return {"rows": [r["canon"] for r in t1], "cols": [r["canon"] for r in t2], "matrix": matrix}
