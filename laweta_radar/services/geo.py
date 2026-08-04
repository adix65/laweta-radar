"""Geografia zlecenia — ile kilometrów i dwa linki, w które operator klika.

CO TEN MODUŁ ROBI, A CZEGO NIE ROBI. Robi trzy rzeczy: zamienia nazwę miejsca
z posta na współrzędne, liczy dystans od bazy i buduje linki do map. NIE decyduje
o niczym. Zasada naczelna repo („system pokazuje, decyduje kierowca") dotyczy tego
modułu najmocniej, bo to on produkuje jedyną liczbę, którą łatwo pomylić z filtrem:
kilometry. `MAX_DYSTANS_KM` jest tu ETYKIETĄ do wyświetlenia, nigdy warunkiem
w `WHERE` — trasa Kolonia-Kraków to 1100 km i normalny dzień pracy tego operatora.

DLACZEGO BEZ SIECI. Geokoder online (Nominatim, Google) w ścieżce powiadomienia
znaczyłby, że alert o zleceniu zależy od cudzego serwisu i jego limitu zapytań
(Nominatim: 1/s, blokada przy przekroczeniu). Alert ma dojść w sekundę, a nie
„zwykle w sekundę". Dlatego słownik jest WBUDOWANY: kilkaset miast PL/DE/CZ/SK
plus tablica prefiksów polskich kodów pocztowych. To wystarcza, bo pytanie brzmi
„czy to 40 km czy 400 km", a nie „pod który numer domu".

Ceną jest dokładność — i ta cena musi być WIDOCZNA. Stąd `zrodlo` przy każdym
dopasowaniu:

    'miasto'          dokładne trafienie w słowniku; kilometry są dobre
    'kod_pocztowy'    trafienie po prefiksie kodu; środek powiatu, ±20 km
    'miasto_niepewne' dopasowanie rozmyte albo nazwa pasująca do kilku miejsc
    'brak'            nie wiemy nic — i mówimy to wprost, zamiast zgadywać

`'miasto_niepewne'` jest tu najważniejsze i istnieje po to, żeby powiadomienie
mogło dopisać znak zapytania przy lokalizacji. Cicho podana zła liczba kilometrów
jest gorsza niż brak liczby: operator pojedzie nie tam i dowie się o tym po
godzinie jazdy.

DWA LINKI, NIE JEDEN — bo to dwie różne czynności:
  `link_do_map`     — POKAŻ trasę (baza -> odbiór -> dostawa). Do oceny zlecenia.
  `link_nawigacji`  — JEDŹ do punktu odbioru. Do wykonania zlecenia.
Oba są zwykłymi URL-ami https, nie intentami `google.navigation:` — intent działa
tylko na Androidzie i tylko z aplikacji natywnej, a te linki klika się w Telegramie
i w przeglądarce na iOS.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote

from laweta_radar.config import settings

# Ile razy droga jest dłuższa od linii prostej. 1.25 to wartość obserwowana dla
# sieci dróg w Europie Środkowej przy trasach kilkudziesięciokilometrowych.
#
# DLACZEGO W OGÓLE MNOŻYMY: haversine liczy odległość w linii prostej, a laweta
# nie lata. Pokazanie operatorowi „42 km", gdy realnie do przejechania jest 55,
# to nie jest drobna nieścisłość — to jest liczba, na której podstawie ktoś
# podaje cenę przez telefon. Lepiej systematycznie zawyżać o kilka procent niż
# systematycznie zaniżać.
WSPOLCZYNNIK_DROGOWY = 1.25

PROMIEN_ZIEMI_KM = 6371.0

# Próg dopasowania rozmytego (Jaccard na trójkach znaków).
#
# WARTOŚĆ ZMIERZONA, NIE ZGADNIĘTA. Rozkład na realnych przypadkach:
#
#   trafienia, które CHCEMY łapać (literówka, odmiana przez przypadek):
#     „Krakuw" 0.40 · „Rzeszuw" 0.46 · „Jasla" 0.50 · „Wroclav" 0.60
#     „Sanoku" 0.63 · „Tarnowa" 0.67 · „Zabrze-Rokitnica" 0.33 (dzielnica Zabrza)
#   tekst, który NIE jest nazwą miasta:
#     „autostrada A4" 0.16 · „pod lasem" 0.14 · „Warsztat u Janka" 0.24
#
# Między tymi zbiorami jest przerwa i próg siedzi w niej. Wyżej (0.62, pierwsza
# wersja) odrzucał „Krakuw" i „Rzeszuw", czyli DOKŁADNIE to, po co istnieje
# dopasowanie rozmyte — jedna przekręcona litera w nazwie miasta jest w postach
# z grup normą, nie wyjątkiem.
#
# Asymetria kosztów przemawia za łapaniem: pomyłka w tę stronę dostaje etykietę
# 'miasto_niepewne', czyli ostrzeżenie w alercie i pasek w panelu z surową treścią
# z posta. Pomyłka w drugą stronę to zlecenie bez kilometrów i bez pinezki.
MIN_PODOBIENSTWO = 0.38


# ---------------------------------------------------------------------------
# SŁOWNIK MIEJSC — dane, nie kod (ta sama zasada co config/groups.py).
#
# Dopisanie miasta to jedna linijka. Kolejność bez znaczenia; klucz normalizuje
# `_klucz()`, więc wpisy zapisujemy w formie ORYGINALNEJ, z diakrytykami —
# ta forma wraca do linku z mapą i to ona ma trafić w geokoder Google.
# ---------------------------------------------------------------------------
MIASTA: dict[str, tuple[float, float]] = {
    # --- POLSKA ------------------------------------------------------------
    "Warszawa": (52.2297, 21.0122),
    "Kraków": (50.0647, 19.9450),
    "Łódź": (51.7592, 19.4560),
    "Wrocław": (51.1079, 17.0385),
    "Poznań": (52.4064, 16.9252),
    "Gdańsk": (54.3520, 18.6466),
    "Szczecin": (53.4285, 14.5528),
    "Bydgoszcz": (53.1235, 18.0084),
    "Lublin": (51.2465, 22.5684),
    "Białystok": (53.1325, 23.1688),
    "Katowice": (50.2649, 19.0238),
    "Gdynia": (54.5189, 18.5305),
    "Częstochowa": (50.7971, 19.1204),
    "Radom": (51.4027, 21.1471),
    "Sosnowiec": (50.2863, 19.1040),
    "Toruń": (53.0138, 18.5984),
    "Kielce": (50.8661, 20.6286),
    "Rzeszów": (50.0412, 21.9991),
    "Gliwice": (50.2945, 18.6714),
    "Zabrze": (50.3249, 18.7857),
    "Olsztyn": (53.7784, 20.4801),
    "Bielsko-Biała": (49.8224, 19.0584),
    "Bytom": (50.3484, 18.9157),
    "Zielona Góra": (51.9356, 15.5062),
    "Rybnik": (50.0971, 18.5416),
    "Ruda Śląska": (50.2584, 18.8555),
    "Opole": (50.6751, 17.9213),
    "Tychy": (50.1374, 18.9662),
    "Gorzów Wielkopolski": (52.7368, 15.2288),
    "Dąbrowa Górnicza": (50.3218, 19.1874),
    "Elbląg": (54.1522, 19.4088),
    "Płock": (52.5463, 19.7065),
    "Wałbrzych": (50.7710, 16.2843),
    "Włocławek": (52.6483, 19.0677),
    "Tarnów": (50.0121, 20.9858),
    "Chorzów": (50.2974, 18.9542),
    "Koszalin": (54.1943, 16.1722),
    "Kalisz": (51.7614, 18.0910),
    "Legnica": (51.2070, 16.1553),
    "Grudziądz": (53.4837, 18.7536),
    "Jaworzno": (50.2050, 19.2731),
    "Słupsk": (54.4641, 17.0287),
    "Jastrzębie-Zdrój": (49.9553, 18.5723),
    "Nowy Sącz": (49.6215, 20.6975),
    "Jelenia Góra": (50.9044, 15.7194),
    "Siedlce": (52.1676, 22.2902),
    "Mysłowice": (50.2077, 19.1666),
    "Konin": (52.2230, 18.2512),
    "Piła": (53.1512, 16.7382),
    "Piotrków Trybunalski": (51.4054, 19.7030),
    "Inowrocław": (52.7986, 18.2611),
    "Lubin": (51.4009, 16.2015),
    "Ostrów Wielkopolski": (51.6493, 17.8158),
    "Suwałki": (54.1114, 22.9309),
    "Stargard": (53.3365, 15.0499),
    "Gniezno": (52.5348, 17.5826),
    "Ostrowiec Świętokrzyski": (50.9294, 21.3856),
    "Siemianowice Śląskie": (50.3268, 19.0292),
    "Głogów": (51.6640, 16.0844),
    "Pabianice": (51.6645, 19.3547),
    "Leszno": (51.8406, 16.5748),
    "Żory": (50.0446, 18.7005),
    "Zamość": (50.7230, 23.2519),
    "Pruszków": (52.1706, 20.8113),
    "Łomża": (53.1783, 22.0593),
    "Ełk": (53.8281, 22.3648),
    "Tomaszów Mazowiecki": (51.5314, 20.0086),
    "Chełm": (51.1431, 23.4716),
    "Mielec": (50.2874, 21.4239),
    "Kędzierzyn-Koźle": (50.3495, 18.2262),
    "Przemyśl": (49.7838, 22.7677),
    "Stalowa Wola": (50.5826, 22.0537),
    "Tczew": (54.0925, 18.7776),
    "Biała Podlaska": (52.0325, 23.1149),
    "Bełchatów": (51.3689, 19.3567),
    "Świdnica": (50.8449, 16.4890),
    "Będzin": (50.3260, 19.1281),
    "Zgierz": (51.8551, 19.4062),
    "Piekary Śląskie": (50.3800, 18.9500),
    "Racibórz": (50.0917, 18.2192),
    "Legionowo": (52.4000, 20.9333),
    "Ostrołęka": (53.0862, 21.5710),
    "Świnoujście": (53.9100, 14.2472),
    "Starachowice": (51.0500, 21.0714),
    "Wejherowo": (54.6050, 18.2360),
    "Zawiercie": (50.4875, 19.4167),
    "Puławy": (51.4164, 21.9694),
    "Skierniewice": (51.9550, 20.1417),
    "Krosno": (49.6886, 21.7647),
    "Krosno Odrzańskie": (52.0533, 15.0975),
    "Sanok": (49.5558, 22.2060),
    "Jasło": (49.7450, 21.4714),
    "Dębica": (50.0516, 21.4111),
    "Nowy Targ": (49.4772, 20.0322),
    "Zakopane": (49.2992, 19.9496),
    "Oświęcim": (50.0344, 19.2098),
    "Chrzanów": (50.1353, 19.4020),
    "Olkusz": (50.2814, 19.5650),
    "Wadowice": (49.8836, 19.4931),
    "Myślenice": (49.8339, 19.9386),
    "Bochnia": (49.9690, 20.4297),
    "Brzesko": (49.9694, 20.6072),
    "Gorlice": (49.6553, 21.1600),
    "Limanowa": (49.7047, 20.4211),
    "Nysa": (50.4740, 17.3336),
    "Brzeg": (50.8617, 17.4670),
    "Kluczbork": (50.9722, 18.2181),
    "Lubliniec": (50.6708, 18.6875),
    "Tarnowskie Góry": (50.4453, 18.8617),
    "Cieszyn": (49.7500, 18.6333),
    "Żywiec": (49.6853, 19.1922),
    "Andrychów": (49.8558, 19.3411),
    "Kraśnik": (50.9236, 22.2200),
    "Świdnik": (51.2233, 22.6961),
    "Lubartów": (51.4600, 22.6100),
    "Łuków": (51.9297, 22.3800),
    "Kutno": (52.2306, 19.3639),
    "Sieradz": (51.5958, 18.7300),
    "Wieluń": (51.2208, 18.5694),
    "Turek": (52.0167, 18.5000),
    "Koło": (52.2000, 18.6333),
    "Krotoszyn": (51.6944, 17.4364),
    "Jarocin": (51.9722, 17.5028),
    "Śrem": (52.0889, 17.0139),
    "Środa Wielkopolska": (52.2278, 17.2761),
    "Września": (52.3253, 17.5653),
    "Swarzędz": (52.4122, 17.0781),
    "Luboń": (52.3450, 16.8867),
    "Nowa Sól": (51.8033, 15.7167),
    "Żary": (51.6417, 15.1389),
    "Żagań": (51.6169, 15.3169),
    "Świebodzin": (52.2467, 15.5333),
    "Międzyrzecz": (52.4442, 15.5772),
    "Wałcz": (53.2733, 16.4706),
    "Choszczno": (53.1667, 15.4167),
    "Police": (53.5522, 14.5722),
    "Goleniów": (53.5636, 14.8283),
    "Gryfino": (53.2500, 14.4833),
    "Kołobrzeg": (54.1758, 15.5836),
    "Białogard": (54.0086, 15.9878),
    "Szczecinek": (53.7086, 16.6989),
    "Chojnice": (53.6958, 17.5578),
    "Starogard Gdański": (53.9667, 18.5333),
    "Malbork": (54.0361, 19.0417),
    "Kwidzyn": (53.7269, 18.9308),
    "Iława": (53.5958, 19.5675),
    "Ostróda": (53.6961, 19.9647),
    "Giżycko": (54.0378, 21.7625),
    "Kętrzyn": (54.0761, 21.3758),
    "Bartoszyce": (54.2522, 20.8083),
    "Mrągowo": (53.8703, 21.3050),
    "Szczytno": (53.5628, 20.9861),
    "Działdowo": (53.2350, 20.1800),
    "Mława": (53.1128, 20.3806),
    "Ciechanów": (52.8814, 20.6194),
    "Płońsk": (52.6236, 20.3778),
    "Sochaczew": (52.2294, 20.2400),
    "Żyrardów": (52.0489, 20.4456),
    "Grodzisk Mazowiecki": (52.1092, 20.6297),
    "Piaseczno": (52.0806, 21.0247),
    "Otwock": (52.1058, 21.2617),
    "Mińsk Mazowiecki": (52.1794, 21.5622),
    "Wołomin": (52.3428, 21.2431),
    "Wyszków": (52.5917, 21.4581),
    "Ostrów Mazowiecka": (52.7994, 21.8931),
    "Sokołów Podlaski": (52.4083, 22.2492),
    "Węgrów": (52.4000, 22.0167),
    "Garwolin": (51.8983, 21.6167),
    "Kozienice": (51.5828, 21.5439),
    "Grójec": (51.8636, 20.8672),
    "Sandomierz": (50.6819, 21.7492),
    "Jarosław": (50.0167, 22.6772),
    "Przeworsk": (50.0592, 22.4936),
    "Nisko": (50.5203, 22.1400),
    "Tarnobrzeg": (50.5731, 21.6794),
    "Leżajsk": (50.2617, 22.4200),
    "Łańcut": (50.0686, 22.2286),
    "Ropczyce": (50.0522, 21.6122),
    "Ustrzyki Dolne": (49.4300, 22.5931),
    "Lesko": (49.4700, 22.3300),
    "Brzozów": (49.6947, 22.0192),
    "Strzyżów": (49.8703, 21.7936),
    # --- NIEMCY ------------------------------------------------------------
    "Berlin": (52.5200, 13.4050),
    "Hamburg": (53.5511, 9.9937),
    "München": (48.1351, 11.5820),
    "Köln": (50.9375, 6.9603),
    "Frankfurt am Main": (50.1109, 8.6821),
    "Stuttgart": (48.7758, 9.1829),
    "Düsseldorf": (51.2277, 6.7735),
    "Leipzig": (51.3397, 12.3731),
    "Dortmund": (51.5136, 7.4653),
    "Essen": (51.4556, 7.0116),
    "Bremen": (53.0793, 8.8017),
    "Dresden": (51.0504, 13.7373),
    "Hannover": (52.3759, 9.7320),
    "Nürnberg": (49.4521, 11.0767),
    "Duisburg": (51.4344, 6.7623),
    "Bochum": (51.4818, 7.2162),
    "Wuppertal": (51.2562, 7.1508),
    "Bielefeld": (52.0302, 8.5325),
    "Bonn": (50.7374, 7.0982),
    "Münster": (51.9607, 7.6261),
    "Karlsruhe": (49.0069, 8.4037),
    "Mannheim": (49.4875, 8.4660),
    "Augsburg": (48.3705, 10.8978),
    "Wiesbaden": (50.0782, 8.2398),
    "Mönchengladbach": (51.1805, 6.4428),
    "Gelsenkirchen": (51.5177, 7.0857),
    "Braunschweig": (52.2689, 10.5268),
    "Kiel": (54.3233, 10.1228),
    "Chemnitz": (50.8278, 12.9214),
    "Aachen": (50.7753, 6.0839),
    "Halle": (51.4826, 11.9698),
    "Magdeburg": (52.1205, 11.6276),
    "Freiburg": (47.9990, 7.8421),
    "Krefeld": (51.3388, 6.5853),
    "Lübeck": (53.8655, 10.6866),
    "Erfurt": (50.9848, 11.0299),
    "Rostock": (54.0924, 12.0991),
    "Kassel": (51.3127, 9.4797),
    "Hagen": (51.3671, 7.4633),
    "Saarbrücken": (49.2402, 6.9969),
    "Potsdam": (52.3906, 13.0645),
    "Frankfurt (Oder)": (52.3412, 14.5506),
    "Cottbus": (51.7563, 14.3329),
    "Görlitz": (51.1520, 14.9871),
    "Regensburg": (49.0134, 12.1016),
    "Ingolstadt": (48.7665, 11.4258),
    "Würzburg": (49.7913, 9.9534),
    "Ulm": (48.4011, 9.9876),
    "Heilbronn": (49.1427, 9.2109),
    "Osnabrück": (52.2799, 8.0472),
    "Oldenburg": (53.1435, 8.2146),
    "Leverkusen": (51.0459, 6.9853),
    "Solingen": (51.1657, 7.0671),
    "Neuss": (51.1979, 6.6855),
    "Paderborn": (51.7189, 8.7575),
    "Darmstadt": (49.8728, 8.6512),
    "Mainz": (49.9929, 8.2473),
    "Heidelberg": (49.3988, 8.6724),
    "Hamm": (51.6739, 7.8150),
    "Wolfsburg": (52.4227, 10.7865),
    "Offenbach": (50.0956, 8.7761),
    "Siegen": (50.8748, 8.0243),
    "Koblenz": (50.3569, 7.5890),
    "Trier": (49.7490, 6.6371),
    "Jena": (50.9271, 11.5892),
    "Zwickau": (50.7189, 12.4961),
    "Schwerin": (53.6355, 11.4012),
    # --- CZECHY ------------------------------------------------------------
    "Praha": (50.0755, 14.4378),
    "Brno": (49.1951, 16.6068),
    "Ostrava": (49.8209, 18.2625),
    "Plzeň": (49.7384, 13.3736),
    "Liberec": (50.7663, 15.0543),
    "Olomouc": (49.5938, 17.2509),
    "České Budějovice": (48.9745, 14.4743),
    "Hradec Králové": (50.2093, 15.8328),
    "Ústí nad Labem": (50.6607, 14.0321),
    "Pardubice": (50.0343, 15.7812),
    "Zlín": (49.2265, 17.6706),
    "Havířov": (49.7798, 18.4368),
    "Kladno": (50.1477, 14.1028),
    "Most": (50.5031, 13.6362),
    "Opava": (49.9387, 17.9026),
    "Frýdek-Místek": (49.6833, 18.3500),
    "Karviná": (49.8540, 18.5416),
    "Jihlava": (49.3961, 15.5912),
    "Teplice": (50.6404, 13.8245),
    "Děčín": (50.7821, 14.2148),
    "Karlovy Vary": (50.2306, 12.8712),
    "Chomutov": (50.4605, 13.4178),
    "Jablonec nad Nisou": (50.7243, 15.1712),
    "Mladá Boleslav": (50.4114, 14.9030),
    "Prostějov": (49.4720, 17.1118),
    "Přerov": (49.4551, 17.4509),
    "Třebíč": (49.2149, 15.8819),
    "Česká Lípa": (50.6855, 14.5378),
    "Třinec": (49.6776, 18.6708),
    "Tábor": (49.4144, 14.6578),
    "Znojmo": (48.8555, 16.0488),
    "Příbram": (49.6899, 14.0104),
    "Cheb": (50.0796, 12.3742),
    # --- SŁOWACJA ----------------------------------------------------------
    "Bratislava": (48.1486, 17.1077),
    "Košice": (48.7164, 21.2611),
    "Prešov": (48.9975, 21.2393),
    "Žilina": (49.2231, 18.7394),
    "Nitra": (48.3069, 18.0864),
    "Banská Bystrica": (48.7395, 19.1533),
    "Trnava": (48.3774, 17.5877),
    "Trenčín": (48.8945, 18.0444),
    "Martin": (49.0665, 18.9216),
    "Poprad": (49.0614, 20.2972),
    "Prievidza": (48.7719, 18.6247),
    "Zvolen": (48.5746, 19.1259),
    "Považská Bystrica": (49.1214, 18.4239),
    "Michalovce": (48.7544, 21.9194),
    "Nové Zámky": (47.9857, 18.1620),
    "Spišská Nová Ves": (48.9447, 20.5619),
    "Komárno": (47.7639, 18.1292),
    "Levice": (48.2167, 18.6069),
    "Humenné": (48.9333, 21.9167),
    "Bardejov": (49.2925, 21.2761),
    "Liptovský Mikuláš": (49.0839, 19.6194),
    "Ružomberok": (49.0783, 19.3086),
    "Piešťany": (48.5936, 17.8267),
    "Lučenec": (48.3319, 19.6675),
    "Topoľčany": (48.5606, 18.1747),
    "Trebišov": (48.6272, 21.7181),
    "Rimavská Sobota": (48.3833, 20.0167),
}

# Prefiksy polskich kodów pocztowych (dwie pierwsze cyfry) -> środek obszaru.
#
# DLACZEGO TO JEST WARTE OSOBNEJ TABLICY: kod pocztowy w poście („38-400") jest
# jedyną informacją geograficzną, której autor NIE mógł napisać z literówką tak,
# żeby wskazała na inny kraj. Nazwę miasta model może przekręcić albo wyciągnąć
# z niewłaściwego zdania; pięć cyfr albo pasuje do wzorca, albo nie. Kod jest
# więc rozjemcą, gdy nazwa miasta nie trafia w słownik.
#
# Dokładność: środek obszaru, ±20-30 km. To jest ta sama klasa błędu co
# „nie wiem, czy odbiór jest po tej, czy po tamtej stronie miasta" — i dlatego
# `zrodlo` = 'kod_pocztowy' jest osobną wartością, a nie zlewa się z 'miasto'.
KODY_POCZTOWE: dict[str, tuple[float, float]] = {
    "00": (52.2297, 21.0122), "01": (52.2400, 20.9400), "02": (52.1900, 21.0000),
    "03": (52.2600, 21.0700), "04": (52.2200, 21.1300), "05": (52.1500, 20.9000),
    "06": (52.8800, 20.6200), "07": (52.8000, 21.6000), "08": (52.1000, 22.3000),
    "09": (52.5500, 19.7100),
    "10": (53.7784, 20.4801), "11": (53.9000, 20.4000), "12": (53.9500, 21.5000),
    "13": (53.3500, 20.2000), "14": (53.6000, 19.6000),
    "15": (53.1325, 23.1688), "16": (53.4000, 23.0000), "17": (52.7700, 23.2000),
    "18": (53.1800, 22.0600), "19": (54.0000, 22.6000),
    "20": (51.2465, 22.5684), "21": (51.3000, 22.7000), "22": (51.0000, 23.3000),
    "23": (51.0000, 22.2000), "24": (51.4200, 21.9700),
    "25": (50.8661, 20.6286), "26": (51.0000, 20.9000), "27": (50.8500, 21.5000),
    "28": (50.4700, 20.7200), "29": (51.1900, 20.4100),
    "30": (50.0647, 19.9450), "31": (50.0700, 20.0000), "32": (50.0500, 19.4000),
    "33": (49.7500, 20.7000), "34": (49.5000, 19.8000),
    "35": (50.0412, 21.9991), "36": (50.0500, 22.1000), "37": (50.1000, 22.6000),
    "38": (49.6886, 21.7647), "39": (50.2000, 21.5000),
    "40": (50.2649, 19.0238), "41": (50.3000, 19.0000), "42": (50.7971, 19.1204),
    "43": (49.8224, 19.0584), "44": (50.1500, 18.7000),
    "45": (50.6751, 17.9213), "46": (50.9700, 18.2200), "47": (50.3500, 18.2300),
    "48": (50.4700, 17.3300), "49": (50.5000, 17.6000),
    "50": (51.1079, 17.0385), "51": (51.1300, 17.0900), "52": (51.0900, 16.9500),
    "53": (51.0900, 16.9900), "54": (51.1200, 16.9500),
    "55": (51.0500, 16.7000), "56": (51.3000, 16.6000), "57": (50.5800, 16.4000),
    "58": (50.9000, 15.7200), "59": (51.2000, 16.1600),
    "60": (52.4064, 16.9252), "61": (52.4100, 16.9300), "62": (52.2000, 17.5000),
    "63": (51.9700, 17.5000), "64": (51.8400, 16.5700),
    "65": (51.9356, 15.5062), "66": (52.2500, 15.5000), "67": (51.6200, 15.3200),
    "68": (51.6400, 15.1400), "69": (52.4000, 15.0000),
    "70": (53.4285, 14.5528), "71": (53.4300, 14.5700), "72": (53.5500, 14.6000),
    "73": (53.3400, 15.0500), "74": (53.2500, 14.4800),
    "75": (54.1943, 16.1722), "76": (54.4641, 17.0287), "77": (53.9000, 17.0000),
    "78": (53.7000, 16.0000), "79": (53.5000, 15.5000),
    "80": (54.3520, 18.6466), "81": (54.5189, 18.5305), "82": (53.9600, 18.9000),
    "83": (54.1000, 18.2000), "84": (54.6000, 18.2400),
    "85": (53.1235, 18.0084), "86": (53.2000, 18.3000), "87": (53.0138, 18.5984),
    "88": (52.7900, 18.2600), "89": (53.3000, 17.5000),
    "90": (51.7592, 19.4560), "91": (51.7700, 19.4300), "92": (51.7400, 19.5000),
    "93": (51.7400, 19.4600), "94": (51.7600, 19.4200),
    "95": (51.8500, 19.4000), "96": (51.9550, 20.1417), "97": (51.4000, 19.7000),
    "98": (51.5900, 18.7300), "99": (52.2300, 19.3600),
}

# Nazwy, które w słowniku występują RAZ, ale w rzeczywistości oznaczają kilka
# miejscowości oddalonych o setki kilometrów. Trafienie w taką nazwę jest
# z definicji niepewne, nawet gdy jest dokładne.
#
# To NIE jest lista wszystkich powtarzalnych toponimów w Polsce (tych są tysiące)
# — to lista tych, które realnie padają w ogłoszeniach transportowych i przy
# których pomyłka kosztuje kilkaset kilometrów pustego przebiegu.
NAZWY_WIELOZNACZNE: frozenset[str] = frozenset({
    "brzeg", "nowe miasto", "nowa wies", "stara wies", "kamienica",
    "bielsko", "ostrow", "ostrowiec", "krosno", "gorzow", "swidnica",
    "frankfurt", "halle", "neustadt", "brno venkov",
})


# ---------------------------------------------------------------------------
# Normalizacja i dopasowanie
# ---------------------------------------------------------------------------
def _klucz(nazwa: str) -> str:
    """Nazwa miejsca do postaci porównywalnej: bez ogonków, bez interpunkcji.

    Ogonki lecą, bo połowa postów z grup jest pisana bez nich („Krakow", „Rzeszow")
    — a bramka widziała już dokładnie ten problem przy detekcji języka. Zapis
    kanoniczny w słowniku zostaje z diakrytykami, bo to on trafia do linku.
    """
    if not nazwa:
        return ""
    tekst = unicodedata.normalize("NFKD", str(nazwa).strip().lower())
    tekst = "".join(z for z in tekst if not unicodedata.combining(z))
    # Polskie ł nie ma formy rozłożonej — NFKD go nie ruszy, trzeba ręcznie.
    tekst = tekst.replace("ł", "l").replace("Ł", "l")
    tekst = re.sub(r"[^a-z0-9\s-]", " ", tekst)
    return re.sub(r"\s+", " ", tekst).strip()


# Nazwa publiczna dla wołających spoza modułu (dedup treściowy w powiadomieniach
# musi normalizować miasta DOKŁADNIE tak samo, inaczej crosspost „Krosno" vs
# „Krosno " dostanie dwa różne klucze i dwa alerty).
klucz_nazwy = _klucz

_INDEKS: dict[str, str] = {_klucz(n): n for n in MIASTA}

# Warianty, których nie da się wyprowadzić z normalizacji, bo to inne SŁOWA,
# nie inny zapis. Polskie egzonimy są tu obowiązkowe mimo instrukcji dla
# klasyfikatora („nazwy miejscowości zostają w oryginale") — instrukcja mówi,
# co ma zrobić MODEL, a tu wpada też tekst przepisany ręcznie przez człowieka
# z posta i wtedy „Monachium" jest normalną formą.
ALIASY: dict[str, str] = {
    "monachium": "München", "kolonia": "Köln", "drezno": "Dresden",
    "lipsk": "Leipzig", "norymberga": "Nürnberg", "hanower": "Hannover",
    "brema": "Bremen", "moguncja": "Mainz", "akwizgran": "Aachen",
    "ratyzbona": "Regensburg", "zgorzelec niemcy": "Görlitz",
    "praga": "Praha", "brno cz": "Brno", "ostrawa": "Ostrava",
    "pilzno cz": "Plzeň", "olomuniec": "Olomouc", "budziejowice": "České Budějovice",
    "bratyslawa": "Bratislava", "koszyce": "Košice", "preszow": "Prešov",
    "zylina": "Žilina", "trnawa": "Trnava", "poprad sk": "Poprad",
    "warszawa mazowieckie": "Warszawa", "stolica": "Warszawa",
    "trojmiasto": "Gdańsk", "gdansk gdynia sopot": "Gdańsk",
    "gora kalwaria": "Piaseczno", "katowice slask": "Katowice",
    "frankfurt nad menem": "Frankfurt am Main",
    "frankfurt nad odra": "Frankfurt (Oder)",
    "bielsko biala": "Bielsko-Biała", "jastrzebie zdroj": "Jastrzębie-Zdrój",
    "kedzierzyn kozle": "Kędzierzyn-Koźle", "ruda slaska": "Ruda Śląska",
    "dabrowa gornicza": "Dąbrowa Górnicza", "zielona gora": "Zielona Góra",
    "jelenia gora": "Jelenia Góra", "nowy sacz": "Nowy Sącz",
    "gorzow wlkp": "Gorzów Wielkopolski", "ostrow wlkp": "Ostrów Wielkopolski",
    "srodda wlkp": "Środa Wielkopolska", "piotrkow tryb": "Piotrków Trybunalski",
    "tomaszow maz": "Tomaszów Mazowiecki", "minsk maz": "Mińsk Mazowiecki",
    "ostrow maz": "Ostrów Mazowiecka", "grodzisk maz": "Grodzisk Mazowiecki",
}


def _trojki(tekst: str) -> set[str]:
    """Zbiór trójek znaków — miara podobieństwa odporna na literówkę w środku."""
    t = f"  {tekst} "
    return {t[i:i + 3] for i in range(len(t) - 2)}


def _podobienstwo(a: str, b: str) -> float:
    """Jaccard na trójkach znaków: 1.0 = identyczne, 0.0 = nic wspólnego.

    Świadomie NIE `difflib.SequenceMatcher`: ten ostatni jest kwadratowy i przy
    kilkuset miastach w pętli po każdej liście zleceń zaczyna być widoczny
    w czasie odpowiedzi API. Trójki liczą się raz i porównują przez przecięcie
    zbiorów.
    """
    ta, tb = _trojki(a), _trojki(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass(frozen=True)
class Punkt:
    """Miejsce z posta sprowadzone do współrzędnych — razem z tym, SKĄD je znamy.

    `zrodlo` jest równie ważne co `lat`/`lon` i dlatego nie ma wartości domyślnej:
    punkt bez informacji o wiarygodności zachęca do pokazania kilometrów tak,
    jakby były pewne.
    """

    lat: float
    lon: float
    nazwa: str          # forma kanoniczna — ta idzie do linku z mapą
    zrodlo: str         # 'miasto' | 'kod_pocztowy' | 'miasto_niepewne' | 'brak'
    surowe: str = ""    # to, co realnie stało w poście (do paska ostrzegawczego)

    @property
    def pewne(self) -> bool:
        return self.zrodlo in ("miasto", "kod_pocztowy")


def _z_kodu(kod: str) -> tuple[float, float] | None:
    """Współrzędne z polskiego kodu pocztowego. Przyjmuje '38-400' i '38400'."""
    if not kod:
        return None
    cyfry = re.sub(r"\D", "", str(kod))
    if len(cyfry) < 2:
        return None
    return KODY_POCZTOWE.get(cyfry[:2])


def _oczysc_nazwe(miejsce: str) -> str:
    """Zdejmij z nazwy to, co dokleja do niej człowiek piszący ogłoszenie.

    „okolice Krosna", „Krosno i okolice", „pod Rzeszowem", „38-400 Krosno" —
    wszystkie te formy padają w postach i wszystkie mają w środku nazwę, która
    jest w słowniku. Bez tego kroku każda z nich schodziłaby na dopasowanie
    rozmyte i lądowała jako 'miasto_niepewne', czyli z ostrzeżeniem tam, gdzie
    naprawdę wiemy, o co chodzi.
    """
    tekst = _klucz(miejsce)
    tekst = re.sub(r"\b\d{2}[-\s]?\d{3}\b", " ", tekst)          # kod pocztowy
    tekst = re.sub(r"\b(okolice|okolica|okolic|pod|kolo|k|niedaleko|"
                   r"blisko|obok|rejon|powiat|gmina|wojewodztwo|woj|"
                   r"i okolice|centrum|nahe|bei|okoli|pri)\b", " ", tekst)
    # Miejscownik i dopełniacz: „Krosna" -> „Krosno" nie da się zrobić regułą
    # bez słownika odmiany, więc końcówki tniemy dopiero w dopasowaniu rozmytym
    # (trójki znaków radzą sobie z jedną literą różnicy).
    return re.sub(r"\s+", " ", tekst).strip()


def wspolrzedne(miejsce: str, kod: str = "") -> Punkt:
    """Nazwa (i opcjonalnie kod pocztowy) -> punkt na mapie z oceną pewności.

    KOLEJNOŚĆ JEST CELOWA i wynika z tego, co da się sfałszować literówką:

    1. Dokładne trafienie nazwy w słowniku — najlepsze, co możemy mieć.
    2. Alias (egzonim, skrót „Gorzów Wlkp") — to samo, tylko przez tablicę.
    3. Kod pocztowy — gdy nazwa nie trafiła. Pięć cyfr albo pasuje, albo nie;
       nie da się ich przekręcić tak, żeby wskazały inny kraj.
    4. Dopasowanie rozmyte — ostatnia deska, ZAWSZE 'miasto_niepewne'.

    ROZBIEŻNOŚĆ NAZWY I KODU (punkt 1 + kod wskazujący gdzie indziej) degraduje
    wynik do 'miasto_niepewne'. To jest rzadkie, ale gdy się zdarza, znaczy, że
    model skleił dwa różne zdania posta — a wtedy operator ma o tym wiedzieć,
    zamiast dostać pewną liczbę zbudowaną z dwóch niezgodnych przesłanek.
    """
    surowe = (miejsce or "").strip()
    oczyszczone = _oczysc_nazwe(surowe)
    z_kodu = _z_kodu(kod) or _z_kodu(surowe)

    kanoniczna = _INDEKS.get(oczyszczone) or ALIASY.get(oczyszczone)
    if kanoniczna:
        lat, lon = MIASTA[kanoniczna]
        zrodlo = ("miasto_niepewne" if oczyszczone in NAZWY_WIELOZNACZNE
                  else "miasto")
        if z_kodu:
            zgadza_sie = dystans_km(lat, lon, z_kodu[0], z_kodu[1]) <= 60
            # KOD JEST ROZJEMCĄ W OBIE STRONY i to jest jego cały sens.
            # Gdy potwierdza nazwę, ZDEJMUJE ostrzeżenie z nazwy wieloznacznej:
            # „Krosno" samo w sobie może być tym pod Rzeszowem albo Odrzańskim,
            # ale „38-400 Krosno" nie może być niczym innym. Ostrzeganie mimo
            # zgodnego kodu to fałszywy alarm przy zleceniu, o którym wiemy
            # wszystko — a operator, który raz zignoruje ostrzeżenie, będzie je
            # ignorował także wtedy, gdy będzie prawdziwe.
            # Gdy przeczy — schodzimy na 'niepewne', bo jedna z dwóch przesłanek
            # jest błędna i nie wiemy która.
            zrodlo = "miasto" if zgadza_sie else "miasto_niepewne"
        return Punkt(lat, lon, kanoniczna, zrodlo, surowe)

    if z_kodu:
        return Punkt(z_kodu[0], z_kodu[1], surowe or "kod pocztowy",
                     "kod_pocztowy", surowe)

    if oczyszczone:
        najlepszy, wynik = "", 0.0
        for klucz, nazwa in _INDEKS.items():
            p = _podobienstwo(oczyszczone, klucz)
            if p > wynik:
                najlepszy, wynik = nazwa, p
        if wynik >= MIN_PODOBIENSTWO:
            lat, lon = MIASTA[najlepszy]
            return Punkt(lat, lon, najlepszy, "miasto_niepewne", surowe)

    return Punkt(0.0, 0.0, surowe, "brak", surowe)


# ---------------------------------------------------------------------------
# Dystans
# ---------------------------------------------------------------------------
def dystans_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine — odległość w LINII PROSTEJ. Do pokazania człowiekowi użyj
    `droga_km`, bo laweta jedzie drogami."""
    fi1, fi2 = math.radians(lat1), math.radians(lat2)
    dfi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dfi / 2) ** 2
         + math.cos(fi1) * math.cos(fi2) * math.sin(dlambda / 2) ** 2)
    return 2 * PROMIEN_ZIEMI_KM * math.asin(math.sqrt(a))


