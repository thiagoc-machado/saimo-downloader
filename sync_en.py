#!/usr/bin/env python3
"""Sincroniza catálogo EN (áudio original) a partir do TMDb.

Grava em `items`/`episodes` com fontes em cascata (vidsrc -> capture) na chave
`orig`. Reaproveita fichas existentes do catálogo PT. Compatível com
sync_catalog.main(args, cancel_event).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from hashlib import sha256

import requests
from dotenv import load_dotenv

load_dotenv()

DB = Path(os.getenv("SAIMO_DB", "./data/catalog.db")).expanduser().resolve()
TOKEN = os.getenv("SAIMO_TMDB_TOKEN", "").strip()
API_KEY = os.getenv("SAIMO_TMDB_API_KEY", "").strip()
IDIOMA = "en"
PAUSA = 0.28
UA = "saimo-en/1.0"
CANCEL: threading.Event | None = None


def _con() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30, check_same_thread=False)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS items(
      id INTEGER PRIMARY KEY, tipo TEXT NOT NULL, titulo TEXT NOT NULL,
      titulo_norm TEXT NOT NULL, ano TEXT NOT NULL DEFAULT '', letra TEXT,
      pedaco INTEGER DEFAULT 0, poster TEXT, generos TEXT DEFAULT '',
      sinopse TEXT DEFAULT '', fontes TEXT DEFAULT '{}', eps INTEGER DEFAULT 0,
      tmdb_id INTEGER, atores TEXT DEFAULT '', destaque INTEGER DEFAULT 0,
      idioma TEXT DEFAULT 'pt', UNIQUE(tipo,titulo,ano));
    CREATE TABLE IF NOT EXISTS episodes(
      item_id INTEGER NOT NULL,temp INTEGER NOT NULL,ep INTEGER NOT NULL,
      versao TEXT NOT NULL,fontes TEXT NOT NULL DEFAULT '[]',
      PRIMARY KEY(item_id,temp,ep,versao));
    CREATE TABLE IF NOT EXISTS tmdb_en_cache(
      chave TEXT PRIMARY KEY,payload TEXT NOT NULL,obtido INTEGER NOT NULL);
    """)
    columns = {row[1] for row in con.execute("PRAGMA table_info(items)")}
    if "idioma" not in columns:
        con.execute("ALTER TABLE items ADD COLUMN idioma TEXT DEFAULT 'pt'")
    return con


def _cancelado() -> bool:
    return bool(CANCEL and CANCEL.is_set())


def _status(con: sqlite3.Connection, **campos) -> None:
    if not campos:
        return
    sets = ", ".join(f"{k}=?" for k in campos)
    con.execute(f"UPDATE catalog_sync_status_en SET {sets} WHERE id=1", list(campos.values()))
    con.commit()


def _norm(texto: str) -> str:
    sem_acento = "".join(c for c in unicodedata.normalize("NFKD", texto)
                         if not unicodedata.combining(c))
    return " ".join(sem_acento.casefold().split())


def _tmdb(caminho: str, *, cache_con: sqlite3.Connection | None = None, **params) -> dict:
    params.setdefault("language", "pt-BR")
    cache_params = {key: value for key, value in params.items() if key != "api_key"}
    cache_key = sha256(json.dumps([caminho, cache_params], sort_keys=True,
                                  ensure_ascii=False).encode("utf-8")).hexdigest()
    owner = cache_con or _con()
    close_owner = cache_con is None
    try:
        cached = owner.execute("SELECT payload,obtido FROM tmdb_en_cache WHERE chave=?",
                               (cache_key,)).fetchone()
        if cached and int(time.time()) - int(cached["obtido"]) < 86400:
            payload = json.loads(cached["payload"])
            if close_owner:
                owner.close()
            return payload
    except (sqlite3.Error, ValueError, TypeError):
        pass
    headers = {"accept": "application/json", "user-agent": UA}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    else:
        params["api_key"] = API_KEY
    try:
        r = requests.get(f"https://api.themoviedb.org/3/{caminho}", params=params,
                         headers=headers, timeout=(5, 25))
        r.raise_for_status()
        payload = r.json()
        try:
            owner.execute("INSERT OR REPLACE INTO tmdb_en_cache(chave,payload,obtido) VALUES(?,?,?)",
                          (cache_key, json.dumps(payload, ensure_ascii=False), int(time.time())))
            owner.commit()
        except sqlite3.Error:
            pass
        return payload
    finally:
        if close_owner:
            owner.close()


