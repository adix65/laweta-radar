"""Generator ikon PWA — czysty zlib, bez Pillow.

ODPALANE RĘCZNIE, RAZ:  python3 panel/scripts/ikony.py
Pliki lądują w panel/public/ i IDĄ DO REPO. Ikona generowana w locie przez
route handler byłaby zależnością runtime dla czegoś, co zmienia się raz na rok
— i pierwszym elementem, który przestaje działać, gdy panel wstaje offline.

Bez Pillow, bo to jednorazowe narzędzie, a nie zależność deployu: paczka do
generowania obrazków w requirements produkcyjnych to coś, co trzeba budować na
VPS-ie za funkcję używaną raz.

Ikona: ciemne tło #0A0A0B (to samo co theme-color), biały znacznik pozycji
z czerwonym środkiem. Znacznik, bo to jest aplikacja o TYM, GDZIE stoi auto —
sylwetka lawety w 192 px zamienia się w plamę.
"""
import struct, zlib, math

TLO = (0x0A, 0x0A, 0x0B)
BIALY = (0xFF, 0xFF, 0xFF)
CZERWONY = (0xFF, 0x45, 0x3A)


def piksel(x, y, n):
    """Kolor piksela. Wszystko liczone we współrzędnych względnych (0..1),
    żeby ten sam kod dał 192 i 512 bez osobnych stałych."""
    u, v = (x + 0.5) / n, (y + 0.5) / n

    # Znacznik pozycji: koło u góry + trójkątny grot w dół.
    cx, cy, r = 0.5, 0.42, 0.235
    d = math.hypot(u - cx, v - cy)

    w_kole = d <= r
    # Grot: trójkąt od dołu koła do punktu (0.5, 0.90).
    szerokosc = max(0.0, (0.90 - v) / (0.90 - cy)) * (r * 0.86)
    w_grocie = cy <= v <= 0.90 and abs(u - cx) <= szerokosc

    if w_kole or w_grocie:
        # Oczko w środku koła — czerwone, ten sam odcień co pasek „pilne".
        if d <= r * 0.42:
            return CZERWONY
        return BIALY
    return TLO


def png(n, sciezka):
    surowe = bytearray()
    for y in range(n):
        surowe.append(0)                      # filtr 0 (None) dla każdego wiersza
        for x in range(n):
            surowe.extend(piksel(x, y, n))

    def kawalek(typ, dane):
        c = typ + dane
        return struct.pack(">I", len(dane)) + c + struct.pack(">I", zlib.crc32(c))

    plik = (b"\x89PNG\r\n\x1a\n"
            + kawalek(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 2, 0, 0, 0))
            + kawalek(b"IDAT", zlib.compress(bytes(surowe), 9))
            + kawalek(b"IEND", b""))
    with open(sciezka, "wb") as f:
        f.write(plik)
    print(f"{sciezka}: {n}x{n}, {len(plik)} B")


png(192, "panel/public/icon-192.png")
png(512, "panel/public/icon-512.png")