def droga_km(a: Punkt, b: Punkt) -> int | None:
    """Szacunek kilometrów DO PRZEJECHANIA między dwoma punktami.

    None, gdy któregokolwiek punktu nie znamy — i to jest wartość poprawna,
    nie błąd. „Nie wiem" pokazane operatorowi jest uczciwe; zero albo losowa
    liczba udająca wiedzę już nie.
    """
    if a.zrodlo == "brak" or b.zrodlo == "brak":
        return None
    return round(dystans_km(a.lat, a.lon, b.lat, b.lon) * WSPOLCZYNNIK_DROGOWY)


def baza_ustawiona() -> bool:
    """Czy `BAZA_LAT`/`BAZA_LON` mają sens. (0, 0) to punkt w Zatoce Gwinejskiej
    — czyli wartość domyślna, a nie współrzędne czyjejś bazy."""
    return bool(settings.BAZA_LAT or settings.BAZA_LON)


def punkt_bazy() -> Punkt:
    return Punkt(settings.BAZA_LAT, settings.BAZA_LON, "baza",
                 "miasto" if baza_ustawiona() else "brak")


def km_od_bazy(miejsce: str, kod: str = "") -> int | None:
    """Kilometry z bazy do punktu odbioru. None = nie da się policzyć.

    To jest liczba, na którą operator patrzy jako na drugą w kolejności (po
    pilności) — i jedyna, którą łatwo pomylić z filtrem. NIE JEST FILTREM.
    """
    return droga_km(punkt_bazy(), wspolrzedne(miejsce, kod))


