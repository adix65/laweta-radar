"""Transport powiadomień na Telegram — SAMA rura, bez wiedzy o tym, co wozi.

To kopia sprawdzonego produkcyjnie transportu z repo źródłowego, ale ROZMYŚLNIE
okrojona do trzech funkcji: `_send`, `_escape_md`, `_truncate`. Oryginał miał
komplet gotowych `notify_*` z treścią alertów wklejoną wprost w kod — i to
właśnie ta część NIE nadaje się do przeniesienia, bo treść alertu to domena
produktu, a nie transportu. Alerty lawety (zlecenie z grupy, dystans, kontakt)
piszemy osobno; ten moduł ma zostać nudny i nie zmieniać się przy każdej zmianie
formatu wiadomości.

Zasady, które się TU nie zmieniają (i dlatego są tu opisane):

  - Brak TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID => `_send` loguje i zwraca False.
    NIE rzuca. Powiadomienie jest skutkiem ubocznym pracy workera, więc
    nieskonfigurowany Telegram ma wyciszyć alerty, a nie wywalić crona w środku
    przetwarzania postów.
  - HTTP 400 przy `parse_mode` => JEDNO ponowienie bez `parse_mode`. Telegram
    odrzuca całą wiadomość, gdy w treści zostanie niezescapowany znak Markdown,
    a treść pochodzi od obcych ludzi z grup FB — zawsze znajdzie się ktoś, kto
    wpisze `*` w numerze telefonu. Lepiej dowieźć alert bez pogrubień niż nie
    dowieźć go wcale.
  - Timeout 5 s i `except Exception` => każdy błąd sieci kończy się `False`.
    Operator ma dostać zlecenie kilka minut po publikacji; zawieszony request
    do api.telegram.org nie może zablokować pobierania kolejnych grup.
  - Obcinamy do MAX_LEN znaków, bo Telegram i tak odrzuca dłuższe.
  - `wyslij_zdjecie` (sendPhoto) oddaje None przy KAŻDYM niepowodzeniu i to
    wołający wysyła wtedy zwykłą wiadomość. Ten moduł nie zna treści alertu,
    więc nie ma prawa decydować, co zrobić z podpisem, który się nie zmieścił:
    limit podpisu (MAX_CAPTION) jest czterokrotnie niższy od limitu wiadomości.

Wołający ZAWSZE sprawdza wartość zwracaną (bool) — brak wyjątku nie znaczy
"wysłano". Stan "czy operator dostał alert" trzymamy w bazie, nie w logach.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

# .env leży obok pakietu (laweta_radar/.env) — ta sama ścieżka, z której czytają
# workery i plik stanu rotacji kluczy Apify.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{metoda}"
MAX_LEN = 4096
# PODPIS POD ZDJĘCIEM MA WŁASNY, CZTEROKROTNIE NIŻSZY LIMIT niż wiadomość
# tekstowa. Treść, która mieści się w `sendMessage`, potrafi nie zmieścić się
# w `sendPhoto` — i wtedy Telegram odrzuca CAŁE wywołanie, czyli alert nie
# dochodzi wcale. Kto woła, ten przycina świadomie (services/powiadomienia).
MAX_CAPTION = 1024
TIMEOUT_S = 5
# Wysyłka zdjęcia to kilkaset kilobajtów w górę, a nie krótki JSON — pięć sekund
# potrafi nie wystarczyć na wolnym łączu VPS-a. Osobna stała, żeby podniesienie
# jednego nie ruszało drugiego.
TIMEOUT_ZDJECIE_S = 15


def _log(msg: str) -> None:
    # stderr, nie stdout: stdout workera bywa parsowany/logowany osobno, a to jest
    # diagnostyka transportu, nie wynik pracy.
    print(f"[telegram_notify] {msg}", file=sys.stderr)


def _escape_md(s) -> str:
    """Escape znaków legacy Markdown: _ * ` [

    Treść posta z grupy FB wchodzi tu jako CUDZY tekst. Jeden niesparowany `*`
    w ogłoszeniu i Telegram odrzuca całą wiadomość — dlatego escapujemy każdy
    fragment pochodzący z zewnątrz, a nie tylko ten, który wygląda podejrzanie.
    """
    if not s:
        return ""
    out = str(s)
    for ch in ("_", "*", "`", "["):
        out = out.replace(ch, "\\" + ch)
    return out


def _truncate(s, n: int) -> str:
    """Skróć do n znaków z wielokropkiem — limit Telegrama dotyczy CAŁEJ wiadomości.

    Obcinamy pojedyncze pola (treść posta, powód), a nie gotową wiadomość, żeby
    przycięcie zjadło opis, a nie link do posta doklejony na końcu.
    """
    if not s:
        return ""
    txt = str(s).strip()
    return txt if len(txt) <= n else txt[:n].rstrip() + "..."


def skonfigurowany() -> bool:
    """Czy da się w ogóle wysłać. Wołający sprawdza to PRZED policzeniem treści,
    gdy budowa treści kosztuje (zapytania do bazy przy dedupie)."""
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
                and os.environ.get("TELEGRAM_CHAT_ID", "").strip())


