#!/bin/bash
# 1) построить граф:  python -m kg.build --src "путь/к/Источники информации" --db data/kg.db --limit 200
# 2) запустить UI:
uvicorn app.main:app --host 0.0.0.0 --port 8000
