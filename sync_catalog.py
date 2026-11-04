#!/usr/bin/env python3
"""Sincroniza o catálogo remoto para SQLite; capas podem ser espelhadas localmente."""
from __future__ import annotations

import argparse
from collections import Counter
from difflib import SequenceMatcher
import json
import os
import re
import sqlite3
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()
CATALOGO = os.getenv("SAIMO_CATALOGO", "https://filmes-saimo.pages.dev").rstrip("/")
DB = Path(os.getenv("SAIMO_DB", "./data/catalog.db")).expanduser()
POSTERS = Path(os.getenv("SAIMO_POSTERS", "./static/posters")).expanduser()
UA = {"User-Agent": "Mozilla/5.0 (compatible; saimo-local/1.0)"}
HTTP = requests.Session()
_missing_optional_feed = False
TMDB_TOKEN = os.getenv("SAIMO_TMDB_TOKEN", "").strip()
TMDB_API_KEY = os.getenv("SAIMO_TMDB_API_KEY", "").strip()


def normalizar_titulo(value: str) -> str:
    """Chave de comparação para títulos com diferenças de acento e pontuação."""
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", value))


def tipo_catalogo(value: str) -> str | None:
    value = normalizar_titulo(value).replace(" ", "")
    if value in {"s", "serie", "series", "tv", "show", "shows"}:
        return "s"
    if value in {"f", "filme", "filmes", "movie", "movies", "film"}:
        return "f"
    if value.startswith(("serie", "tv", "show")):
        return "s"
    if value.startswith(("filme", "movie", "film")):
        return "f"
    return None


def campo_poster(columns: list[str]) -> str:
    """Aceita a capa na coluna esperada e variações do formato do feed."""
    for value in columns[2:]:
        value = value.strip()
        lower = value.lower()
        if lower.startswith(("http://", "https://")):
            path = urlparse(value).path.lower()
            if "image.tmdb.org" in lower or path.endswith((".jpg", ".jpeg", ".png", ".webp")):
                return value
        elif value.startswith("/") and value.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            return value
    return columns[3].strip() if len(columns) > 3 else ""


def diagnosticar_fichas() -> None:
    lines = [line for line in texto("fichas.txt").splitlines() if line.strip()]
    print(f"linhas úteis em fichas.txt: {len(lines)}")
    shapes: dict[int, int] = {}
    for line in lines:
        shapes[len(line.split("\t"))] = shapes.get(len(line.split("\t")), 0) + 1
    print(f"quantidade de colunas TSV: {dict(sorted(shapes.items()))}")
    for index, line in enumerate(lines[:5], start=1):
        fields = line.split("\t")
        preview = [field.strip()[:100] for field in fields[:8]]
        print(f"amostra {index}: {preview!r}")
        if line.lstrip().startswith("{"):
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    print(f"chaves JSON: {sorted(record.keys())}")
            except ValueError:
                pass


