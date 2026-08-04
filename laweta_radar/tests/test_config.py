"""Offline testy config/groups.py i config/settings.py.

Sprawdzają JEDNĄ rzecz, na której stoi cała reszta repo: że brak konfiguracji
kończy się CZYSTYM wyjściem z komunikatem, a nie wyjątkiem. To nie jest detal
kosmetyczny — workery chodzą z crona, więc wyjątek przy starcie zamienia się
w awarię powtarzaną co kilka minut, a niezerowy kod wyjścia zapycha skrzynkę
operatora do momentu, w którym prawdziwa awaria już nie ma jak się przebić.

Bez sieci i bez bazy: środowisko podajemy jawnie przez monkeypatch.
"""
from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from laweta_radar.config import groups, settings  # noqa: E402


# ---------------------------------------------------------------------------
# groups — filtr "co wolno pobrać"
# ---------------------------------------------------------------------------
def test_pobieramy_tylko_zweryfikowane_z_adresem():
    """Wpis bez adresu albo niezweryfikowany NIE trafia do pobierania.

    Run na grupie prywatnej/martwej i tak zostanie przez Apify policzony, więc
    filtr jest tu ochroną budżetu, nie kosmetyką.
    """
    src = [
        {"url": "https://fb.com/groups/a", "name": "ok", "status": "ok"},
        {"url": "https://fb.com/groups/b", "name": "niesprawdzona", "status": "unverified"},
        {"url": "", "name": "bez adresu", "status": "ok"},
        {"url": "   ", "name": "same spacje", "status": "ok"},
    ]
    assert [g["name"] for g in groups.grupy_do_pobrania(src)] == ["ok"]


def test_brak_statusu_znaczy_ok():
    """Zgodność ze wzorcem z repo źródłowego: brak klucza `status` = "ok"."""
    src = [{"url": "https://fb.com/groups/a", "name": "stary wpis"}]
    assert len(groups.grupy_do_pobrania(src)) == 1


def test_domyslna_lista_nie_pobiera_niczego():
    """Repo po sklonowaniu NIE strzela do Apify.

    Wszystkie wpisy startują bez adresu i jako "unverified" — świeży klon nie
    zna regionu operatora, a pobieranie z przykładowych grup to wydany kredyt
    za zlecenia, po które nikt nie pojedzie.
    """
    assert groups.grupy_do_pobrania() == []


def test_opis_listy_liczy_braki():
    assert "0 grup do pobrania" in groups.opis_listy()


# ---------------------------------------------------------------------------
# settings — brak konfiguracji to nie awaria
# ---------------------------------------------------------------------------
def test_brakujace_zwraca_wszystkie_braki_naraz(monkeypatch):
    """Lista, nie bool — żeby operator poprawił .env RAZ, a nie w kółko."""
    monkeypatch.setattr(settings, "_txt", lambda n, d="": {"JEST": "wartosc"}.get(n, d))
    assert settings.brakujace("JEST", "NIE_MA", "TEZ_NIE") == ["NIE_MA", "TEZ_NIE"]


def test_puste_i_same_spacje_liczą_się_jako_brak(monkeypatch):
    """`FOO=` i `FOO="   "` w .env to ta sama pomyłka co brak linijki."""
    monkeypatch.setenv("PUSTE", "")
    monkeypatch.setenv("SPACJE", "   ")
    assert settings.brakujace("PUSTE", "SPACJE") == ["PUSTE", "SPACJE"]


def test_wyjscie_bez_konfiguracji_zwraca_zero():
    """Kod 0 = "nie ma nic do zrobienia", nie awaria — patrz docstring funkcji."""
    buf = io.StringIO()
    assert settings.wyjscie_bez_konfiguracji("test", ["DATABASE_URL"], buf) == 0


def test_wyjscie_bez_konfiguracji_mowi_CO_ustawic():
    """Komunikat musi nieść nazwę zmiennej i jej opis — sam fakt braku nie pomaga."""
    buf = io.StringIO()
    settings.wyjscie_bez_konfiguracji("test", ["DATABASE_URL", "TELEGRAM_CHAT_ID"], buf)
    out = buf.getvalue()
    assert "DATABASE_URL" in out and "TELEGRAM_CHAT_ID" in out
    assert settings.OPIS_ZMIENNYCH["DATABASE_URL"] in out
    assert ".env" in out


def test_liczby_degraduja_do_domyslnych_zamiast_rzucac(monkeypatch):
    """Literówka w pokrętle nie może zatrzymać przebiegu."""
    monkeypatch.setenv("X_INT", "30 minut")
    monkeypatch.setenv("X_FLOAT", "bardzo daleko")
    assert settings._int("X_INT", 12) == 12
    assert settings._float("X_FLOAT", 1.5) == 1.5


def test_przecinek_dziesietny_we_wspolrzednych(monkeypatch):
    """Współrzędne kopiuje się z Map Google, a te podają je z przecinkiem."""
    monkeypatch.setenv("LAT", "49,6885")
    assert settings._float("LAT", 0.0) == 49.6885


def test_opis_srodowiska_nie_niesie_sekretow(monkeypatch):
    """Linia startowa mówi 'tak/BRAK', nigdy wartości — logi bywają czytane szeroko."""
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "123456:TAJNY-TOKEN")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "-100999")
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://user:haslo@host/laweta")
    opis = settings.opis_srodowiska()
    assert "TAJNY-TOKEN" not in opis and "haslo" not in opis and "-100999" not in opis
    assert "telegram=tak" in opis and "db=tak" in opis


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