# ---------------------------------------------------------------------------
# Wycena — szacunek, nie cennik
# ---------------------------------------------------------------------------
def szacunek_pln(km_dojazd: int | None, km_trasy: int | None = None) -> int | None:
    """Ile ten kurs może być wart. Liczba do TRIAŻU, nie do podania klientowi.

    Wzór jest celowo prymitywny: opłata startowa + stawka za każdy kilometr
    dojazdu i trasy. Nie uwzględnia masy, pory, autostrad ani tego, że wracasz
    pusty — bo żadnej z tych rzeczy nie wiemy z posta, a wzór, który udaje, że
    wie, produkuje liczby wyglądające na wiążące.

    Ta liczba istnieje po to, żeby odróżnić kurs za 200 zł od kursu za 2000 zł
    w dwie sekundy patrzenia na ekran. Cenę podaje kierowca przez telefon.
    """
    if km_dojazd is None:
        return None
    km = km_dojazd + (km_trasy or 0)
    return int(round(settings.OPLATA_STARTOWA_PLN + km * settings.STAWKA_ZA_KM_PLN))


# ---------------------------------------------------------------------------
# Linki — to, w co operator realnie klika
# ---------------------------------------------------------------------------
def _cel(punkt: Punkt) -> str:
    """Co wpisać do linku: nazwę czy współrzędne.

    Nazwa jest LEPSZA, gdy pochodzi ze słownika — Google znajdzie centrum miasta
    dokładniej niż nasz punkt z tablicy. Przy kodzie pocztowym i dopasowaniu
    rozmytym idą współrzędne, bo wysłanie do Google przekręconej nazwy kończy się
    trafieniem w zupełnie inne miejsce, i to bez żadnego objawu.
    """
    if punkt.zrodlo == "miasto":
        return punkt.nazwa
    return f"{punkt.lat:.5f},{punkt.lon:.5f}"


