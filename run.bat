@echo off
rem 1) построить граф:  python -m kg.build --src "путь\к\Источники информации" --db data\kg.db --limit 200
rem 2) запустить UI:
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
