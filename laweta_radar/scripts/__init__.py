"""Narzędzia URUCHAMIANE RĘCZNIE — nie są częścią pipeline'u i nie chodzą z crona.

Różnica wobec `workers/` jest celowa i warto ją utrzymać: worker odpala się sam,
co kilka minut, i ma zasadę „brak konfiguracji = ciche, czyste wyjście". Skrypt
z tego katalogu odpala CZŁOWIEK, świadomie i zwykle raz — więc wolno mu zadać
pytanie, poczekać na odpowiedź i wypisać ścianę tekstu. Za to musi powiedzieć,
ile będzie kosztował, ZANIM cokolwiek wyda.
"""
