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


# ---------------------------------------------------------------------------
# Wspólna pula Apify — najgroźniejsze miejsce w tym repo
#
# Wspólny .env sales-core-engine niesie SWOJE DATABASE_URL i TELEGRAM_*. Gdyby
# przeciekły, laweta pisałaby do bazy sprzedażowej, a alerty o zleceniach szłyby
# na czat handlowca — i nikt by tego nie zauważył, bo wszystko „działa".
# ---------------------------------------------------------------------------
def _wspolny_env(tmp_path, tresc: str):
    p = tmp_path / "shared.env"
    p.write_text(tresc, encoding="utf-8")
    return p


def test_z_wspolnego_env_bierzemy_klucze_apify(tmp_path, monkeypatch):
    p = _wspolny_env(tmp_path, "APIFY_API_TOKEN1=aaa\nAPIFY_API_TOKEN2=bbb\n"
                               "APIFY_PROXY_URLS=http://u:p@host:8000\n"
                               "APIFY_PROXY3=http://u:p@inny:8000\n"
                               "APIFY_PROXY_REQUIRED=1\n")
    monkeypatch.setenv("SHARED_ENV_PATH", str(p))
    for n in ("APIFY_API_TOKEN1", "APIFY_API_TOKEN2", "APIFY_PROXY_URLS",
              "APIFY_PROXY3", "APIFY_PROXY_REQUIRED"):
        monkeypatch.delenv(n, raising=False)

    ile, skad = settings._wczytaj_wspolne_apify()
    assert ile == 5 and skad == str(p)
    assert os.environ["APIFY_API_TOKEN1"] == "aaa"
    assert os.environ["APIFY_PROXY_URLS"] == "http://u:p@host:8000"
    assert os.environ["APIFY_PROXY_REQUIRED"] == "1"


def test_ze_wspolnego_env_NIE_bierzemy_bazy_ani_telegrama(tmp_path, monkeypatch):
    """Sedno zabezpieczenia: cudzy DSN i cudzy czat nie mogą tu wejść."""
    p = _wspolny_env(tmp_path,
                     "APIFY_API_TOKEN1=aaa\n"
                     "DATABASE_URL=postgresql://sales@host/sales\n"
                     "TELEGRAM_BOT_TOKEN=111:handlowiec\n"
                     "TELEGRAM_CHAT_ID=-100handlowiec\n"
                     "ANTHROPIC_API_KEY=sk-cudzy\n"
                     "SMTP_HOST=smtp.cudzy\n")
    monkeypatch.setenv("SHARED_ENV_PATH", str(p))
    for n in ("APIFY_API_TOKEN1", "DATABASE_URL", "TELEGRAM_BOT_TOKEN",
              "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY", "SMTP_HOST"):
        monkeypatch.delenv(n, raising=False)

    ile, _ = settings._wczytaj_wspolne_apify()
    assert ile == 1, "przepisano coś spoza listy APIFY_*"
    assert "DATABASE_URL" not in os.environ, "DSN bazy sprzedażowej przeciekł do lawety!"
    assert "TELEGRAM_BOT_TOKEN" not in os.environ
    assert "TELEGRAM_CHAT_ID" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "SMTP_HOST" not in os.environ


def test_darmowa_pula_proxy_NIE_jest_dziedziczona(tmp_path, monkeypatch):
    """APIFY_PROXY_POOL ma zostać wyłączone TUTAJ, niezależnie od tamtego repo.

    Pula bez odświeżania kieruje przez jeden żywy wpis komplet kont i zamienia
    runy w timeouty. Gdyby zmienna się dziedziczyła, ktoś włączający pulę
    w sales-core-engine włączyłby ją tutaj po cichu.
    """
    p = _wspolny_env(tmp_path, "APIFY_API_TOKEN1=aaa\nAPIFY_PROXY_POOL=1\n"
                               "APIFY_PROXY_POOL_ANONYMITY=any\n"
                               "APIFY_PROXY_POOL_FILE=/cudzy/pool.json\n")
    monkeypatch.setenv("SHARED_ENV_PATH", str(p))
    for n in ("APIFY_API_TOKEN1", "APIFY_PROXY_POOL", "APIFY_PROXY_POOL_ANONYMITY",
              "APIFY_PROXY_POOL_FILE"):
        monkeypatch.delenv(n, raising=False)

    settings._wczytaj_wspolne_apify()
    assert "APIFY_PROXY_POOL" not in os.environ
    assert "APIFY_PROXY_POOL_ANONYMITY" not in os.environ
    assert "APIFY_PROXY_POOL_FILE" not in os.environ


def test_wlasne_ustawienie_wygrywa_ze_wspolnym(tmp_path, monkeypatch):
    """Klucz testowy na maszynie deweloperskiej ma odcinać od wspólnej puli."""
    p = _wspolny_env(tmp_path, "APIFY_API_TOKEN1=ze_wspolnego\n")
    monkeypatch.setenv("SHARED_ENV_PATH", str(p))
    monkeypatch.setenv("APIFY_API_TOKEN1", "moj_lokalny")

    settings._wczytaj_wspolne_apify()
    assert os.environ["APIFY_API_TOKEN1"] == "moj_lokalny"


def test_brak_wspolnego_pliku_to_nie_awaria(monkeypatch):
    """Na maszynie deweloperskiej sales-core-engine po prostu nie ma."""
    monkeypatch.setenv("SHARED_ENV_PATH", "/nie/ma/takiej/sciezki/.env")
    assert settings.sciezka_wspolnego_env() is None
    ile, skad = settings._wczytaj_wspolne_apify()
    assert ile == 0 and skad == "nie znaleziono"


def test_powtorne_wczytanie_raportuje_TE_SAME_liczby(tmp_path, monkeypatch):
    """Drugie wywołanie w tym samym procesie ma dać ten sam wynik, co pierwsze.

    Regresja z realnej wpadki: gdy funkcja liczyła ZAPISY, a nie „ile pochodzi ze
    wspólnego pliku", drugi przebieg raportował 0 (bo wszystko siedziało już
    w os.environ) — i diagnostyka pokazywała „0 kluczy ze wspólnej puli" tuż obok
    listy tych kluczy. Operator ma z takiej sprzeczności wyciągnąć jeden wniosek:
    że narzędziu nie można wierzyć.
    """
    p = _wspolny_env(tmp_path, "APIFY_API_TOKEN1=aaa\nAPIFY_API_TOKEN2=bbb\n")
    monkeypatch.setenv("SHARED_ENV_PATH", str(p))
    monkeypatch.delenv("APIFY_API_TOKEN1", raising=False)
    monkeypatch.delenv("APIFY_API_TOKEN2", raising=False)

    assert settings._wczytaj_wspolne_apify() == settings._wczytaj_wspolne_apify() == (2, str(p))


def test_puste_wartosci_we_wspolnym_sa_pomijane(tmp_path, monkeypatch):
    """`APIFY_API_TOKEN2=` w cudzym pliku nie może udawać ustawionego klucza."""
    p = _wspolny_env(tmp_path, "APIFY_API_TOKEN1=aaa\nAPIFY_API_TOKEN2=\n"
                               "APIFY_PROXY_URL=   \n")
    monkeypatch.setenv("SHARED_ENV_PATH", str(p))
    for n in ("APIFY_API_TOKEN1", "APIFY_API_TOKEN2", "APIFY_PROXY_URL"):
        monkeypatch.delenv(n, raising=False)

    ile, _ = settings._wczytaj_wspolne_apify()
    assert ile == 1
    assert "APIFY_API_TOKEN2" not in os.environ


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