def resolver_poster_tmdb(tipo: str, titulo: str, ano: str) -> tuple[str | None, bool]:
    """Busca o pôster no TMDb e informa se a resposta pode ser guardada em cache."""
    if not TMDB_TOKEN and not TMDB_API_KEY:
        return None, False
    endpoint = "tv" if tipo == "s" else "movie"
    search_title = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", titulo).strip()
    search_title = re.sub(r"^#[^:]{1,15}:\s*", "", search_title)
    params: dict[str, str] = {"query": search_title, "language": "pt-BR", "include_adult": "false"}
    year = next((part for part in re.findall(r"\b(?:19|20)\d{2}\b", ano or titulo) if part), "")
    if year:
        params["first_air_date_year" if tipo == "s" else "year"] = year
    headers = {**UA, "accept": "application/json"}
    if TMDB_TOKEN:
        headers["Authorization"] = f"Bearer {TMDB_TOKEN}"
    else:
        params["api_key"] = TMDB_API_KEY
    results = []
    for attempt in range(3):
        try:
            response = requests.get(f"https://api.themoviedb.org/3/search/{endpoint}",
                                    params=params, headers=headers, timeout=(5, 12))
            if response.status_code in (401, 403):
                raise RuntimeError("TMDb recusou a credencial; confira SAIMO_TMDB_TOKEN/API_KEY")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 2:
                    return None, False
                retry_after = response.headers.get("Retry-After", "")
                if attempt < 2:
                    time.sleep(min(15, int(retry_after)) if retry_after.isdigit() else 2 ** attempt)
                continue
            response.raise_for_status()
            results = response.json().get("results", [])
            break
        except (requests.RequestException, ValueError, AttributeError):
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                return None, False

    query = normalizar_titulo(search_title)
    query_tokens = set(query.split())
    best: tuple[float, str] | None = None
    for result in results:
        poster_path = result.get("poster_path")
        candidate_titles = ([result.get("name", ""), result.get("original_name", "")]
                            if tipo == "s" else
                            [result.get("title", ""), result.get("original_title", "")])
        candidate_titles = [value for value in candidate_titles if value]
        if not poster_path or not candidate_titles:
            continue
        score = 0.0
        for candidate_title in candidate_titles:
            candidate = normalizar_titulo(candidate_title)
            candidate_tokens = set(candidate.split())
            overlap = len(query_tokens & candidate_tokens) / max(1, len(query_tokens))
            score = max(score, SequenceMatcher(None, query, candidate).ratio(), overlap)
        if best is None or score > best[0]:
            best = score, poster_path
    poster = f"https://image.tmdb.org/t/p/w342/{best[1].lstrip('/')}" if best and best[0] >= 0.5 else None
    return poster, True


def buscar_capas_tmdb(con: sqlite3.Connection) -> None:
    if not TMDB_TOKEN and not TMDB_API_KEY:
        print("erro: configure SAIMO_TMDB_TOKEN ou SAIMO_TMDB_API_KEY no .env", flush=True)
        return
    missing = con.execute(
        "SELECT id,tipo,titulo,ano FROM items WHERE poster IS NULL OR poster='' ").fetchall()
    cache_rows = {row["chave"]: row["poster"] for row in con.execute(
        "SELECT chave,poster FROM tmdb_poster_cache")}
    lookup_groups: dict[str, list[sqlite3.Row]] = {}
    for row in missing:
        cache_key = "|".join((row["tipo"], normalizar_titulo(row["titulo"]), row["ano"] or ""))
        if cache_key not in cache_rows:
            lookup_groups.setdefault(cache_key, []).append(row)
        elif cache_rows[cache_key]:
            con.execute("UPDATE items SET poster=? WHERE id=?", (cache_rows[cache_key], row["id"]))
    con.commit()
    cached = len(missing) - sum(map(len, lookup_groups.values()))
    new_by_type = Counter(key.split("|", 1)[0] for key in lookup_groups)
    print(f"fallback TMDb: {len(lookup_groups)} buscas novas; {cached} itens já avaliados/cacheados",
          flush=True)
    print("fallback TMDb por tipo: " + ", ".join(
        f"{'séries' if kind == 's' else 'filmes'}={count}" for kind, count in sorted(new_by_type.items())),
        flush=True)

    def lookup(group):
        cache_key, rows = group
        representative = rows[0]
        poster, cacheable = resolver_poster_tmdb(
            representative["tipo"], representative["titulo"], representative["ano"] or "")
        return cache_key, poster, cacheable, [row["id"] for row in rows]

    resolved = completed = failed = 0
    resolved_by_type: Counter[str] = Counter()
    groups = list(lookup_groups.items())
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(lookup, group) for group in groups]
        for future in as_completed(futures):
            try:
                cache_key, poster, cacheable, item_ids = future.result()
            except RuntimeError:
                raise
            except Exception as exc:
                failed += 1
                completed += 1
                if failed == 1:
                    print(f"aviso: uma busca TMDb falhou ({type(exc).__name__}); continuando as demais",
                          flush=True)
                if completed % 25 == 0 or completed == len(groups):
                    print(f"fallback TMDb: {completed}/{len(groups)} buscas concluídas; "
                          f"{resolved} capas encontradas; {failed} falhas", flush=True)
                continue
            completed += 1
            if cacheable:
                con.execute("INSERT OR REPLACE INTO tmdb_poster_cache(chave,poster) VALUES(?,?)",
                            (cache_key, poster or ""))
            if poster:
                con.executemany("UPDATE items SET poster=? WHERE id=?",
                                [(poster, item_id) for item_id in item_ids])
                resolved += len(item_ids)
                resolved_by_type[cache_key.split("|", 1)[0]] += len(item_ids)
            if completed % 25 == 0 or completed == len(groups):
                con.commit()
                print(f"fallback TMDb: {completed}/{len(groups)} buscas concluídas; "
                      f"{resolved} capas encontradas; {failed} falhas", flush=True)
    con.commit()
    print(f"fallback TMDb finalizado: {resolved} capas encontradas de {len(missing)} títulos; "
          f"por tipo: séries={resolved_by_type['s']}, filmes={resolved_by_type['f']}; "
          f"{failed} buscas falharam. Resultados válidos foram guardados em cache", flush=True)


