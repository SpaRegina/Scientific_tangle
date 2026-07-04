# -*- coding: utf-8 -*-
"""
Извлечение сущностей, числовых параметров и связей из текста.
Подход: словарная разметка (ru/en синонимы) + правила + регулярные выражения.
Опционально дообогащается LLM-модулем (kg.llm).
"""
import os
import re
from collections import defaultdict

from .ontology import TERMS, UNITS, RELATION_TYPES, DEFAULT_RELATION

# ---------- компиляция словаря терминов ----------
_PATTERNS = []  # (canon, type, compiled_regex)


def _compile():
    if _PATTERNS:
        return
    for canon, (etype, syns) in TERMS.items():
        alts_ci, alts_cs = [], []
        for s in syns:
            if re.fullmatch(r"[a-z0-9]{1,2}", s):
                # Химические символы матчим только в исходном регистре.
                alts_cs.append(s.capitalize())
            elif re.search(r"[\[\]()?*+]", s):
                alts_ci.append(s + r"[а-яё]*")
            elif re.search(r"[а-яё]$", s):
                alts_ci.append(re.escape(s) + r"[а-яё]*")
            else:
                alts_ci.append(re.escape(s))
        if alts_ci:
            _PATTERNS.append((
                canon,
                etype,
                re.compile(r"(?<![а-яёa-z0-9])(?:%s)(?![а-яёa-z])" % "|".join(alts_ci), re.I),
            ))
        if alts_cs:
            _PATTERNS.append((
                canon,
                etype,
                re.compile(r"(?<![А-Яа-яёA-Za-z0-9])(?:%s)(?![a-zа-яё])" % "|".join(alts_cs)),
            ))


_UNIT_RE = re.compile(
    r"(?P<qual>≤|≥|<|>|до|от|не более|не менее|около|порядка)?\s*"
    r"(?P<val>\d{1,6}(?:[.,]\d{1,3})?)"
    r"(?:\s*[–—\-…]\s*(?P<val2>\d{1,6}(?:[.,]\d{1,3})?))?"
    r"\s*(?P<unit>%s)" % "|".join("(?:%s)" % u for u in UNITS)
)
_SENT_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n{2,}")

_PERSON_RE = re.compile(
    r"\b([А-ЯЁ][а-яё]{2,})\s+([А-ЯЁ])\.\s?([А-ЯЁ])\.|\b([А-ЯЁ])\.\s?([А-ЯЁ])\.\s+([А-ЯЁ][а-яё]{2,})"
)
_LAB_RE = re.compile(r"\b(ЛГМ|ЛПМ|ИАЦ|ЛИМ|ЦГМ)\b|лаборатори[яи]\s+([а-яё\- ]{5,60}?)(?=[,.;\n)])", re.I)
_YEAR_RE = re.compile(r"\b(19[89]\d|20[0-2]\d)\b")
_FACILITY_RE = re.compile(
    r"\b((?:обогатительн\w*\s+фабрик\w*)|(?:завод\w*)|(?:комбинат\w*)|(?:рудник\w*)|(?:цех\w*)|"
    r"(?:полигон\w*)|(?:участок\w*)|(?:площадк\w*)|(?:фабрик\w*))\b",
    re.I,
)
_EXPERIMENT_RE = re.compile(
    r"\b((?:эксперимент\w*)|(?:опыт\w*)|(?:испытани\w*)|(?:серия опытов)|(?:опытно-промышленн\w*)|"
    r"(?:лабораторн\w+\s+опыт\w*))\b",
    re.I,
)


def find_entities(text):
    """-> list[(canon, type, start, end)]"""
    _compile()
    out = []
    for canon, etype, rx in _PATTERNS:
        for m in rx.finditer(text):
            out.append((canon, etype, m.start(), m.end()))
    return out


def find_parameters(text):
    """-> list[(raw, qualifier, value, value2, unit, start)]"""
    out = []
    for m in _UNIT_RE.finditer(text):
        val = float(m.group("val").replace(",", "."))
        val2 = m.group("val2")
        out.append((
            m.group(0).strip(),
            m.group("qual") or "",
            val,
            float(val2.replace(",", ".")) if val2 else None,
            re.sub(r"\s+", "", m.group("unit")),
            m.start(),
        ))
    return out


