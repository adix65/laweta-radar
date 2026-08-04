"""Laweta Radar — monitoring grup FB pod kątem zleceń dla lawety.

Import tego pakietu ma JEDEN skutek uboczny: scala konfigurację środowiska
(własny `.env` + klucze Apify ze wspólnego `.env` sales-core-engine — patrz
`config/settings.py`). Robimy to tutaj, a nie w każdym workerze osobno, z powodu
praktycznego: `workers/apify_keys.py` i `workers/apify_proxy.py` są kopiami 1:1
i czytają wprost `os.environ`. Gdyby scalanie siedziało w workerach, ich własne
punkty wejścia CLI (`python -m laweta_radar.workers.apify_keys`) nie widziałyby
wspólnej puli — i diagnostyka pokazywałaby „0 kluczy" na maszynie, na której
klucze są. Import podpakietu wykonuje najpierw ten plik, więc wszystkie wejścia
widzą to samo środowisko.

Ładowanie `.env` przy imporcie jest tu zresztą idiomem zastanym: tak samo robią
`services/telegram_notify.py` i `workers/apify_proxy.py` w repo źródłowym.
"""
from __future__ import annotations

try:
    from laweta_radar.config import shared_env as _shared_env  # noqa: F401
except Exception:  # noqa: BLE001
    # Brak python-dotenv (albo uszkodzony .env) NIE może uniemożliwić importu
    # pakietu. `workers/apify_keys.py` celowo nie ma twardych zależności i ma
    # dać się zaimportować wszędzie — a zasada w tym repo mówi, że brak
    # konfiguracji kończy się komunikatem, nie wyjątkiem.
    pass