def baixar_capas(con: sqlite3.Connection) -> None:
    pending = con.execute("SELECT id,poster FROM items WHERE poster LIKE 'http%' ").fetchall()
    downloaded_images = 0

    def download_image(pair):
        item_id, url = pair["id"], pair["poster"]
        try:
            response = requests.get(url, headers=UA, timeout=(5, 20))
            if not response.ok or not response.content:
                return None
            content_type = response.headers.get("Content-Type", "").lower()
            signatures = {
                ".jpg": response.content.startswith(b"\xff\xd8\xff"),
                ".png": response.content.startswith(b"\x89PNG\r\n\x1a\n"),
                ".webp": response.content.startswith(b"RIFF") and response.content[8:12] == b"WEBP",
                ".gif": response.content.startswith((b"GIF87a", b"GIF89a")),
            }
            if not content_type.startswith("image/") and not any(signatures.values()):
                return None
            mime_extensions = {"image/jpeg": ".jpg", "image/png": ".png",
                               "image/webp": ".webp", "image/gif": ".gif"}
            ext = next((suffix for suffix, valid in signatures.items() if valid),
                       mime_extensions.get(content_type.split(";", 1)[0],
                                           Path(urlparse(url).path).suffix.lower()))
            if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                ext = ".jpg"
            filename = f"{item_id}{ext}"
            temporary = POSTERS / (filename + ".part")
            temporary.write_bytes(response.content)
            temporary.replace(POSTERS / filename)
            return item_id, f"/posters/{filename}"
        except (requests.RequestException, OSError):
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(download_image, row) for row in pending]
        for completed, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            if result:
                con.execute("UPDATE items SET poster=? WHERE id=?", (result[1], result[0]))
                downloaded_images += 1
            if completed % 100 == 0 or completed == len(pending):
                con.commit()
                print(f"capas: {completed}/{len(pending)} URLs processadas; "
                      f"{downloaded_images} baixadas", flush=True)
    con.commit()
    print(f"capas finalizadas: {downloaded_images} baixadas de {len(pending)} URLs; "
          f"{len(pending) - downloaded_images} falharam", flush=True)


def texto(filename: str, optional: bool = False) -> str:
    global _missing_optional_feed
    try:
        response = HTTP.get(f"{CATALOGO}/{filename}", headers=UA, timeout=30)
        if optional and response.status_code == 404:
            _missing_optional_feed = True
            print(f"aviso: catálogo sem arquivo opcional {filename}")
            return ""
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        raise RuntimeError(f"não foi possível baixar {filename}: {type(exc).__name__}") from exc


