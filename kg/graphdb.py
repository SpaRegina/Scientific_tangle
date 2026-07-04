# -*- coding: utf-8 -*-
"""Хранилище графа знаний: SQLite (узлы, рёбра, провенанс, параметры) + FTS5."""
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE, title TEXT, category TEXT,
  year INTEGER, geography TEXT, n_pages INTEGER, added_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS nodes(
  id INTEGER PRIMARY KEY, canon TEXT, type TEXT, UNIQUE(canon, type)
);
CREATE TABLE IF NOT EXISTS mentions(
  node_id INTEGER, doc_id INTEGER, cnt INTEGER, PRIMARY KEY(node_id, doc_id)
);
CREATE TABLE IF NOT EXISTS edges(
  id INTEGER PRIMARY KEY, src INTEGER, dst INTEGER, rel TEXT, weight INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now')),
  status TEXT DEFAULT 'auto',
  source_count INTEGER DEFAULT 0,
  UNIQUE(src, dst, rel)
);
CREATE TABLE IF NOT EXISTS edge_evidence(
  edge_id INTEGER, doc_id INTEGER, page INTEGER, snippet TEXT
);
CREATE TABLE IF NOT EXISTS params(
  id INTEGER PRIMARY KEY, node_id INTEGER, doc_id INTEGER, page INTEGER,
  raw TEXT, qual TEXT, val REAL, val2 REAL, unit TEXT, snippet TEXT
);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, doc_id INTEGER, page INTEGER, txt TEXT
);
CREATE TABLE IF NOT EXISTS fact_history(
  id INTEGER PRIMARY KEY,
  edge_id INTEGER,
  ts TEXT DEFAULT (datetime('now')),
  doc_id INTEGER,
  page INTEGER,
  action TEXT,
  status TEXT,
  note TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  txt, content='chunks', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE INDEX IF NOT EXISTS ix_mentions_doc ON mentions(doc_id);
CREATE INDEX IF NOT EXISTS ix_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS ix_edges_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS ix_params_node ON params(node_id);
CREATE INDEX IF NOT EXISTS ix_evidence_edge ON edge_evidence(edge_id);
CREATE INDEX IF NOT EXISTS ix_fact_history_edge ON fact_history(edge_id);
"""


class GraphDB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(edges)").fetchall()}
        if "created_at" not in cols:
            self.con.execute("ALTER TABLE edges ADD COLUMN created_at TEXT")
            self.con.execute("UPDATE edges SET created_at = COALESCE(created_at, datetime('now'))")
        if "updated_at" not in cols:
            self.con.execute("ALTER TABLE edges ADD COLUMN updated_at TEXT")
            self.con.execute("UPDATE edges SET updated_at = COALESCE(updated_at, datetime('now'))")
        if "status" not in cols:
            self.con.execute("ALTER TABLE edges ADD COLUMN status TEXT")
            self.con.execute("UPDATE edges SET status = COALESCE(status, 'auto')")
        if "source_count" not in cols:
            self.con.execute("ALTER TABLE edges ADD COLUMN source_count INTEGER")
            self.con.execute("UPDATE edges SET source_count = COALESCE(source_count, 0)")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS fact_history("
            "id INTEGER PRIMARY KEY, edge_id INTEGER, ts TEXT DEFAULT (datetime('now')), "
            "doc_id INTEGER, page INTEGER, action TEXT, status TEXT, note TEXT)"
        )
        self.commit()

    # ---------- запись ----------
    def add_document(self, path, title, category, year, geography, n_pages):
        cur = self.con.execute(
            "INSERT OR IGNORE INTO documents(path,title,category,year,geography,n_pages) VALUES(?,?,?,?,?,?)",
            (path, title, category, year, geography, n_pages),
        )
        if cur.lastrowid:
            return cur.lastrowid, True
        row = self.con.execute("SELECT id FROM documents WHERE path=?", (path,)).fetchone()
        return row["id"], False

    def node_id(self, canon, etype):
        self.con.execute("INSERT OR IGNORE INTO nodes(canon,type) VALUES(?,?)", (canon, etype))
        return self.con.execute("SELECT id FROM nodes WHERE canon=? AND type=?", (canon, etype)).fetchone()["id"]

    def add_mention(self, node_id, doc_id, cnt):
        self.con.execute(
            "INSERT INTO mentions(node_id,doc_id,cnt) VALUES(?,?,?) "
            "ON CONFLICT(node_id,doc_id) DO UPDATE SET cnt=cnt+excluded.cnt",
            (node_id, doc_id, cnt),
        )

    def add_edge(self, src, dst, rel, doc_id, page, snippets, status="auto", note=None):
        delta = max(1, len(snippets))
        self.con.execute(
            "INSERT INTO edges(src,dst,rel,weight,status,source_count) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(src,dst,rel) DO UPDATE SET "
            "weight=weight+excluded.weight, "
            "updated_at=datetime('now'), "
            "status=excluded.status, "
            "source_count=source_count+CASE WHEN excluded.source_count>0 THEN 1 ELSE 0 END",
            (src, dst, rel, delta, status, 1 if doc_id else 0),
        )
        edge_id = self.con.execute("SELECT id FROM edges WHERE src=? AND dst=? AND rel=?", (src, dst, rel)).fetchone()["id"]
        for sn in snippets[:2]:
            self.con.execute(
                "INSERT INTO edge_evidence(edge_id,doc_id,page,snippet) VALUES(?,?,?,?)",
                (edge_id, doc_id, page, sn),
            )
        self.con.execute(
            "INSERT INTO fact_history(edge_id, doc_id, page, action, status, note) VALUES(?,?,?,?,?,?)",
            (edge_id, doc_id, page, "upsert", status, (note or "")[:300]),
        )

    def add_param(self, node_id, doc_id, page, raw, qual, val, val2, unit, snippet):
        self.con.execute(
            "INSERT INTO params(node_id,doc_id,page,raw,qual,val,val2,unit,snippet) VALUES(?,?,?,?,?,?,?,?,?)",
            (node_id, doc_id, page, raw, qual, val, val2, unit, snippet),
        )

    def add_chunk(self, doc_id, page, txt):
        cur = self.con.execute("INSERT INTO chunks(doc_id,page,txt) VALUES(?,?,?)", (doc_id, page, txt))
        self.con.execute("INSERT INTO chunks_fts(rowid,txt) VALUES(?,?)", (cur.lastrowid, txt))

    def commit(self):
        self.con.commit()

    # ---------- чтение ----------
    def stats(self):
        q = lambda sql: self.con.execute(sql).fetchone()[0]
        return {
            "documents": q("SELECT COUNT(*) FROM documents"),
            "nodes": q("SELECT COUNT(*) FROM nodes"),
            "edges": q("SELECT COUNT(*) FROM edges"),
            "params": q("SELECT COUNT(*) FROM params"),
            "chunks": q("SELECT COUNT(*) FROM chunks"),
            "by_type": {
                r["type"]: r["c"]
                for r in self.con.execute("SELECT type, COUNT(*) c FROM nodes GROUP BY type ORDER BY c DESC")
            },
        }

    def fts(self, match, limit=40):
        return self.con.execute(
            "SELECT c.doc_id, c.page, snippet(chunks_fts, 0, '<mark>', '</mark>', '…', 40) AS snip, "
            "bm25(chunks_fts) AS score, d.title, d.path, d.category, d.year, d.geography "
            "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid JOIN documents d ON d.id = c.doc_id "
            "WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?",
            (match, limit),
        ).fetchall()

    def get_node(self, canon, etype=None):
        if etype:
            return self.con.execute("SELECT * FROM nodes WHERE canon=? AND type=?", (canon, etype)).fetchone()
        return self.con.execute("SELECT * FROM nodes WHERE canon=?", (canon,)).fetchone()

    def neighbors(self, node_id, limit=60):
        return self.con.execute(
            """
            SELECT e.rel, e.weight, e.status, e.updated_at, n2.id, n2.canon, n2.type, 'out' AS dir
              FROM edges e JOIN nodes n2 ON n2.id = e.dst WHERE e.src = ?
            UNION ALL
            SELECT e.rel, e.weight, e.status, e.updated_at, n1.id, n1.canon, n1.type, 'in' AS dir
              FROM edges e JOIN nodes n1 ON n1.id = e.src WHERE e.dst = ?
            ORDER BY weight DESC LIMIT ?
            """,
            (node_id, node_id, limit),
        ).fetchall()

    def edge_between(self, id1, id2):
        return self.con.execute(
            """
            SELECT e.*, (SELECT json_group_array(json_object('doc_id',doc_id,'page',page,'snippet',snippet))
                         FROM edge_evidence ev WHERE ev.edge_id=e.id LIMIT 5) AS evidence
            FROM edges e WHERE (src=? AND dst=?) OR (src=? AND dst=?)
            """,
            (id1, id2, id2, id1),
        ).fetchall()

    def node_docs(self, node_id, limit=30):
        return self.con.execute(
            """
            SELECT d.*, m.cnt FROM mentions m JOIN documents d ON d.id=m.doc_id
            WHERE m.node_id=? ORDER BY m.cnt DESC LIMIT ?
            """,
            (node_id, limit),
        ).fetchall()

    def node_params(self, node_id, limit=50):
        return self.con.execute(
            """
            SELECT p.*, d.title, d.year FROM params p JOIN documents d ON d.id=p.doc_id
            WHERE p.node_id=? ORDER BY p.id LIMIT ?
            """,
            (node_id, limit),
        ).fetchall()