def wywolaj(metoda: str, payload: dict, timeout: int = TIMEOUT_S) -> dict | None:
    """Dowolna metoda Bot API. Zwraca `result` z odpowiedzi albo None.

    Jedno wejście do api.telegram.org dla całego repo — alerty (`_send`),
    przyciski (`wyslij`) i bot z long pollingiem (`workers/bot.py`) chodzą tędy.
    Osobne wołania w każdym z tych miejsc znaczyłyby trzy różne obsługi timeoutu
    i trzy różne odpowiedzi na pytanie „co, gdy Telegram zwróci 429".

    None zamiast wyjątku, ZAWSZE — także przy 500 od Telegrama i przy zerwanej
    sieci. To jest transport skutku ubocznego: powiadomienie o zleceniu nie może
    wywalić przebiegu, który akurat przetwarza dwieście postów.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        _log(f"{metoda}: brak TELEGRAM_BOT_TOKEN - skip")
        return None
    req = urllib.request.Request(
        TELEGRAM_API.format(token=token, metoda=metoda),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return _odpowiedz(metoda, req, timeout)


def _odpowiedz(metoda: str, req, timeout: int) -> dict | None:
    """Wykonaj gotowe żądanie i oddaj `result`. Wspólne dla JSON-a i multiparta.

    Obsługa błędów jest tu JEDNA, bo różnica między wysłaniem tekstu a wysłaniem
    zdjęcia kończy się na ciele żądania. Dwie kopie tego bloku znaczyłyby dwie
    różne odpowiedzi na pytanie „co, gdy Telegram zwróci 429" — a to jest
    dokładnie ta klasa rozjazdu, której się nie zauważa, dopóki nie zaboli.
    """
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            odp = json.loads(resp.read().decode("utf-8"))
        if not odp.get("ok"):
            _log(f"{metoda}: ok=false, {str(odp.get('description'))[:200]}")
            return None
        return odp.get("result")
    except urllib.error.HTTPError as e:
        # Treść błędu Telegrama jest w ciele odpowiedzi, nie w `reason` —
        # bez niej „HTTP 400" nie mówi, CZY chodzi o Markdown, czy o zły chat_id.
        try:
            szczegol = e.read().decode("utf-8")[:300]
        except Exception:  # noqa: BLE001 — diagnostyka nie może dorzucić drugiego błędu
            szczegol = e.reason
        _log(f"{metoda}: HTTP {e.code}: {szczegol}")
        raise
    except Exception as e:  # noqa: BLE001 — patrz docstring `wywolaj`
        _log(f"{metoda}: exception: {str(e)[:200]}")
        return None


def wywolaj_z_plikiem(metoda: str, pola: dict, nazwa_pola: str,
                      nazwa_pliku: str, dane: bytes, typ_mime: str = "image/png",
                      timeout: int = TIMEOUT_ZDJECIE_S) -> dict | None:
    """Metoda Bot API z załącznikiem — `multipart/form-data` zamiast JSON-a.

    Bot API przyjmuje plik na trzy sposoby: `file_id` (mamy tylko po wysłaniu),
    URL (musiałby być publiczny — czyli mapa trasy operatora w internecie) albo
    upload multipartem. Zostaje ostatni i dlatego ta funkcja w ogóle istnieje.

    Ciało składamy ręcznie ze stdlib, tak jak reszta tego modułu chodzi na
    `urllib`: to jedno krótkie wołanie bez retry, a mniej zależności w ścieżce
    alertu = mniej rzeczy, które mogą go zablokować.

    Wartości pól tekstowych idą jako UTF-8 bez kodowania nazw — nazwy pól są
    STAŁYMI z tego repo („caption", „reply_markup"), a nie danymi z zewnątrz.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        _log(f"{metoda}: brak TELEGRAM_BOT_TOKEN - skip")
        return None

    # Losowy ogon granicy: gdyby ciąg trafił się w bajtach PNG, serwer przeciąłby
    # obrazek w środku. Szansa jest znikoma, ale koszt zabezpieczenia zerowy.
    granica = "----lawetaradar" + secrets.token_hex(16)
    czesci: list[bytes] = []
    for nazwa, wartosc in pola.items():
        czesci.append(
            f"--{granica}\r\n"
            f'Content-Disposition: form-data; name="{nazwa}"\r\n\r\n'
            f"{wartosc}\r\n".encode("utf-8"))
    czesci.append(
        f"--{granica}\r\n"
        f'Content-Disposition: form-data; name="{nazwa_pola}"; '
        f'filename="{nazwa_pliku}"\r\n'
        f"Content-Type: {typ_mime}\r\n\r\n".encode("utf-8"))
    czesci.append(dane)
    czesci.append(f"\r\n--{granica}--\r\n".encode("utf-8"))
    cialo = b"".join(czesci)

    req = urllib.request.Request(
        TELEGRAM_API.format(token=token, metoda=metoda),
        data=cialo,
        headers={"Content-Type": f"multipart/form-data; boundary={granica}",
                 "Content-Length": str(len(cialo))},
    )
    return _odpowiedz(metoda, req, timeout)


