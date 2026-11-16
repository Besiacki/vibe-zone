# Starter / Baseline

## Polski

Twoje zadanie: przyspiesz mini social API, zachowując [kontrakt API](../docs/API.md). Możesz przepisać całość w Rust, Go, C, Pythonie lub dowolnym innym języku. SQLite pozostaje trwałym magazynem danych. AI do kodowania jest dozwolone.

### Uruchomienie

Z katalogu głównego repozytorium:

```sh
docker build --platform linux/amd64 -t vibezone-starter:local starter
docker pull --platform linux/amd64 python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89
docker pull --platform linux/amd64 grafana/k6@sha256:f21270290d702cbf0a7d6ba5d7ed100b63bcb233b558b885ed787547b3488279
docker run -d --name vz-starter --platform linux/amd64 \
  --cpus 1 --memory 2g --memory-swap 2g --pids-limit 512 \
  -e PORT=8080 -e DATA_DIR=/data -p 127.0.0.1:8080:8080 \
  -v vz-starter-data:/data vibezone-starter:local
curl http://localhost:8080/health
python3 bench/tests/api_check.py http://localhost:8080
bench/run_bench.sh vibezone-starter:local --pull never --smoke
```

Testy HTTP zapisują dane. Runner zawsze tworzy własne świeże volumes, sprawdza SIGKILL/restart, a po zakończeniu usuwa swoje kontenery, sieć i dane. Wyniki zostawia w `results/`.

### Kontrakt kontenera i zasady

- Obraz `linux/amd64`, serwer na `0.0.0.0:$PORT` (domyślnie 8080), `/health` gotowe w 30 s.
- SQLite w `$DATA_DIR` (domyślnie `/data`), obsługa zapisywalnego named volume; nie wymagaj innych usług.
- Potwierdzone posty i like'i muszą przetrwać wymuszone zatrzymanie. Kolejny feed widzi zapis natychmiast, z uwzględnieniem limitu 20 najnowszych postów.
- 1 vCPU, 2 GiB RAM, 512 procesów, UID/GID 10001:10001. Rootfs tylko do odczytu; zapis wyłącznie w /data (≤1 GiB) i /tmp (64 MiB, noexec). W benchmarku brak internetu, capabilities, własnego healthchecka i dodatkowych wolumenów.
- Obraz **< 100 MB bez kompresji**, cel **< 50 MB**; MB = 1 000 000 bajtów. Wariant `docker save | gzip` raportowany dodatkowo.
- Wynik to mediana najwyższych zaliczonych poziomów VU z trzech pomiarów, przy 0% błędów, p99 < 500 ms i minimalnej liczbie żądań; szczegóły w [SCORING.md](../docs/SCORING.md). Przy remisie: mniejszy obraz, niższa mediana cold start, niższe p99 na zaliczonym poziomie.
- Nie hardkoduj danych ani odpowiedzi. Testy używają losowych danych, sprawdzają kolejność, walidację, współbieżną idempotencję i trwałość.

### Miejsca do poprawy - celowe antyoptymalizacje

1. Gunicorn: **jeden synchroniczny worker**, bez async i strojenia keep-alive.
2. **N+1** w feedzie: jedno SELECT postów i do 20 osobnych COUNT po likes.
3. **Brak dodatkowych indeksów**, w szczególności `likes(post_id)`. Jedyny klucz podstawowy to ID posta. Idempotencję zapewnia transakcja `BEGIN IMMEDIATE` + skan pary, nie indeks UNIQUE.
4. **Bez WAL**, domyślny rollback journal SQLite i `synchronous=FULL`.
5. **Nowe połączenie SQLite na żądanie**, brak poolingu i cache.

Baseline używa `sqlite3` bez ORM: łatwiej zobaczyć koszt zapytań i zmieścić się poniżej 50 MB. Dockerfile wywodzi runtime z `python:3.12-alpine`, usuwa pip/nagłówki i zbędne moduły narzędziowe, po czym kopiuje system plików do końcowego etapu `scratch`. Python, Flask, gunicorn i SQLite pozostają; usunięte pliki nie zajmują miejsca w starych warstwach obrazu.

