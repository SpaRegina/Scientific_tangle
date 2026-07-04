# -*- coding: utf-8 -*-
"""
Веб-приложение «Научный клубок» — карта знаний R&D.
Запуск:  uvicorn app.main:app --port 8000
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Query, Request, Body
from fastapi.responses import FileResponse, JSONResponse
from kg.graphdb import GraphDB
from kg import search, llm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("KG_DB", os.path.join(ROOT, "data", "kg.db"))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
# папка с исходными документами (для открытия источников из выдачи)
SRC_DIR = os.environ.get("KG_SRC", os.path.join(os.path.dirname(ROOT), "Источники информации"))

app = FastAPI(title="Научный клубок — карта знаний R&D")

# ---------- Ролевая модель (демо-реализация; в проде — SSO/LDAP) ----------
# исследователь / аналитик — поиск и просмотр; руководитель — + дашборд и аудит;
# администратор — всё; внешний партнёр — без внутренних документов и без аудита.
ROLES = {"researcher": "исследователь", "analyst": "аналитик", "lead": "руководитель проекта",
         "admin": "администратор", "partner": "внешний партнёр"}
INTERNAL_CATEGORIES = {"Статьи", "Доклады", "Обзоры"}  # внутренние отчёты и статьи


def get_role(request: Request) -> str:
    r = request.headers.get("X-Role", "researcher")
    return r if r in ROLES else "researcher"


def db():
    return GraphDB(DB_PATH)


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/stats")
def stats():
    s = db().stats()
    s["llm"] = llm.available()
    s["llm_model"] = llm.MODEL if llm.available() else None
    return s


@app.get("/api/ask")
def ask(request: Request, q: str = Query(..., min_length=3)):
    d = db()
    role = get_role(request)
    try:
        res = search.answer(d, q)
        if role == "partner":  # внешний партнёр не видит внутренние документы
            res["sources"] = [x for x in res["sources"] if x.get("category") not in INTERNAL_CATEGORIES]
            res["experts"] = []
        _audit(d, q, res.get("engine", "?"), len(res.get("sources", [])), role)
        return res
    except Exception as e:
        _audit(d, q, "error", 0, role)
        return JSONResponse({"error": str(e)}, status_code=500)


def _audit(d, query, engine, n_sources, role="researcher"):
    """Аудит действий: логирование запросов с ролью (см. раздел ИБ в ТЗ)."""
    try:
        d.con.execute("CREATE TABLE IF NOT EXISTS audit_log(ts TEXT DEFAULT (datetime('now')), "
                      "query TEXT, engine TEXT, n_sources INTEGER, role TEXT)")
        d.con.execute("INSERT INTO audit_log(query, engine, n_sources, role) VALUES(?,?,?,?)",
                      (query[:500], engine, n_sources, role))
        d.commit()
    except Exception:
        pass


@app.get("/api/audit")
def audit(request: Request, limit: int = 50):
    """Журнал запросов — только руководитель и администратор."""
    if get_role(request) not in ("lead", "admin"):
        return JSONResponse({"error": "недостаточно прав (нужна роль: руководитель или администратор)"}, status_code=403)
    d = db()
    try:
        rows = d.con.execute("SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


@app.get("/api/node")
def node(canon: str, type: str = None):
    d = db()
    n = d.get_node(canon, type)
    if not n:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "canon": n["canon"], "type": n["type"],
        "neighbors": [dict(r) for r in d.neighbors(n["id"])],
        "documents": [dict(r) for r in d.node_docs(n["id"])],
        "params": [dict(r) for r in d.node_params(n["id"])],
        "history": [dict(r) for r in d.con.execute(
            "SELECT fh.ts, fh.action, fh.status, fh.note, e.rel, s.canon AS src, t.canon AS dst "
            "FROM fact_history fh "
            "JOIN edges e ON e.id=fh.edge_id "
            "JOIN nodes s ON s.id=e.src "
            "JOIN nodes t ON t.id=e.dst "
            "WHERE e.src=? OR e.dst=? ORDER BY fh.ts DESC LIMIT 20", (n["id"], n["id"]))],
    }


@app.get("/api/gaps")
def gaps(type1: str = "Material", type2: str = "Process", top: int = 12):
    return search.gap_matrix(db(), type1, type2, top)


@app.get("/api/graph")
def graph(terms: str):
    return search.subgraph(db(), [t.strip() for t in terms.split(",") if t.strip()])


@app.get("/api/file")
def file(request: Request, path: str):
    if get_role(request) == "partner" and path.split("/")[0].split("\\")[0] in INTERNAL_CATEGORIES:
        return JSONResponse({"error": "доступ к внутренним документам ограничен для внешних партнёров"}, status_code=403)
    """Открыть исходный документ (в т.ч. вложенный в zip-архив)."""
    import zipfile, mimetypes
    from fastapi.responses import Response
    rel = path.replace("\\", "/")
    member = None
    if "::" in rel:
        rel, member = rel.split("::", 1)
    base = os.path.realpath(SRC_DIR)
    fpath = os.path.realpath(os.path.join(base, rel))
    if not fpath.startswith(base):
        return JSONResponse({"error": "недопустимый путь"}, status_code=400)
    if not os.path.exists(fpath):  # многотомные архивы: rar -> part1.rar, zip -> zip.001
        for alt in (fpath[:-4] + ".part1.rar", fpath + ".001", fpath[:-4] + ".zip.001"):
            if os.path.exists(alt):
                fpath = alt
                break
        else:
            return JSONResponse({"error": "файл не найден: " + rel}, status_code=404)
    if member and fpath.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(fpath) as zf:
                data = zf.read(member)
            mt = mimetypes.guess_type(member)[0] or "application/octet-stream"
            fn = os.path.basename(member).encode("ascii", "ignore").decode() or "file"
            return Response(data, media_type=mt,
                            headers={"Content-Disposition": f'inline; filename="{fn}"'})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    return FileResponse(fpath, filename=os.path.basename(fpath))


DOMAIN_TERMS = {
    "Гидрометаллургия": ["выщелачивание", "экстракция", "электроэкстракция", "осаждение", "цементация", "автоклавное выщелачивание", "ионный обмен"],
    "Пирометаллургия": ["плавка", "взвешенная плавка", "конвертирование", "обжиг", "восстановление", "штейн", "шлак"],
    "Обогащение": ["флотация", "обогащение", "измельчение", "концентрат", "крупность"],
    "Экология и вода": ["очистка воды", "очистка газов", "обессоливание", "SO2", "сточные воды", "шахтная вода", "хвосты"],
    "Переработка отходов": ["закладка", "гипс", "угольные отходы", "брикетирование", "гранулирование"],
}


@app.get("/api/dashboard")
def dashboard(request: Request):
    """Дашборд руководителя: покрытие по направлениям, зоны риска, активность."""
    if get_role(request) not in ("lead", "admin"):
        return JSONResponse({"error": "недостаточно прав (нужна роль: руководитель или администратор)"}, status_code=403)
    d = db()
    domains = []
    for name, terms in DOMAIN_TERMS.items():
        qm = ",".join("?" * len(terms))
        row = d.con.execute(
            f"SELECT COUNT(DISTINCT m.doc_id) c, COALESCE(SUM(m.cnt),0) mm FROM mentions m "
            f"JOIN nodes n ON n.id=m.node_id WHERE n.canon IN ({qm})", terms).fetchone()
        domains.append({"domain": name, "docs": row["c"], "mentions": row["mm"]})
    # зоны риска: тематические сущности с малым числом источников
    risk = [dict(r) for r in d.con.execute(
        "SELECT n.canon, n.type, COUNT(DISTINCT m.doc_id) docs FROM nodes n "
        "JOIN mentions m ON m.node_id=n.id WHERE n.type IN ('Process','Material','Equipment') "
        "GROUP BY n.id ORDER BY docs, n.canon LIMIT 15")]
    by_cat = [dict(r) for r in d.con.execute(
        "SELECT category, COUNT(*) docs FROM documents GROUP BY category ORDER BY docs DESC")]
    try:
        activity = [dict(r) for r in d.con.execute(
            "SELECT substr(ts,1,10) day, COUNT(*) queries FROM audit_log GROUP BY day ORDER BY day DESC LIMIT 14")]
        recent = [dict(r) for r in d.con.execute(
            "SELECT ts, query, engine, role FROM audit_log ORDER BY ts DESC LIMIT 10")]
    except Exception:
        activity, recent = [], []
    return {"domains": domains, "risk": risk, "by_category": by_cat,
            "activity": activity, "recent_queries": recent}


@app.get("/api/subscribe")
def subscribe(request: Request, q: str):
    """Подписка на тему: уведомление о новых документах (проверка — scripts/check_alerts.py)."""
    d = db()
    d.con.execute("CREATE TABLE IF NOT EXISTS subscriptions(id INTEGER PRIMARY KEY, "
                  "ts TEXT DEFAULT (datetime('now')), query TEXT, role TEXT)")
    d.con.execute("INSERT INTO subscriptions(query, role) VALUES(?,?)", (q[:300], get_role(request)))
    d.commit()
    return {"ok": True, "message": f"Подписка оформлена: «{q}». Новые документы по теме появятся в /api/alerts после доиндексации."}


@app.get("/api/alerts")
def alerts():
    """Уведомления: новые документы по подписанным темам."""
    d = db()
    try:
        return [dict(r) for r in d.con.execute("SELECT * FROM alerts ORDER BY ts DESC LIMIT 50")]
    except Exception:
        return []


@app.post("/api/graph_edit")
def graph_edit(request: Request, payload: dict = Body(...)):
    """Ручная корректировка графа экспертом: добавить/уточнить связь с автором и датой."""
    role = get_role(request)
    if role == "partner":
        return JSONResponse({"error": "внешний партнёр не может править граф"}, status_code=403)
    src, rel, dst = payload.get("src", "").strip(), payload.get("rel", "related_to").strip(), payload.get("dst", "").strip()
    author, comment = payload.get("author", "аноним").strip(), payload.get("comment", "").strip()
    if not src or not dst:
        return JSONResponse({"error": "нужны src и dst"}, status_code=400)
    d = db()
    ns = d.con.execute("SELECT * FROM nodes WHERE canon=?", (src,)).fetchone()
    nd = d.con.execute("SELECT * FROM nodes WHERE canon=?", (dst,)).fetchone()
    if not ns or not nd:
        return JSONResponse({"error": "сущность не найдена в графе (укажите канонические имена)"}, status_code=404)
    d.add_edge(ns["id"], nd["id"], rel[:40], 0, 0,
               [f"[ручная правка: {author}, роль {role}] {comment}"[:300]])
    d.con.execute("CREATE TABLE IF NOT EXISTS graph_edits(ts TEXT DEFAULT (datetime('now')), "
                  "src TEXT, rel TEXT, dst TEXT, author TEXT, role TEXT, comment TEXT)")
    d.con.execute("INSERT INTO graph_edits(src, rel, dst, author, role, comment) VALUES(?,?,?,?,?,?)",
                  (src, rel, dst, author, role, comment))
    d.commit()
    return {"ok": True, "message": f"Связь «{src}» —{rel}→ «{dst}» добавлена (автор: {author})"}


@app.get("/api/export")
def export(q: str):
    """Экспорт результата в JSON-LD."""
    res = search.answer(db(), q)
    jsonld = {
        "@context": {"@vocab": "https://schema.org/", "kg": "urn:kg:"},
        "@type": "Dataset", "name": f"Ответ на запрос: {q}",
        "text": res["answer"],
        "hasPart": [{"@type": "CreativeWork", "name": s["title"],
                     "temporalCoverage": s.get("year"), "spatialCoverage": s.get("geography")}
                    for s in res["sources"]],
        "about": [{"@type": "DefinedTerm", "name": n["label"], "inDefinedTermSet": n["type"]}
                  for n in res["graph"]["nodes"] if n.get("root")],
    }
    return jsonld