def wyslij(text: str, przyciski: list | None = None,
           parse_mode: str | None = "Markdown") -> int | None:
    """Wyślij wiadomość; oddaj `message_id` albo None.

    ODDAJEMY message_id, a nie bool, bo bez niego nie da się później EDYTOWAĆ
    wiadomości — a edycja jest tu funkcją, nie ozdobą: po kliknięciu „Śmieć"
    alert musi zgubić przyciski i dostać dopisek. Wiadomość, która po kliknięciu
    wygląda identycznie, uczy operatora klikać drugi raz.

    `przyciski` to gotowy `inline_keyboard` (lista wierszy). Transport nie wie,
    co na nich jest — treść i układ alertu to domena `services/powiadomienia.py`.
    """
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        _log("brak TELEGRAM_CHAT_ID - skip")
        return None

    payload: dict = {
        "chat_id": chat_id,
        "text": text[:MAX_LEN],
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if przyciski:
        payload["reply_markup"] = {"inline_keyboard": przyciski}

    try:
        wynik = wywolaj("sendMessage", payload)
    except urllib.error.HTTPError as e:
        if e.code == 400 and parse_mode:
            # Jedyne ponowienie w tym module. Telegram odrzuca CAŁĄ wiadomość,
            # gdy w treści zostanie niesparowany znak Markdown — a treść pochodzi
            # od obcych ludzi z grup FB i zawsze znajdzie się ktoś, kto wpisze `*`
            # w numerze telefonu. Lepiej dowieźć alert bez pogrubień niż wcale.
            # Rekurencja z parse_mode=None nie zapętli się: drugi przebieg nie
            # spełnia już warunku.
            _log(f"HTTP 400 z parse_mode={parse_mode}, retry plain text")
            return wyslij(text, przyciski, parse_mode=None)
        return None
    return (wynik or {}).get("message_id")


def wyslij_zdjecie(sciezka, caption: str, przyciski: list | None = None,
                   parse_mode: str | None = "Markdown") -> int | None:
    """Wyślij zdjęcie z podpisem; oddaj `message_id` albo None.

    None znaczy „nie poszło" i wołający MA wtedy wysłać zwykłą wiadomość —
    obrazek jest dodatkiem, a alert, który nie dotarł, to stracony kurs. Ta
    funkcja nie robi tego sama, bo nie zna treści pełnej wiadomości: podpis jest
    przycięty do MAX_CAPTION, a tekst może być dłuższy.

    Powodów na None jest cztery i wszystkie kończą się tak samo: brak
    TELEGRAM_CHAT_ID, nieczytelny plik, odrzucenie przez Telegram, awaria sieci.
    """
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        _log("brak TELEGRAM_CHAT_ID - skip")
        return None

    plik = Path(sciezka)
    try:
        dane = plik.read_bytes()
    except OSError as e:
        # Plik mógł zniknąć między wygenerowaniem a wysyłką (sprzątanie cache'u,
        # pełny dysk). To nie jest awaria alertu — to powód, żeby poszedł tekstem.
        _log(f"sendPhoto: nie mogę odczytać {plik.name}: {type(e).__name__}")
        return None
    if not dane:
        _log(f"sendPhoto: {plik.name} jest pusty")
        return None

    pola: dict = {"chat_id": chat_id, "caption": caption[:MAX_CAPTION]}
    if parse_mode:
        pola["parse_mode"] = parse_mode
    if przyciski:
        # Przyciski w multiparcie idą jako pole tekstowe z JSON-em — inaczej niż
        # przy `sendMessage`, gdzie całe ciało jest JSON-em.
        pola["reply_markup"] = json.dumps({"inline_keyboard": przyciski},
                                          ensure_ascii=False)

    try:
        wynik = wywolaj_z_plikiem("sendPhoto", pola, "photo", plik.name, dane)
    except urllib.error.HTTPError as e:
        if e.code == 400 and parse_mode:
            # Ta sama zasada co w `wyslij`: niesparowany znak Markdowna w cudzym
            # poście nie może skasować całego alertu. Rekurencja nie zapętli się,
            # bo drugi przebieg nie spełnia już warunku.
            _log(f"sendPhoto: HTTP 400 z parse_mode={parse_mode}, retry plain text")
            return wyslij_zdjecie(sciezka, caption, przyciski, parse_mode=None)
        return None
    return (wynik or {}).get("message_id")


def _send(text: str, parse_mode: str | None = "Markdown") -> bool:
    """Wyślij wiadomość na skonfigurowany czat. True = dostarczona, False = nie.

    Cienka nakładka na `wyslij` — zostaje, bo to jest podpis używany przez
    wołających, którzy nie mają co zrobić z `message_id` (CLI diagnostyczne,
    komunikaty serwisowe).

    Świadomie na `urllib` ze stdlib, nie na httpx: to jedno krótkie wołanie bez
    retry i bez proxy, a mniej zależności w ścieżce alertu = mniej rzeczy, które
    mogą go zablokować.
    """
    if not skonfigurowany():
        _log("brak TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID - skip")
        return False
    return wyslij(text, parse_mode=parse_mode) is not None


# ---------------------------------------------------------------------------
# CLI — sprawdzenie, czy Telegram Z TEJ MASZYNY dowozi. Diagnostyka sytuacji
# "zlecenia są w bazie, telefon milczy": rozdziela problem transportu od
# problemu pipeline'u, zanim ktoś zacznie szukać błędu w klasyfikatorze.
#   python -m laweta_radar.services.telegram_notify            # wiadomość testowa
#   python -m laweta_radar.services.telegram_notify "tekst"    # własna treść
# Kod wyjścia: 0 = wysłano, 1 = skip/błąd (szczegóły na stderr z _log).
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    print(f"TELEGRAM_BOT_TOKEN: {'ustawiony' if token else 'BRAK'} | "
          f"TELEGRAM_CHAT_ID: {'ustawiony' if chat_id else 'BRAK'}")
    text = " ".join(argv[1:]).strip() or (
        "✅ Test powiadomień LAWETA RADAR — jeśli to widzisz, "
        "Telegram z tego środowiska działa.")
    ok = _send(text)
    print(f"Wysłano: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