def link_do_map(miasto_od: str, miasto_do: str = "", kod_od: str = "",
                kod_do: str = "") -> str:
    """POKAŻ trasę: baza -> odbiór -> dostawa. Do oceny zlecenia przed telefonem.

    Punkt odbioru idzie jako `waypoints`, a nie jako `origin`, bo operator startuje
    z bazy i chce zobaczyć CAŁY przebieg — łącznie z dojazdem, który jest zwykle
    połową kosztu. Trasa licząca się od miejsca odbioru pokazuje kurs tańszym,
    niż jest.

    Gdy bazy nie ustawiono, trasa zaczyna się od punktu odbioru. Link bez `origin`
    Google uzupełnia bieżącą pozycją telefonu, co jest sensownym zachowaniem
    zapasowym, a nie błędem.
    """
    od = wspolrzedne(miasto_od, kod_od)
    do = wspolrzedne(miasto_do, kod_do) if miasto_do else od

    czesci = ["https://www.google.com/maps/dir/?api=1", "travelmode=driving"]
    if baza_ustawiona():
        czesci.append(f"origin={settings.BAZA_LAT:.5f},{settings.BAZA_LON:.5f}")
        if od.zrodlo != "brak" and do is not od and do.zrodlo != "brak":
            czesci.append("waypoints=" + quote(_cel(od)))
    cel = do if do.zrodlo != "brak" else od
    if cel.zrodlo == "brak":
        # Nie znamy żadnego punktu — oddajemy wyszukiwanie po surowym tekście
        # zamiast linku, który zaprowadzi w losowe miejsce.
        return ("https://www.google.com/maps/search/?api=1&query="
                + quote(miasto_od or miasto_do or ""))
    czesci.append("destination=" + quote(_cel(cel)))
    return "&".join(czesci)