def find_persons(text):
    people = set()
    for m in _PERSON_RE.finditer(text):
        if m.group(1):
            people.add(f"{m.group(1)} {m.group(2)}.{m.group(3)}.")
        else:
            people.add(f"{m.group(6)} {m.group(4)}.{m.group(5)}.")
    return people


def find_labs(text):
    labs = set()
    for m in _LAB_RE.finditer(text):
        labs.add((m.group(1) or ("Лаборатория " + m.group(2).strip())).strip())
    return labs


def find_facilities(text):
    facilities = set()
    for m in _FACILITY_RE.finditer(text):
        raw = m.group(1).strip(" ,.;:-")
        if len(raw) >= 3:
            facilities.add(raw)
    return facilities


def find_experiment_markers(text):
    markers = set()
    for m in _EXPERIMENT_RE.finditer(text):
        raw = m.group(1).strip(" ,.;:-")
        if len(raw) >= 3:
            markers.add(raw)
    return markers


def persons_from_filename(fname):
    base = os.path.splitext(os.path.basename(fname))[0]
    return find_persons(base), find_labs(base)


def facilities_from_filename(fname):
    base = os.path.splitext(os.path.basename(fname))[0]
    return find_facilities(base)


def guess_year(path, first_page_text=""):
    m = re.search(r"[\\/](19[89]\d|20[0-2]\d)(?:-[^\\/]*)?[\\/]", path)
    if m:
        return int(m.group(1))
    years = [int(y) for y in _YEAR_RE.findall(os.path.basename(path))]
    if years:
        return max(years)
    years = [int(y) for y in _YEAR_RE.findall(first_page_text[:3000])]
    return max(years) if years else None


def relation_type(t1, t2):
    return RELATION_TYPES.get((t1, t2)) or RELATION_TYPES.get((t2, t1)) or DEFAULT_RELATION


def extract_page(text):
    """
    Обработка страницы по предложениям.
    -> entities: {(canon,type): count}, relations: {(c1,t1,c2,t2,rel): [snippet]},
       params: [(entity_canon, raw, qual, val, val2, unit, snippet)]
    """
    import bisect

    entities = defaultdict(int)
    relations = defaultdict(list)
    params = []

    all_ents = sorted(find_entities(text), key=lambda x: x[2])
    all_params = find_parameters(text)
    bounds, pos = [], 0
    for m in _SENT_SPLIT.finditer(text):
        bounds.append((pos, m.start()))
        pos = m.end()
    bounds.append((pos, len(text)))
    starts = [b[0] for b in bounds]
    ent_by_sent = defaultdict(list)
    for canon, etype, s, e in all_ents:
        ent_by_sent[bisect.bisect_right(starts, s) - 1].append((canon, etype, s, e))
    par_by_sent = defaultdict(list)
    for p in all_params:
        par_by_sent[bisect.bisect_right(starts, p[5]) - 1].append(p)

    for si, (b0, b1) in enumerate(bounds):
        sent = text[b0:b1]
        if len(sent) < 15 or len(sent) > 1500 or (si not in ent_by_sent and si not in par_by_sent):
            continue
        seen = {}
        for canon, etype, s, e in ent_by_sent.get(si, []):
            entities[(canon, etype)] += 1
            seen.setdefault((canon, etype), s - b0)
        uniq = sorted(seen.items(), key=lambda kv: kv[1])
        snippet = sent.strip()[:400]

        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                (c1, t1), (c2, t2) = uniq[i][0], uniq[j][0]
                if c1 == c2:
                    continue
                rel = relation_type(t1, t2)
                key = (
                    (c1, t1, c2, t2, rel)
                    if (t1, t2) in RELATION_TYPES or rel == DEFAULT_RELATION
                    else (c2, t2, c1, t1, rel)
                )
                if len(relations[key]) < 5:
                    relations[key].append(snippet)

        for raw, qual, val, val2, unit, ppos in par_by_sent.get(si, []):
            best, bestd = None, 10 ** 9
            for (canon, etype), s in seen.items():
                d = abs((ppos - b0) - s)
                if d < bestd:
                    best, bestd = canon, d
            if best:
                params.append((best, raw, qual, val, val2, unit, snippet))
    return entities, relations, params