### Zgłoszenie

Fork  >  zmiana  >  build  >  testy  >  publiczny obraz GHCR/Docker Hub  >  zaproszenie i zgłoszenie  >  zatwierdzenie  >  batch co 15 min. [Polecenia krok po kroku](../docs/PARTICIPANT_GUIDE.pl.md). Ranking: https://devfest.atlasapx.pl/leaderboard; obecnie tylko wyniki testowe startera. Pełne pomiary uczestników wymagają osobnych runnerów.

Uzyskaj jednorazowe zaproszenie od organizatora. Wpisz nick/zespół, zaproszenie i publiczny obraz Docker albo adres repo GitHub/GitLab. Zapisz otrzymany kod zespołu do aktualizacji. Organizator przygotowuje obraz poza hostem pomiarowym i zatwierdza lokalne immutable ID; każda nowa wersja wymaga ponownego zatwierdzenia. Nie ma automatycznego pull/build ani odświeżania tagów. Przegląd kodu finalistów jest wymagany przed nagrodami.

## English

Build the fastest mini social API while preserving the [API contract](../docs/API.md). Rewrite it in **any language**: Rust, Go, C, Python, or another stack. SQLite must remain the durable data store. AI coding tools are welcome.

Use the build/run commands above from the repository root. The HTTP checker works against any URL and writes test data. `bench/run_bench.sh IMAGE --pull never --smoke` runs an isolated local check, including a forced kill/restart. The full profile requires a prepared native amd64 worker and a bounded disk volume. Automatic pulls and repository builds are disabled. Smoke uses bounded tmpfs storage and is diagnostic only.

### Container contract and rules

- Target **linux/amd64**; listen on `0.0.0.0:$PORT` (8080 by default). `/health` must become ready within 30 seconds.
- Store SQLite under `$DATA_DIR` (`/data` by default), backed by a writable Docker volume. No external services.
- Every acknowledged write must survive a forced container kill. The next feed must reflect committed writes, subject to the newest-20 limit.
- Resource limits: **1 vCPU, 2 GiB RAM, 512 processes**, UID/GID **10001:10001**, read-only root filesystem, writable `/data` (≤1 GiB) and `/tmp` (64 MiB, noexec). No internet, extra capabilities, extra declared volumes or image healthcheck.
- Image size must be **under 100 MB uncompressed**, with a target **under 50 MB**. MB means 1,000,000 bytes. The gzipped Docker archive size is also recorded.
- Ranking: median of three highest qualifying VU levels with **0% errors, p99 < 500 ms**, completed integrity checks and a minimum request count. Exact ties share a place. See [SCORING.md](../docs/SCORING.md). Ties: smaller image, lower median cold start, lower p99 at the winning level.
- Do not hardcode responses. Tests include randomized content, validation, ordering, concurrent duplicate likes and crash durability.

### Deliberate optimization opportunities

The starter uses a single synchronous Gunicorn worker, N+1 feed queries, no extra database indexes, rollback journaling instead of WAL, FULL synchronization, a new SQLite connection per request, and no cache. A `BEGIN IMMEDIATE` transaction serializes the duplicate-like check and insert without a UNIQUE index. `sqlite3` is used directly to keep the bottlenecks visible.

The runtime is built from `python:3.12-alpine`, with packaging tools and unused development files removed. A final `scratch` stage copies the pruned filesystem so removed files do not remain in image layers. Flask, Gunicorn, Python and SQLite still run normally.

### Submit

Fork  >  change  >  build  >  test  >  public GHCR/Docker Hub image  >  invitation and submission  >  approval  >  batch every 15 minutes. [Step-by-step commands](../docs/PARTICIPANT_GUIDE.md). Leaderboard: https://devfest.atlasapx.pl/leaderboard; currently starter test results only. Participant competition measurements require separate runners.

Obtain a one-use invitation from the organizer, then submit your team name and a public image/repository reference. Save the separate team update code. The organizer prepares and reviews the image on isolated infrastructure and approves its immutable local ID. Every new submission needs approval; tags are not automatically refreshed. Each batch remeasures approved images three times; no results are cached across batches. Finalists must provide source code for review.