def link_nawigacji(miasto_od: str, kod_od: str = "") -> str:
    """JEDŹ do punktu odbioru — link uruchamia nawigację, nie podgląd.

    `dir_action=navigate` startuje prowadzenie od razu, bez ekranu podsumowania.
    Świadomie zwykły https, a nie intent `google.navigation:q=` — intent działa
    wyłącznie na Androidzie i wyłącznie z aplikacji natywnej, a ten link jest
    klikany w Telegramie i w przeglądarce na iOS, gdzie po prostu by nie zadziałał.
    """
    od = wspolrzedne(miasto_od, kod_od)
    if od.zrodlo == "brak":
        return ("https://www.google.com/maps/search/?api=1&query="
                + quote(miasto_od or ""))
    return ("https://www.google.com/maps/dir/?api=1&travelmode=driving"
            "&dir_action=navigate&destination=" + quote(_cel(od)))


# ---------------------------------------------------------------------------
# Jedno wywołanie dla wołających — powiadomienie i API pytają o to samo
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Geo:
    """Komplet geografii jednego zlecenia. Powiadomienie (prompt 5) i API
    (prompt 6) liczą DOKŁADNIE to samo, więc liczą to w jednym miejscu —
    inaczej panel pokazywałby inne kilometry niż alert na telefonie i nikt
    nie wiedziałby, która liczba jest prawdziwa."""

    km_od_bazy: int | None
    km_trasy: int | None
    szacunek_pln: int | None
    link_mapy: str
    link_nawigacji: str
    zrodlo: str              # pewność punktu ODBIORU — to nim jedzie laweta
    miejsce_od: str          # forma kanoniczna albo surowa, gdy nie rozpoznano
    miejsce_do: str
    surowe_od: str           # co realnie stało w poście — do paska ostrzegawczego
    # Współrzędne punktu ODBIORU — dla pinezki na mapie w panelu. `None` przy
    # `zrodlo == 'brak'`, i to jest jedyny poprawny wynik: pinezka postawiona
    # „gdzieś" jest gorsza niż jej brak, bo wygląda tak samo jak pinezka pewna.
    # Wyciągane z linku po stronie panelu NIE działa — link dla dokładnego
    # trafienia niesie NAZWĘ miasta (Google znajdzie centrum lepiej niż nasza
    # tablica), więc akurat najlepiej rozpoznane zlecenia zostawałyby bez pinezki.
    lat: float | None = None
    lon: float | None = None


