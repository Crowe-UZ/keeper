#!/usr/bin/env sh
# Модульные тесты качалки. Берёт Python из .venv проекта, если он есть (там стоит yt-dlp), иначе системный.
cd "$(dirname "$0")/.." || exit 2
if [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe
elif [ -x .venv/bin/python ]; then PY=.venv/bin/python
else PY=python
fi
KACHALKA_PORT="${KACHALKA_PORT:-5599}" PYTHONIOENCODING=utf-8 exec "$PY" -m unittest discover -s tests -t .