def _fonte(kind: str, tmdb: int, season: int = 0, episode: int = 0) -> list[dict]:
    base = {"kind": kind, "tmdb": tmdb}
    if season:
        base |= {"season": season, "episode": episode}
    return [{"provider": "vidsrc", **base}, {"provider": "capture", **base}]


def _capa(con: sqlite3.Connection, item_id: int, caminho: str | None) -> None:
    if not caminho:
        return
    con.execute("UPDATE items SET poster=? WHERE id=? AND (poster IS NULL OR poster='')",
                (f"https://image.tmdb.org/t/p/w500/{caminho.lstrip('/')}", item_id))
    con.commit()


def _upsert(con: sqlite3.Connection, tipo: str, dados: dict, destaque: int) -> int | None:
    titulo = str(dados.get("title") or dados.get("name") or "").strip()
    if not titulo:
        return None
    ano = str(dados.get("release_date") or dados.get("first_air_date") or "")[:4]
    norm = _norm(titulo)
    letra = titulo[0].upper() if titulo[0].upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" else None
    generos = ", ".join(g.get("name", "") for g in dados.get("genres") or []
                        if isinstance(g, dict) and g.get("name"))
    sinopse = str(dados.get("overview") or "").strip()
    fontes_map = {"orig": _fonte("movie" if tipo == "f" else "tv", int(dados["id"]))}
    fontes = json.dumps(fontes_map, ensure_ascii=False)
    con.execute("""INSERT INTO items(tipo,titulo,titulo_norm,ano,letra,generos,sinopse,
                     tmdb_id,destaque,idioma,fontes)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(tipo,titulo,ano) DO UPDATE SET
                     idioma = CASE WHEN instr(',' || ifnull(items.idioma,'') || ',', ',en,') = 0
                                   THEN trim(ifnull(items.idioma,'') || ',en', ',')
                                   ELSE items.idioma END,
                     tmdb_id = excluded.tmdb_id""",
                (tipo, titulo, norm, ano, letra, generos, sinopse,
                 int(dados["id"]), destaque, IDIOMA, fontes))
    linha = con.execute("SELECT id,fontes FROM items WHERE tipo=? AND titulo=? AND ano=?",
                        (tipo, titulo, ano)).fetchone()
    if not linha:
        return None
    mapa = {}
    try:
        mapa = json.loads(linha["fontes"] or "{}")
    except ValueError:
        mapa = {}
    if not isinstance(mapa, dict):
        mapa = {}
    mapa["orig"] = fontes_map["orig"]
    con.execute("UPDATE items SET fontes=? WHERE id=?",
                (json.dumps(mapa, ensure_ascii=False), linha["id"]))
    con.commit()
    _capa(con, int(linha["id"]), dados.get("poster_path"))
    return int(linha["id"])


def _episodios(con: sqlite3.Connection, item_id: int, tmdb: int, temporadas: int,
               pausa: float = PAUSA) -> int:
    total = 0
    for temporada in range(1, temporadas + 1):
        if _cancelado():
            break
        try:
            dados = _tmdb(f"tv/{tmdb}/season/{temporada}", cache_con=con)
        except requests.RequestException:
            continue
        for ep in dados.get("episodes") or []:
            numero = int(ep.get("episode_number") or 0)
            if not numero:
                continue
            fontes = json.dumps({"orig": _fonte("tv", tmdb, temporada, numero)},
                                ensure_ascii=False)
            con.execute("""INSERT OR IGNORE INTO episodes(item_id,temp,ep,versao,fontes)
                           VALUES(?,?,?,'orig',?)""", (item_id, temporada, numero, fontes))
            total += 1
        con.commit()
        time.sleep(pausa)
    return total


def _lista(caminho: str, paginas: int, cache_con: sqlite3.Connection | None = None,
           pausa: float = PAUSA, **params) -> list[dict]:
    saida: list[dict] = []
    for pagina in range(1, paginas + 1):
        if _cancelado():
            break
        try:
            dados = _tmdb(caminho, cache_con=cache_con, page=pagina, **params)
        except requests.RequestException:
            raise
        resultados = dados.get("results") or []
        if not resultados:
            break
        saida.extend(resultados)
        time.sleep(pausa)
    return saida