def criar(con: sqlite3.Connection) -> None:
    con.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS items(
      id INTEGER PRIMARY KEY, tipo TEXT NOT NULL, titulo TEXT NOT NULL,
      titulo_norm TEXT NOT NULL, ano TEXT NOT NULL DEFAULT '', letra TEXT,
      pedaco INTEGER DEFAULT 0, poster TEXT, generos TEXT DEFAULT '',
      sinopse TEXT DEFAULT '', fontes TEXT DEFAULT '{}', eps INTEGER DEFAULT 0,
      tmdb_id INTEGER, atores TEXT DEFAULT '',
      destaque INTEGER DEFAULT 0, UNIQUE(tipo, titulo, ano));
    CREATE TABLE IF NOT EXISTS episodes(
      item_id INTEGER NOT NULL, temp INTEGER NOT NULL, ep INTEGER NOT NULL,
      versao TEXT NOT NULL, fontes TEXT NOT NULL DEFAULT '[]',
      PRIMARY KEY(item_id, temp, ep, versao));
    CREATE TABLE IF NOT EXISTS jobs(
      id INTEGER PRIMARY KEY, titulo TEXT NOT NULL, arquivo TEXT, status TEXT NOT NULL,
      pct REAL DEFAULT 0, erro TEXT, criado TEXT DEFAULT CURRENT_TIMESTAMP,
      task_json TEXT, bytes_done INTEGER DEFAULT 0, bytes_total INTEGER DEFAULT 0,
      item_id INTEGER, hidden INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS tmdb_poster_cache(
      chave TEXT PRIMARY KEY, poster TEXT NOT NULL DEFAULT '', atualizado TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE INDEX IF NOT EXISTS ix_busca ON items(titulo_norm);
    CREATE INDEX IF NOT EXISTS ix_tipo ON items(tipo, letra);
    CREATE INDEX IF NOT EXISTS ix_episode_item ON episodes(item_id, temp, ep);
    """)
    columns = {row[1] for row in con.execute("PRAGMA table_info(items)")}
    if "tmdb_id" not in columns:
        con.execute("ALTER TABLE items ADD COLUMN tmdb_id INTEGER")
    if "atores" not in columns:
        con.execute("ALTER TABLE items ADD COLUMN atores TEXT DEFAULT ''")


def upsert(con: sqlite3.Connection, tipo: str, titulo: str, letra: str,
           ano: str, fontes: dict, eps: int = 0, pedaco: int = 0) -> int:
    con.execute("""INSERT INTO items(tipo,titulo,titulo_norm,ano,letra,pedaco,fontes,eps)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(tipo,titulo,ano) DO UPDATE SET
                     fontes=excluded.fontes, eps=excluded.eps,
                     letra=excluded.letra, pedaco=excluded.pedaco""",
                (tipo, titulo, titulo.casefold(), ano, letra, pedaco,
                 json.dumps(fontes, ensure_ascii=False), eps))
    row = con.execute("SELECT id FROM items WHERE tipo=? AND titulo=? AND ano=?",
                      (tipo, titulo, ano)).fetchone()
    return int(row[0])


def main() -> None:
    global _missing_optional_feed
    _missing_optional_feed = False
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--imagens", action="store_true", help="espelhar capas em disco")
    parser.add_argument("--pausa", type=float, default=0.25, help="pausa entre arquivos do catálogo")
    parser.add_argument("--diagnostico-fichas", action="store_true",
                        help="mostrar o formato de fichas.txt sem alterar o banco")
    parser.add_argument("--tmdb-fallback", action="store_true",
                        help="buscar no TMDb capas ausentes (requer token configurado)")
    parser.add_argument("--somente-capas", action="store_true",
                        help="não sincronizar catálogo; processar somente capas do banco local")
    parser.add_argument("--refazer-cache-negativo", action="store_true",
                        help="repetir buscas anteriores do TMDb que não encontraram capa")
    args = parser.parse_args()
    if args.diagnostico_fichas:
        diagnosticar_fichas()
        return
    if args.tmdb_fallback and not TMDB_TOKEN and not TMDB_API_KEY:
        raise SystemExit("configure SAIMO_TMDB_TOKEN ou SAIMO_TMDB_API_KEY no .env antes de usar --tmdb-fallback")
    DB.parent.mkdir(parents=True, exist_ok=True)
    POSTERS.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    criar(con)

    if args.somente_capas:
        if args.tmdb_fallback:
            if args.refazer_cache_negativo:
                removed = con.execute("DELETE FROM tmdb_poster_cache WHERE poster='' ").rowcount
                con.commit()
                print(f"cache TMDb: {removed} buscas sem capa serão repetidas", flush=True)
            buscar_capas_tmdb(con)
        if args.imagens:
            baixar_capas(con)
        con.close()
        return

    print("catálogo: baixando indice.txt...", flush=True)
    bases: dict[int, str] = {}
    index_lines = texto("indice.txt").splitlines()
    for line in index_lines:
        parts = line.split()
        if len(parts) == 3 and parts[0] == "base:":
            try:
                bases[int(parts[1])] = parts[2]
            except ValueError:
                continue
    if not bases:
        con.close()
        raise RuntimeError("indice.txt não contém nenhuma base de mídia válida")

    def expandir(value: str) -> str | None:
        value = value.strip()
        if value.startswith(("http://", "https://")):
            return value
        pair = value.split(":", 1)
        if len(pair) != 2 or not pair[0].isdigit() or int(pair[0]) not in bases:
            return None
        relative = pair[1]
        filename = relative.rsplit("/", 1)[-1]
        if "." not in filename:
            relative += ".mp4"
        return bases[int(pair[0])].rstrip("/") + "/" + relative.lstrip("/")

    def campos(line: str):
        parts = line.split("\t")
        sources = {"dub": [], "leg": []}
        for column in parts[1:]:
            if "=" not in column:
                continue
            key, value = column.split("=", 1)
            if key in sources:
                sources[key] = [url for raw in value.split(",") if (url := expandir(raw))]
        return parts[0].strip(), sources

    con.execute("CREATE TEMP TABLE IF NOT EXISTS _sync_items(id INTEGER PRIMARY KEY)")
    con.execute("DELETE FROM _sync_items")
    con.execute("""CREATE TEMP TABLE IF NOT EXISTS _sync_episodes(
        item_id INTEGER, temp INTEGER, ep INTEGER, versao TEXT,
        PRIMARY KEY(item_id,temp,ep,versao))""")
    con.execute("DELETE FROM _sync_episodes")
    series_parts: dict[tuple[str, int], list[tuple[str, str, list[str]]]] = {}

    letters = [line.split("\t", 1)[0].strip() for line in index_lines
               if line and not line.startswith(("base:", "#")) and "\t" in line]
    if not letters:
        con.close()
        raise RuntimeError("indice.txt não lista letras do catálogo")
    print(f"catálogo: {len(letters)} letras encontradas; iniciando feeds de filmes e séries", flush=True)
    for letter_index, letter in enumerate(letters, start=1):
        print(f"catálogo: letra {letter_index}/{len(letters)} ({letter})", flush=True)
        movie_lines = texto(f"filmes-{letter}.txt", optional=True).splitlines()
        for line in movie_lines:
            if not line.strip():
                continue
            title, sources = campos(line)
            if title and (sources["dub"] or sources["leg"]):
                item_id = upsert(con, "f", title, letter, "", sources)
                con.execute("INSERT OR IGNORE INTO _sync_items VALUES(?)", (item_id,))
        con.commit()
        time.sleep(max(0, args.pausa))

        series_lines = texto(f"series-{letter}.txt", optional=True).splitlines()
        for line in series_lines:
            columns = line.split("\t")
            if len(columns) < 4 or not columns[0].strip():
                continue
            try:
                title, year = columns[0].strip(), columns[1].strip()
                part, episode_count = int(columns[2] or 0), int(columns[3] or 0)
            except ValueError:
                continue
            item_id = upsert(con, "s", title, letter, year, {}, episode_count, part)
            con.execute("INSERT OR IGNORE INTO _sync_items VALUES(?)", (item_id,))
            part_key = (letter, part)
            if part_key not in series_parts:
                raw_lines = texto(f"series-{letter}-{part}.txt").splitlines()
                sections: list[tuple[str, str, list[str]]] = []
                current_title = current_year = None
                current_episodes: list[str] = []
                for raw_line in raw_lines:
                    if raw_line.startswith("@"):
                        if current_title is not None:
                            sections.append((current_title, current_year or "", current_episodes))
                        marker = raw_line[1:].split("\t")
                        current_title = marker[0].strip()
                        current_year = marker[1].strip() if len(marker) > 1 else ""
                        current_episodes = []
                    elif current_title is not None and raw_line.strip():
                        current_episodes.append(raw_line)
                if current_title is not None:
                    sections.append((current_title, current_year or "", current_episodes))
                series_parts[part_key] = sections

            title_key = normalizar_titulo(title)
            matches = []
            for section_title, section_year, section_episodes in series_parts[part_key]:
                section_key = normalizar_titulo(section_title)
                trailing_year = re.search(r"\s+((?:19|20)\d{2})$", section_title)
                title_matches = section_key == title_key or (
                    trailing_year and normalizar_titulo(section_title[:trailing_year.start()]) == title_key
                )
                year_matches = not year or not section_year or year == section_year
                if trailing_year and year and trailing_year.group(1) != year:
                    year_matches = False
                if title_matches and year_matches:
                    matches.append((section_title, section_year, section_episodes))

            if len(matches) != 1:
                reason = "nenhuma seção correspondente" if not matches else "seção ambígua"
                print(f"série: {reason} para {title!r} ({year or 'sem ano'}), "
                      f"arquivo series-{letter}-{part}.txt; episódios ignorados", flush=True)
                continue

            for raw_episode in matches[0][2]:
                fields = raw_episode.split("\t")
                if len(fields) < 4 or not fields[0].isdigit() or not fields[1].isdigit():
                    continue
                version = fields[2].strip()
                if version not in ("dub", "leg"):
                    continue
                sources = [url for value in fields[3].split(",") if (url := expandir(value))]
                if sources:
                    con.execute("INSERT OR REPLACE INTO episodes VALUES(?,?,?,?,?)",
                                (item_id, int(fields[0]), int(fields[1]), version,
                                 json.dumps(sources, ensure_ascii=False)))
                    con.execute("INSERT OR IGNORE INTO _sync_episodes VALUES(?,?,?,?)",
                                (item_id, int(fields[0]), int(fields[1]), version))
            con.commit()
            time.sleep(max(0, args.pausa))
        con.commit()

    print("metadados: baixando fichas.txt e destaques.txt...", flush=True)
    metadata_lines = texto("fichas.txt").splitlines()
    highlight_lines = texto("destaques.txt").splitlines()
    exact_items: dict[tuple[str, str], list[int]] = {}
    normalized_items: dict[tuple[str, str], list[int]] = {}
    normalized_titles: dict[str, list[tuple[str, int]]] = {}
    for item_id, item_type, title in con.execute("SELECT id,tipo,titulo FROM items"):
        exact_items.setdefault((item_type, title.strip().casefold()), []).append(item_id)
        normalized_title = normalizar_titulo(title)
        normalized_items.setdefault((item_type, normalized_title), []).append(item_id)
        normalized_titles.setdefault(normalized_title, []).append((item_type, item_id))
    matched_metadata = 0
    metadata_with_poster = 0
    for line in metadata_lines:
        if line.lstrip().startswith("{"):
            try:
                record = json.loads(line)
                kind = str(record.get("tipo", record.get("type", ""))).strip()
                title = str(record.get("titulo", record.get("title", record.get("name", "")))).strip()
                poster = str(record.get("poster", record.get("capa", record.get("poster_path", "")))).strip()
                if poster.casefold() in {"none", "null"}:
                    poster = ""
                columns = [kind, title, str(record.get("tmdb_id", record.get("id", ""))), poster,
                           str(record.get("generos", record.get("genres", ""))),
                           str(record.get("sinopse", record.get("overview", "")))]
            except (AttributeError, TypeError, ValueError):
                continue
        else:
            columns = line.split("\t")
        if len(columns) < 2:
            continue
        kind, title = columns[0].strip(), columns[1].strip()
        poster = campo_poster(columns)
        item_type = tipo_catalogo(kind)
        if not title or not item_type:
            continue
        if poster:
            metadata_with_poster += 1
        genres = columns[4].strip() if len(columns) > 4 else ""
        synopsis = columns[5].strip() if len(columns) > 5 else ""
        tmdb_id = columns[2].strip() if len(columns) > 2 and columns[2].strip().isdigit() else None
        if not poster and not genres and not synopsis and not tmdb_id:
            continue
        if poster and not poster.lower().startswith(("http://", "https://")):
            poster = "https://image.tmdb.org/t/p/w342/" + poster.lstrip("/")
        item_ids = exact_items.get((item_type, title.casefold()))
        if not item_ids:
            item_ids = normalized_items.get((item_type, normalizar_titulo(title)), [])
        if not item_ids:
            # Alguns feeds identificam filme/série com rótulos que variam por versão.
            # Só usa o título sem tipo quando a correspondência é única.
            candidates = normalized_titles.get(normalizar_titulo(title), [])
            if len(candidates) == 1:
                item_ids = [candidates[0][1]]
        for item_id in item_ids:
            con.execute("""UPDATE items SET
                poster=CASE WHEN ?='' THEN poster ELSE ? END,
                generos=CASE WHEN ?='' THEN generos ELSE ? END,
                sinopse=CASE WHEN ?='' THEN sinopse ELSE ? END,
                tmdb_id=COALESCE(?,tmdb_id) WHERE id=?""",
                (poster, poster, genres, genres, synopsis, synopsis, tmdb_id, item_id))
            matched_metadata += 1
    print(f"metadados: {matched_metadata} fichas associadas; "
          f"{metadata_with_poster} linhas com capa de {len(metadata_lines)} linhas", flush=True)
    if not matched_metadata:
        print("aviso: nenhuma ficha correspondeu ao catálogo; capas não serão baixadas")
    if not _missing_optional_feed:
        con.execute("UPDATE items SET destaque=0")
        for line in highlight_lines:
            if "\t" in line or " " in line:
                title = line.split("\t")[0].replace("fila ", "").strip()
                con.execute("UPDATE items SET destaque=1 WHERE titulo=?", (title,))
        con.execute("DELETE FROM episodes WHERE item_id IN (SELECT id FROM _sync_items) "
                    "AND NOT EXISTS (SELECT 1 FROM _sync_episodes s WHERE s.item_id=episodes.item_id "
                    "AND s.temp=episodes.temp AND s.ep=episodes.ep AND s.versao=episodes.versao)")
        con.execute("DELETE FROM episodes WHERE item_id NOT IN (SELECT id FROM _sync_items)")
        con.execute("DELETE FROM items WHERE id NOT IN (SELECT id FROM _sync_items)")
    else:
        print("aviso: sincronização incompleta; preservando registros antigos que poderiam estar ausentes")
    con.commit()

    if args.tmdb_fallback:
        if args.refazer_cache_negativo:
            removed = con.execute("DELETE FROM tmdb_poster_cache WHERE poster='' ").rowcount
            con.commit()
            print(f"cache TMDb: {removed} buscas sem capa serão repetidas", flush=True)
        buscar_capas_tmdb(con)

    if args.imagens:
        baixar_capas(con)

    count = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    con.close()
    print(f"catálogo sincronizado: {count} itens ({DB})")


if __name__ == "__main__":
    main()