def opisz(zlecenie: dict) -> Geo:
    """Policz geografię ze słownika zlecenia. Nie dotyka bazy ani sieci.

    Przyjmuje zarówno wynik klasyfikatora, jak i wiersz z `posty` rozwinięty
    o `ai_json` — klucze są te same, bo to ten sam kontrakt.
    """
    miasto_od = str(zlecenie.get("miasto_od") or zlecenie.get("miejsce") or "")
    miasto_do = str(zlecenie.get("miasto_do") or "")
    kod_od = str(zlecenie.get("kod_pocztowy") or zlecenie.get("kod_od") or "")
    kod_do = str(zlecenie.get("kod_do") or "")

    od = wspolrzedne(miasto_od, kod_od)
    do = wspolrzedne(miasto_do, kod_do) if miasto_do else None

    km_dojazd = droga_km(punkt_bazy(), od)
    km_trasy = droga_km(od, do) if do is not None else None

    return Geo(
        km_od_bazy=km_dojazd,
        km_trasy=km_trasy,
        szacunek_pln=szacunek_pln(km_dojazd, km_trasy),
        link_mapy=link_do_map(miasto_od, miasto_do, kod_od, kod_do),
        link_nawigacji=link_nawigacji(miasto_od, kod_od),
        zrodlo=od.zrodlo,
        miejsce_od=od.nazwa or miasto_od,
        miejsce_do=(do.nazwa if do is not None else "") or miasto_do,
        surowe_od=od.surowe or miasto_od,
        lat=None if od.zrodlo == "brak" else od.lat,
        lon=None if od.zrodlo == "brak" else od.lon,
    )


# ---------------------------------------------------------------------------
# CLI — „czemu to zlecenie pokazuje 900 km". Diagnostyka dopasowania nazwy
# bez odpalania pipeline'u:
#   python -m laweta_radar.services.geo Krosno Rzeszów
#   python -m laweta_radar.services.geo "38-400 Krosno"
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("użycie: python -m laweta_radar.services.geo <miejsce> [miejsce_do]")
        return 0
    baza = punkt_bazy()
    print(f"baza: {baza.lat}, {baza.lon} "
          f"({'ustawiona' if baza_ustawiona() else 'BRAK — ustaw BAZA_LAT/BAZA_LON'})")
    wynik = opisz({"miasto_od": argv[1], "miasto_do": argv[2] if len(argv) > 2 else ""})
    print(f"odbiór:  {wynik.miejsce_od}  [{wynik.zrodlo}]")
    if wynik.miejsce_do:
        print(f"dostawa: {wynik.miejsce_do}")
    print(f"dojazd:  {wynik.km_od_bazy} km")
    print(f"trasa:   {wynik.km_trasy} km")
    print(f"szacunek: {wynik.szacunek_pln} zł")
    print(f"mapa:    {wynik.link_mapy}")
    print(f"nawiguj: {wynik.link_nawigacji}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