def main(args=None, cancel_event: threading.Event | None = None) -> dict:
    global CANCEL
    CANCEL = cancel_event or threading.Event()
    args = args or argparse.Namespace()
    limite = int(getattr(args, "limite", 300) or 300)
    paginas = max(1, min(20, int(getattr(args, "paginas", 5) or 5)))
    temporadas_max = int(getattr(args, "temporadas", 20) or 20)
    pausa = float(getattr(args, "pausa", PAUSA) or PAUSA)
    so_capas = bool(getattr(args, "somente_capas", False))

    con = _con()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS catalog_sync_status_en(
      id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL DEFAULT 'ocioso',
      fase TEXT, atual TEXT, processados INTEGER NOT NULL DEFAULT 0,
      total INTEGER NOT NULL DEFAULT 0, feed INTEGER NOT NULL DEFAULT 0,
      feeds INTEGER NOT NULL DEFAULT 0, iniciado TEXT, finalizado TEXT,
      ultima_concluida TEXT, erro TEXT, cancelar INTEGER NOT NULL DEFAULT 0);
    INSERT OR IGNORE INTO catalog_sync_status_en(id,status) VALUES(1,'ocioso');
    """)
    _status(con, status="executando", fase="Preparando", atual="Coletando do TMDb",
            processados=0, total=0, feed=0, feeds=2, iniciado=datetime.now().isoformat(),
            finalizado=None, erro=None, cancelar=0)

    try:
        if not TOKEN and not API_KEY:
            raise RuntimeError("Configure SAIMO_TMDB_TOKEN ou SAIMO_TMDB_API_KEY para sincronizar o catálogo EN")
        alvos: list[tuple[str, dict]] = []
        if not so_capas:
            filmes = _lista("discover/movie", paginas, pausa=pausa, with_original_language=IDIOMA,
                            sort_by="popularity.desc", include_adult="false", cache_con=con)
            alvos += [("f", d) for d in filmes]
            series = _lista("discover/tv", paginas, pausa=pausa, with_original_language=IDIOMA,
                            sort_by="popularity.desc", include_adult="false", cache_con=con)
            alvos += [("s", d) for d in series]
            alvos = alvos[:limite]

        _status(con, fase="Gravando", total=len(alvos), feed=1)
        inseridos = 0
        for indice, (tipo, bruto) in enumerate(alvos, 1):
            if _cancelado():
                break
            _status(con, atual=f"{bruto.get('title') or bruto.get('name')}",
                    processados=indice)
            try:
                detalhe = _tmdb(f"{'movie' if tipo == 'f' else 'tv'}/{bruto['id']}", cache_con=con)
            except (requests.RequestException, KeyError):
                continue
            destaque = 1 if float(bruto.get("popularity") or 0) > 80 else 0
            item_id = _upsert(con, tipo, detalhe, destaque)
            if item_id:
                inseridos += 1
                if tipo == "s":
                    _episodios(con, item_id, int(bruto["id"]),
                               min(int(detalhe.get("number_of_seasons") or 1), temporadas_max), pausa)
            time.sleep(pausa)

        if so_capas:
            _status(con, fase="Capas", feed=2)
            for linha in con.execute("SELECT id,tmdb_id,tipo FROM items "
                                     "WHERE instr(idioma,'en')>0 AND (poster IS NULL OR poster='') "
                                     "AND tmdb_id IS NOT NULL LIMIT ?", (limite,)):
                try:
                    d = _tmdb(f"{'movie' if linha['tipo'] == 'f' else 'tv'}/{linha['tmdb_id']}", cache_con=con)
                    _capa(con, int(linha["id"]), d.get("poster_path"))
                except requests.RequestException:
                    continue
                time.sleep(pausa)

        _status(con, status="concluido" if not _cancelado() else "interrompido",
                fase="Concluído" if not _cancelado() else "Interrompido",
                atual=f"{inseridos} títulos processados",
                finalizado=datetime.now().isoformat(),
                **({"ultima_concluida": datetime.now().isoformat()} if not _cancelado() else {}))
        return {"processados": inseridos, "cancelado": _cancelado()}
    except Exception as exc:
        _status(con, status="erro", fase="Falhou", erro=str(exc)[:300],
                finalizado=datetime.now().isoformat())
        raise
    finally:
        con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="sincroniza catálogo EN")
    ap.add_argument("--limite", type=int, default=300)
    ap.add_argument("--paginas", type=int, default=5)
    ap.add_argument("--temporadas", type=int, default=20)
    ap.add_argument("--pausa", type=float, default=PAUSA)
    ap.add_argument("--somente-capas", action="store_true")
    main(ap.parse_args())
