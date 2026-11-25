#!/usr/bin/env python3
"""Servidor web local para navegar no catálogo e gerenciar downloads."""
from __future__ import annotations

import json
import hashlib
import hmac
import os
import queue
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import saimo_core as core
import saimo_en

load_dotenv()
DB = Path(os.getenv("SAIMO_DB", "./data/catalog.db")).expanduser().resolve()
TOKEN = os.getenv("SAIMO_TOKEN", "")
TMDB_TOKEN = os.getenv("SAIMO_TMDB_TOKEN", "").strip()
TMDB_API_KEY = os.getenv("SAIMO_TMDB_API_KEY", "").strip()
PARALELO = max(1, min(8, int(os.getenv("SAIMO_PARALLEL", "2"))))
POSTERS = Path(os.getenv("SAIMO_POSTERS", "./static/posters")).expanduser().resolve()
DB.parent.mkdir(parents=True, exist_ok=True)
Path(core.DESTINO).mkdir(parents=True, exist_ok=True)
POSTERS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Saimo local", version="1.0.0")
fila: queue.Queue = queue.Queue()
SEARCH_FTS = False
controles: dict[int, core.DownloadControl] = {}
controle_lock = threading.Lock()
ids_enfileirados: set[int] = set()
fila_lock = threading.Lock()
catalog_sync_lock = threading.Lock()
catalog_sync_cancel = threading.Event()
catalog_sync_thread: threading.Thread | None = None
catalog_sync_lock_en = threading.Lock()
catalog_sync_cancel_en = threading.Event()
catalog_sync_thread_en: threading.Thread | None = None
ALPHA_VARIANTS = {
    "A": "aáàâãäåāăą", "C": "cçćč", "E": "eéèêëēĕėęě",
    "I": "iíìîïīĭįı", "N": "nñńňņ", "O": "oóòôõöøōŏő",
    "S": "sśšşṣ", "U": "uúùûüūŭůűų", "Y": "yýÿŷ",
    "Z": "zżžź",
}


def condicao_letra(letter: str) -> tuple[str, list[str]]:
    letter = letter.strip().upper()
    if len(letter) != 1 or letter not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        raise HTTPException(400, "letra deve ser de A a Z")
    variants = ALPHA_VARIANTS.get(letter, letter.lower())
    ranges = []
    values = []
    for char in variants:
        low = char.casefold()
        high = chr(ord(low[-1]) + 1)
        ranges.append("(titulo_norm>=? AND titulo_norm<?)")
        values.extend((low, high))
    return "(" + " OR ".join(ranges) + ")", values


def enfileirar(task: tuple) -> None:
    job_id = int(task[0])
    with fila_lock:
        if job_id not in ids_enfileirados:
            ids_enfileirados.add(job_id)
            fila.put(task)


def limpar_dados_job(output_base: str) -> None:
    """Remove apenas os arquivos pertencentes ao job cancelado."""
    base = Path(output_base).resolve()
    root = Path(core.DESTINO).resolve()
    if not base.is_relative_to(root):
        return
    final = Path(str(base) + ".mp4")
    for path in (final, Path(str(final) + ".part")):
        try:
            path.unlink(missing_ok=True)
        except IsADirectoryError:
            shutil.rmtree(path, ignore_errors=True)
    shutil.rmtree(Path(str(final) + ".segs"), ignore_errors=True)
    try:
        base.parent.rmdir()
    except OSError:
        pass


def limpar_progresso_job(output_base: str) -> None:
    """Remove apenas temporários de um job pausado, preservando arquivos finais."""
    base = Path(output_base).resolve()
    root = Path(core.DESTINO).resolve()
    if not base.is_relative_to(root):
        return
    final = Path(str(base) + ".mp4")
    Path(str(final) + ".part").unlink(missing_ok=True)
    shutil.rmtree(Path(str(final) + ".segs"), ignore_errors=True)
    try:
        base.parent.rmdir()
    except OSError:
        pass


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=30, check_same_thread=False)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA synchronous=NORMAL")
    con.row_factory = sqlite3.Row
    return con


@contextmanager
def session():
    con = db()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def checar(req: Request) -> None:
    if TOKEN and req.headers.get("x-token") != TOKEN:
        raise HTTPException(401, "token inválido")


def preparar_armazenamento() -> None:
    """Migra a tabela de jobs e recupera tarefas persistidas após reinício."""
    global SEARCH_FTS
    with session() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.executescript("""
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
        CREATE INDEX IF NOT EXISTS ix_busca ON items(titulo_norm);
        CREATE INDEX IF NOT EXISTS ix_tipo ON items(tipo, letra);
        CREATE INDEX IF NOT EXISTS ix_episode_item ON episodes(item_id, temp, ep);
        """)
        item_columns = {row[1] for row in con.execute("PRAGMA table_info(items)")}
        if "idioma" not in item_columns:
            con.execute("ALTER TABLE items ADD COLUMN idioma TEXT DEFAULT 'pt'")
        if "tmdb_id" not in item_columns:
            con.execute("ALTER TABLE items ADD COLUMN tmdb_id INTEGER")
        if "atores" not in item_columns:
            con.execute("ALTER TABLE items ADD COLUMN atores TEXT DEFAULT ''")
        con.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY, titulo TEXT NOT NULL, arquivo TEXT, status TEXT NOT NULL,
            pct REAL DEFAULT 0, erro TEXT, criado TEXT DEFAULT CURRENT_TIMESTAMP,
            task_json TEXT, bytes_done INTEGER DEFAULT 0, bytes_total INTEGER DEFAULT 0,
            item_id INTEGER, hidden INTEGER DEFAULT 0, aviso TEXT)""")
        columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
        if "task_json" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN task_json TEXT")
        if "bytes_done" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN bytes_done INTEGER DEFAULT 0")
        if "bytes_total" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN bytes_total INTEGER DEFAULT 0")
        if "item_id" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN item_id INTEGER")
        if "hidden" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN hidden INTEGER DEFAULT 0")
        if "aviso" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN aviso TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS ix_jobs_item_status ON jobs(item_id,status)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_jobs_hidden_status_id ON jobs(hidden,status,id DESC)")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS catalog_sync_status(
          id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL DEFAULT 'ocioso',
          fase TEXT, atual TEXT, processados INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
          feed INTEGER NOT NULL DEFAULT 0, feeds INTEGER NOT NULL DEFAULT 0,
          iniciado TEXT, finalizado TEXT, ultima_concluida TEXT, erro TEXT, cancelar INTEGER NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO catalog_sync_status(id,status) VALUES(1,'ocioso');
        CREATE TABLE IF NOT EXISTS catalog_sync_meta(id INTEGER PRIMARY KEY CHECK(id=1), baseline_ativo INTEGER NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO catalog_sync_meta(id,baseline_ativo) VALUES(1,0);
        CREATE TABLE IF NOT EXISTS catalog_sync_baseline_items(
          id INTEGER PRIMARY KEY, tipo TEXT NOT NULL, titulo TEXT NOT NULL, ano TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS catalog_sync_baseline_episodes(
          item_id INTEGER NOT NULL, temp INTEGER NOT NULL, ep INTEGER NOT NULL, versao TEXT NOT NULL,
          PRIMARY KEY(item_id,temp,ep,versao));
        CREATE TABLE IF NOT EXISTS catalog_sync_changes(
          id INTEGER PRIMARY KEY AUTOINCREMENT, tipo TEXT NOT NULL, item_id INTEGER,
          item_tipo TEXT NOT NULL, titulo TEXT NOT NULL, ano TEXT NOT NULL DEFAULT '',
          temp INTEGER, ep INTEGER, versao TEXT);
        CREATE INDEX IF NOT EXISTS ix_catalog_changes_type ON catalog_sync_changes(tipo,item_id);
        UPDATE catalog_sync_status SET status='interrompido',fase='Interrompido',
          atual='A aplicação foi reiniciada durante a atualização',finalizado=CURRENT_TIMESTAMP
          WHERE status='executando';
        CREATE TABLE IF NOT EXISTS catalog_sync_status_en(
          id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL DEFAULT 'ocioso',
          fase TEXT, atual TEXT, processados INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
          feed INTEGER NOT NULL DEFAULT 0, feeds INTEGER NOT NULL DEFAULT 0,
          iniciado TEXT, finalizado TEXT, ultima_concluida TEXT, erro TEXT, cancelar INTEGER NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO catalog_sync_status_en(id,status) VALUES(1,'ocioso');
        UPDATE catalog_sync_status_en SET status='interrompido',fase='Interrompido',
          atual='A aplicação foi reiniciada durante a atualização',finalizado=CURRENT_TIMESTAMP
          WHERE status='executando';
        """)
        status_columns = {row[1] for row in con.execute("PRAGMA table_info(catalog_sync_status)")}
        if "ultima_concluida" not in status_columns:
            con.execute("ALTER TABLE catalog_sync_status ADD COLUMN ultima_concluida TEXT")
        has_items = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'").fetchone()
        if has_items:
            search_exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='item_search'").fetchone()
            try:
                con.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS item_search USING fts5(
                    titulo_norm, content='items', content_rowid='id', tokenize='trigram')""")
                con.executescript("""
                CREATE TRIGGER IF NOT EXISTS items_search_ai AFTER INSERT ON items BEGIN
                  INSERT INTO item_search(rowid,titulo_norm) VALUES(new.id,new.titulo_norm);
                END;
                CREATE TRIGGER IF NOT EXISTS items_search_ad AFTER DELETE ON items BEGIN
                  INSERT INTO item_search(item_search,rowid,titulo_norm)
                  VALUES('delete',old.id,old.titulo_norm);
                END;
                CREATE TRIGGER IF NOT EXISTS items_search_au AFTER UPDATE OF titulo_norm ON items BEGIN
                  INSERT INTO item_search(item_search,rowid,titulo_norm)
                  VALUES('delete',old.id,old.titulo_norm);
                  INSERT INTO item_search(rowid,titulo_norm) VALUES(new.id,new.titulo_norm);
                END;
                """)
                if not search_exists:
                    con.execute("INSERT INTO item_search(item_search) VALUES('rebuild')")
                SEARCH_FTS = True
            except sqlite3.OperationalError:
                # SQLite sem o tokenizer trigram continua funcionando com LIKE.
                SEARCH_FTS = False

        pending = con.execute("SELECT id,titulo,status,task_json FROM jobs "
                              "WHERE hidden=0 AND status IN "
                              "('fila','resolvendo','baixando','pausando','pausado','cancelando')").fetchall()
        for row in pending:
            try:
                if row["status"] in ("pausado", "pausando"):
                    con.execute("UPDATE jobs SET status='pausado',erro=NULL WHERE id=?", (row["id"],))
                    continue
                payload = json.loads(row["task_json"] or "")
                task = (int(row["id"]), row["titulo"], payload["sources"], payload["output_base"])
                if row["status"] == "cancelando":
                    limpar_dados_job(payload["output_base"])
                    con.execute("UPDATE jobs SET status='cancelado',pct=0 WHERE id=?", (row["id"],))
                    continue
                con.execute("UPDATE jobs SET status='fila',erro=NULL WHERE id=?", (row["id"],))
                enfileirar(task)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                con.execute("UPDATE jobs SET status='erro',erro=? WHERE id=?",
                            ("job antigo sem dados para recuperação; enfileire novamente", row["id"]))


def ticket_media(relative: str) -> str:
    encoded_path = quote(relative, safe="/")
    if not TOKEN:
        return f"/media/{encoded_path}"
    expires = int(time.time()) + 1800
    message = f"{relative}\n{expires}".encode("utf-8")
    signature = hmac.new(TOKEN.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"/media/{encoded_path}?expires={expires}&sig={signature}"


def validar_ticket(relative: str, expires: int, signature: str) -> bool:
    if not TOKEN or expires < int(time.time()) or expires > int(time.time()) + 1800:
        return False
    message = f"{relative}\n{expires}".encode("utf-8")
    expected = hmac.new(TOKEN.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def criar_sessao_media() -> tuple[str, int]:
    expires = int(time.time()) + 86400
    message = f"media-session\n{expires}".encode("utf-8")
    signature = hmac.new(TOKEN.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{expires}.{signature}", expires


def validar_sessao_media(value: str) -> bool:
    try:
        raw_expiry, signature = value.split(".", 1)
        expires = int(raw_expiry)
    except (ValueError, AttributeError):
        return False
    if not TOKEN or expires < int(time.time()) or expires > int(time.time()) + 86400:
        return False
    message = f"media-session\n{expires}".encode("utf-8")
    expected = hmac.new(TOKEN.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def worker() -> None:
    while True:
        job_id, title, sources, output_base = fila.get()
        with fila_lock:
            ids_enfileirados.discard(int(job_id))
        con = db()
        progress_lock = threading.Lock()
        control = core.DownloadControl()
        try:
            with controle_lock:
                controles[job_id] = control
                claimed = con.execute("UPDATE jobs SET status='resolvendo' WHERE id=? AND status='fila'",
                                      (job_id,)).rowcount
                con.commit()
            if not claimed:
                with controle_lock:
                    controles.pop(job_id, None)
                continue
            task_row = con.execute("SELECT task_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            try:
                task_data = json.loads(task_row["task_json"] or "{}") if task_row else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                task_data = {}
            control.checkpoint()
            is_original = task_data.get("version") == "orig"
            if is_original:
                quality = int(task_data.get("max_h") or 1080)
                sources = [dict(source, max_h=quality) if isinstance(source, dict) else source
                           for source in sources]
                resolved = saimo_en.resolver_fontes(
                    sources, control, int(task_data.get("provider_offset") or 0))
            else:
                resolved = core.resolver_fontes(sources, control)
            if not resolved:
                con.execute("UPDATE jobs SET status='erro',aviso=NULL,erro=? WHERE id=?",
                            ("sem fonte disponível", job_id))
                con.commit()
                continue

            last_progress = {"time": 0.0, "units": -1.0}

            def progress(done: int, total: int, bytes_done: int | None = None,
                         bytes_total: int | None = None) -> None:
                control.checkpoint()
                percent = min(99.9, done / total * 100) if total else 0
                with progress_lock:
                    now = time.monotonic()
                    final_update = total > 0 and done >= total
                    if not final_update and done - last_progress["units"] < (1 << 20) and now - last_progress["time"] < 0.5:
                        return
                    con.execute("UPDATE jobs SET pct=?,status='baixando',bytes_done=?,bytes_total=? WHERE id=?",
                                (percent, done if bytes_done is None else bytes_done,
                                 total if bytes_total is None else bytes_total, job_id))
                    con.commit()
                    last_progress["time"] = now
                    last_progress["units"] = done

            result = None
            source_errors = []
            if is_original:
                for candidate in resolved:
                    control.checkpoint()
                    candidate["quality"] = quality
                    try:
                        if candidate.get("audio_url") or candidate.get("provider") == "ytdlp":
                            result = saimo_en.baixar_ytdlp(candidate, output_base, progress, control)
                            if not result and not candidate.get("audio_url"):
                                result = core.baixar([candidate], output_base, progress, control)
                        else:
                            result = core.baixar([candidate], output_base, progress, control)
                        if result:
                            break
                        source_errors.append(f"{candidate.get('provider') or 'fonte'}: falha no download")
                    except (core.DownloadPaused, core.DownloadCancelled):
                        raise
                    except Exception as exc:
                        source_errors.append(f"{candidate.get('provider') or 'fonte'}: {str(exc)[:100]}")
            else:
                result = core.baixar(resolved, output_base, progress, control)

            aviso = None
            if result and is_original and task_data.get("legenda") and task_data.get("tmdb_id"):
                try:
                    legenda = saimo_en.buscar_legenda(
                        int(task_data["tmdb_id"]), str(task_data.get("kind") or "movie"),
                        int(task_data.get("season") or 0), int(task_data.get("episode") or 0))
                    if legenda:
                        Path(result).with_suffix(".srt").write_text(legenda, encoding="utf-8-sig")
                    else:
                        aviso = "Vídeo baixado sem legenda PT-BR: não encontrada ou API indisponível."
                except Exception:
                    aviso = "Vídeo baixado sem legenda PT-BR: não foi possível consultar a busca."
            with controle_lock:
                control.checkpoint()
                if result:
                    relative = os.path.relpath(result, core.DESTINO)
                    con.execute("UPDATE jobs SET status='concluido', pct=100, arquivo=?,aviso=?,erro=NULL WHERE id=?",
                                (relative, aviso, job_id))
                else:
                    con.execute("UPDATE jobs SET status='erro', erro=? WHERE id=?",
                                ("; ".join(source_errors[-3:]) or "todas as fontes falharam", job_id))
                con.commit()
        except core.DownloadPaused:
            row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row and row["status"] == "cancelando":
                limpar_dados_job(output_base)
                con.execute("UPDATE jobs SET status='cancelado',pct=0,arquivo=NULL,erro=NULL WHERE id=?",
                            (job_id,))
            else:
                con.execute("UPDATE jobs SET status='pausado',erro=NULL WHERE id=?", (job_id,))
            con.commit()
        except core.DownloadCancelled:
            limpar_dados_job(output_base)
            con.execute("UPDATE jobs SET status='cancelado',pct=0,arquivo=NULL,erro=NULL WHERE id=?",
                        (job_id,))
            con.commit()
        except Exception as exc:
            row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row and row["status"] == "cancelando":
                limpar_dados_job(output_base)
                con.execute("UPDATE jobs SET status='cancelado',pct=0,arquivo=NULL,erro=NULL WHERE id=?",
                            (job_id,))
            else:
                con.execute("UPDATE jobs SET status='erro', erro=? WHERE id=?",
                            (str(exc)[:200] or type(exc).__name__, job_id))
            con.commit()
        finally:
            with controle_lock:
                if controles.get(job_id) is control:
                    controles.pop(job_id, None)
            con.close()
            fila.task_done()


preparar_armazenamento()
for _ in range(PARALELO):
    threading.Thread(target=worker, daemon=True, name="saimo-download-worker").start()


@app.get("/api/catalog/letters")
def catalog_letters(req: Request, tipo: str = "", idioma: str = "pt"):
    checar(req)
    if tipo not in ("", "f", "s"):
        raise HTTPException(400, "tipo deve ser f ou s")
    if idioma not in ("pt", "en", "todos"):
        raise HTTPException(400, "idioma deve ser pt, en ou todos")
    counts = []
    with session() as con:
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            condition, values = condicao_letra(letter)
            conditions = [condition]
            if idioma != "todos":
                conditions.append("instr(ifnull(idioma,'pt'),?)>0")
                values.append(idioma)
            if tipo:
                conditions.append("tipo=?")
                values.append(tipo)
            total = con.execute(
                f"SELECT COUNT(*) FROM items WHERE {' AND '.join(conditions)}", values
            ).fetchone()[0]
            counts.append({"letra": letter, "total": total})
    return {"letras": counts}


@app.get("/api/items")
def items(req: Request, q: str = "", tipo: str = "", pagina: int = 0,
          limite: int = 60, letra: str = "", idioma: str = "pt"):
    checar(req)
    if idioma not in ("pt", "en", "todos"):
        raise HTTPException(400, "idioma deve ser pt, en ou todos")
    pagina = max(0, pagina)
    limite = max(1, min(100, limite))
    conditions, values = ["1=1"], []
    if idioma != "todos":
        conditions.append("instr(ifnull(idioma,'pt'),?)>0")
        values.append(idioma)
    if letra.strip():
        letter_condition, letter_values = condicao_letra(letra)
        conditions.append(letter_condition)
        values.extend(letter_values)
    if q.strip():
        term = q.strip().casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        if SEARCH_FTS and len(q.strip()) >= 3:
            conditions.append("id IN (SELECT rowid FROM item_search WHERE titulo_norm MATCH ?)")
            values.append('"' + q.strip().replace('"', '""') + '"')
        else:
            conditions.append("titulo_norm LIKE ? ESCAPE '\\'")
            values.append(f"%{term}%")
    if tipo in ("f", "s"):
        conditions.append("tipo=?")
        values.append(tipo)
    where = " AND ".join(conditions)
    with session() as con:
        rows = [dict(row) for row in con.execute(
            f"SELECT id,tipo,titulo,ano,poster,generos,eps,destaque,"
            "(SELECT COUNT(*) FROM jobs j WHERE j.item_id=items.id AND j.status='concluido') AS baixados,"
            "(SELECT COUNT(*) FROM jobs j WHERE j.item_id=items.id AND j.status IN "
            "('resolvendo','baixando','pausando','cancelando')) AS downloads_ativos,"
            "(SELECT COUNT(*) FROM jobs j WHERE j.item_id=items.id AND j.status IN ('fila','pausado')) AS downloads_fila "
            f"FROM items WHERE {where} "
            f"ORDER BY {'titulo COLLATE NOCASE' if letra.strip() else 'destaque DESC, titulo COLLATE NOCASE'} LIMIT ? OFFSET ?",
            values + [limite, pagina * limite])]
        total = con.execute(f"SELECT COUNT(*) FROM items WHERE {where}", values).fetchone()[0]
        if idioma == "todos":
            catalog_count = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        else:
            catalog_count = con.execute("SELECT COUNT(*) FROM items "
                                        "WHERE instr(ifnull(idioma,'pt'),?)>0",
                                        (idioma,)).fetchone()[0]
    return {"items": rows, "total": total, "pagina": pagina,
            "catalogo_sincronizado": catalog_count > 0}


@app.get("/api/items/status")
def item_status(req: Request, ids: str = ""):
    checar(req)
    try:
        item_ids = list(dict.fromkeys(int(value) for value in ids.split(",") if value.strip()))
    except ValueError:
        raise HTTPException(400, "ids deve conter números separados por vírgula")
    if not item_ids:
        return {"items": []}
    if len(item_ids) > 500:
        raise HTTPException(400, "limite de 500 títulos por consulta")
    placeholders = ",".join("?" for _ in item_ids)
    with session() as con:
        rows = [dict(row) for row in con.execute(
            f"SELECT i.id,i.tipo,"
            "SUM(CASE WHEN j.status='concluido' THEN 1 ELSE 0 END) AS baixados,"
            "SUM(CASE WHEN j.status IN ('resolvendo','baixando','pausando','cancelando') THEN 1 ELSE 0 END) AS downloads_ativos,"
            "SUM(CASE WHEN j.status IN ('fila','pausado') THEN 1 ELSE 0 END) AS downloads_fila "
            f"FROM items i LEFT JOIN jobs j ON j.item_id=i.id WHERE i.id IN ({placeholders}) GROUP BY i.id,i.tipo",
            item_ids)]
    return {"items": rows}


def _catalog_sync_worker() -> None:
    import argparse
    import sync_catalog
    args = argparse.Namespace(imagens=True, pausa=0.0, diagnostico_fichas=False,
                              tmdb_fallback=False, somente_capas=False,
                              refazer_cache_negativo=False)
    try:
        sync_catalog.main(args=args, cancel_event=catalog_sync_cancel)
    except Exception:
        # O estado e a mensagem de erro ficam persistidos pelo sincronizador.
        pass
    finally:
        catalog_sync_cancel.clear()
        catalog_sync_lock.release()


@app.get("/api/catalog/sync/status")
def catalog_sync_status(req: Request):
    checar(req)
    with session() as con:
        row = con.execute("SELECT * FROM catalog_sync_status WHERE id=1").fetchone()
    result = dict(row) if row else {"status": "ocioso", "processados": 0, "total": 0}
    total = int(result.get("total") or 0)
    processed = int(result.get("processados") or 0)
    result["porcentagem"] = round(min(100, processed * 100 / total), 1) if total else 0
    return result


@app.post("/api/catalog/sync")
def start_catalog_sync(req: Request):
    checar(req)
    global catalog_sync_thread
    if not catalog_sync_lock.acquire(blocking=False):
        raise HTTPException(409, "atualização do catálogo já está em andamento")
    catalog_sync_cancel.clear()
    try:
        with session() as con:
            current = con.execute("SELECT status FROM catalog_sync_status WHERE id=1").fetchone()
            if current and current[0] == "executando":
                raise HTTPException(409, "atualização do catálogo já está em andamento")
            con.execute("UPDATE catalog_sync_status SET status='executando',fase='Iniciando',atual='Preparando feeds',"
                        "processados=0,total=0,feed=0,feeds=0,iniciado=CURRENT_TIMESTAMP,finalizado=NULL,"
                        "erro=NULL,cancelar=0 WHERE id=1")
        catalog_sync_thread = threading.Thread(target=_catalog_sync_worker,
                                               daemon=True, name="saimo-catalog-sync")
        catalog_sync_thread.start()
    except Exception:
        catalog_sync_lock.release()
        raise
    return {"status": "executando"}


@app.post("/api/catalog/sync/cancel")
def cancel_catalog_sync(req: Request):
    checar(req)
    if not catalog_sync_lock.locked():
        raise HTTPException(409, "não há atualização em andamento")
    catalog_sync_cancel.set()
    with session() as con:
        con.execute("UPDATE catalog_sync_status SET cancelar=1,fase='Interrompendo' WHERE id=1")
    return {"status": "interrompendo"}


def _catalog_sync_en_worker() -> None:
    import argparse
    import sync_en
    args = argparse.Namespace(limite=300, paginas=8, temporadas=20,
                              pausa=0.3, somente_capas=False)
    try:
        sync_en.main(args=args, cancel_event=catalog_sync_cancel_en)
    except Exception as exc:
        with session() as con:
            con.execute("UPDATE catalog_sync_status_en SET status='erro',fase='Falhou',erro=?,"
                        "finalizado=CURRENT_TIMESTAMP WHERE id=1", (str(exc)[:300],))
    finally:
        catalog_sync_cancel_en.clear()
        catalog_sync_lock_en.release()


@app.get("/api/en/catalog/sync/status")
def catalog_sync_status_en(req: Request):
    checar(req)
    with session() as con:
        row = con.execute("SELECT * FROM catalog_sync_status_en WHERE id=1").fetchone()
    result = dict(row) if row else {"status": "ocioso", "processados": 0, "total": 0}
    total = int(result.get("total") or 0)
    processed = int(result.get("processados") or 0)
    result["porcentagem"] = round(min(100, processed * 100 / total), 1) if total else 0
    return result


@app.post("/api/en/catalog/sync")
def start_catalog_sync_en(req: Request):
    checar(req)
    global catalog_sync_thread_en
    if not catalog_sync_lock_en.acquire(blocking=False):
        raise HTTPException(409, "atualização do catálogo EN já está em andamento")
    catalog_sync_cancel_en.clear()
    try:
        with session() as con:
            current = con.execute("SELECT status FROM catalog_sync_status_en WHERE id=1").fetchone()
            if current and current[0] == "executando":
                raise HTTPException(409, "atualização do catálogo EN já está em andamento")
            con.execute("UPDATE catalog_sync_status_en SET status='executando',fase='Iniciando',"
                        "atual='Preparando feeds',processados=0,total=0,feed=0,feeds=0,"
                        "iniciado=CURRENT_TIMESTAMP,finalizado=NULL,erro=NULL,cancelar=0 WHERE id=1")
        catalog_sync_thread_en = threading.Thread(target=_catalog_sync_en_worker,
                                                  daemon=True, name="saimo-catalog-sync-en")
        catalog_sync_thread_en.start()
    except Exception:
        catalog_sync_lock_en.release()
        raise
    return {"status": "executando"}


@app.post("/api/en/catalog/sync/cancel")
def cancel_catalog_sync_en(req: Request):
    checar(req)
    if not catalog_sync_lock_en.locked():
        raise HTTPException(409, "não há atualização EN em andamento")
    catalog_sync_cancel_en.set()
    with session() as con:
        con.execute("UPDATE catalog_sync_status_en SET cancelar=1,fase='Interrompendo' WHERE id=1")
    return {"status": "interrompendo"}


@app.get("/api/catalog/novelties")
def catalog_novelties(req: Request, pagina: int = 0, limite: int = 60):
    checar(req)
    pagina = max(0, pagina)
    limite = max(1, min(100, limite))
    with session() as con:
        additions = con.execute("""SELECT i.id,i.tipo,i.titulo,i.ano,i.poster,i.generos,i.eps,i.destaque,
            (SELECT COUNT(*) FROM jobs j WHERE j.item_id=i.id AND j.status='concluido') AS baixados,
            (SELECT COUNT(*) FROM jobs j WHERE j.item_id=i.id AND j.status IN
              ('resolvendo','baixando','pausando','cancelando')) AS downloads_ativos,
            (SELECT COUNT(*) FROM jobs j WHERE j.item_id=i.id AND j.status IN ('fila','pausado')) AS downloads_fila,
            SUM(CASE WHEN c.tipo='episodio_adicionado' THEN 1 ELSE 0 END) AS novos_episodios,
            MAX(CASE WHEN c.tipo='adicionado' THEN 1 ELSE 0 END) AS titulo_novo
            FROM catalog_sync_changes c JOIN items i ON i.id=c.item_id
            WHERE c.tipo IN ('adicionado','episodio_adicionado')
            GROUP BY i.id ORDER BY i.tipo,i.titulo COLLATE NOCASE LIMIT ? OFFSET ?""",
            (limite, pagina * limite)).fetchall()
        total_additions = con.execute("""SELECT COUNT(*) FROM (
            SELECT item_id FROM catalog_sync_changes WHERE tipo IN ('adicionado','episodio_adicionado')
            GROUP BY item_id)""").fetchone()[0]
        removed = con.execute("SELECT item_tipo,titulo,ano FROM catalog_sync_changes WHERE tipo='eliminado' "
                              "ORDER BY item_tipo,titulo COLLATE NOCASE LIMIT ? OFFSET ?",
                              (limite, pagina * limite)).fetchall()
        total_removed = con.execute("SELECT COUNT(*) FROM catalog_sync_changes WHERE tipo='eliminado'").fetchone()[0]
        status = con.execute("SELECT ultima_concluida FROM catalog_sync_status WHERE id=1").fetchone()
    return {"adicionados": [dict(row) for row in additions],
            "eliminados": [dict(row) for row in removed],
            "total_adicionados": total_additions, "total_eliminados": total_removed,
            "pagina": pagina, "limite": limite,
            "sincronizado_em": status[0] if status else None}


def enriquecer_detalhes_tmdb(result: dict) -> dict:
    """Busca e guarda detalhes e elenco do TMDb sob demanda, usando o ID das fichas."""
    tmdb_id = result.get("tmdb_id")
    if not tmdb_id or result.get("atores") or not (TMDB_TOKEN or TMDB_API_KEY):
        return result
    media_type = "tv" if result.get("tipo") == "s" else "movie"
    params = {"language": "pt-BR", "append_to_response": "credits"}
    headers = {"accept": "application/json"}
    if TMDB_TOKEN:
        headers["Authorization"] = f"Bearer {TMDB_TOKEN}"
    else:
        params["api_key"] = TMDB_API_KEY
    try:
        response = requests.get(
            f"https://api.themoviedb.org/3/{media_type}/{int(tmdb_id)}",
            params=params, headers=headers, timeout=(4, 10))
        if response.status_code in (401, 403, 404):
            return result
        response.raise_for_status()
        details = response.json()
        cast = [person.get("name", "").strip()
                for person in (details.get("credits") or {}).get("cast", [])[:12]
                if isinstance(person, dict) and person.get("name")]
        genres = ", ".join(genre.get("name", "") for genre in details.get("genres", [])
                            if genre.get("name"))
        overview = str(details.get("overview") or "").strip()
        poster_path = str(details.get("poster_path") or "").strip()
        poster = f"https://image.tmdb.org/t/p/w500/{poster_path.lstrip('/')}" if poster_path else ""
        with session() as con:
            con.execute("""UPDATE items SET
                generos=CASE WHEN generos IS NULL OR generos='' THEN ? ELSE generos END,
                sinopse=CASE WHEN sinopse IS NULL OR sinopse='' THEN ? ELSE sinopse END,
                poster=CASE WHEN poster IS NULL OR poster='' THEN ? ELSE poster END,
                atores=? WHERE id=?""",
                (genres, overview, poster, json.dumps(cast, ensure_ascii=False), result["id"]))
        result["generos"] = result.get("generos") or genres
        result["sinopse"] = result.get("sinopse") or overview
        result["poster"] = result.get("poster") or poster
        result["atores"] = cast
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return result
    return result


@app.get("/api/item/{item_id}")
def item(req: Request, item_id: int):
    checar(req)
    with session() as con:
        row = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "item não encontrado")
        episodes = [dict(ep) for ep in con.execute(
            "SELECT temp,ep,versao,fontes FROM episodes WHERE item_id=? ORDER BY temp,ep,versao",
            (item_id,))]
        new_episodes = {(entry[0], entry[1], entry[2]) for entry in con.execute(
            "SELECT temp,ep,versao FROM catalog_sync_changes "
            "WHERE tipo='episodio_adicionado' AND item_id=?", (item_id,))}
    result = enriquecer_detalhes_tmdb(dict(row))
    try:
        result["fontes"] = json.loads(result.get("fontes") or "{}")
    except (TypeError, ValueError):
        result["fontes"] = {}
    for episode in episodes:
        try:
            episode["fontes"] = json.loads(episode["fontes"] or "[]")
        except (TypeError, ValueError):
            episode["fontes"] = []
        episode["novo_na_sincronizacao"] = (
            episode["temp"], episode["ep"], episode["versao"]) in new_episodes
    result["episodios"] = episodes
    actors = result.get("atores")
    if isinstance(actors, str):
        try:
            actors = json.loads(actors or "[]")
        except ValueError:
            actors = []
    result["atores"] = actors if isinstance(actors, list) else []
    return result


def selecionar_episodios(selection, available):
    """Interpreta 'primeiro', 'todos', uma lista ou faixas como 1-3,5."""
    if selection == "todos":
        return {int(ep["ep"]) for ep in available}
    if selection == "primeiro" or not selection:
        return {int(available[0]["ep"])} if available else set()
    try:
        chosen = set()
        for part in str(selection).split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start, end = (int(value.strip()) for value in part.split("-", 1))
                if end < start or end - start > 500:
                    raise ValueError
                chosen.update(range(start, end + 1))
            else:
                chosen.add(int(part))
        return chosen
    except ValueError:
        raise HTTPException(400, "episódios inválidos; use números, faixas ou 'todos'")


@app.post("/api/download")
async def download(req: Request):
    checar(req)
    try:
        body = await req.json()
        item_id = int(body.get("item_id"))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "item_id inválido")
    version = str(body.get("versao", "dub"))
    if version not in ("dub", "leg", "orig"):
        raise HTTPException(400, "versão inválida")
    original = version == "orig"
    if original:
        try:
            quality = int(body.get("max_h", 1080))
            provider_offset = max(0, min(10, int(body.get("provider_offset", 0))))
        except (ValueError, TypeError):
            raise HTTPException(400, "qualidade ou servidor inválido")
        if quality not in (480, 720, 1080):
            raise HTTPException(400, "qualidade deve ser 480, 720 ou 1080")
        legenda_ativa = bool(body.get("legenda", True))
    else:
        quality, provider_offset, legenda_ativa = 1080, 0, False
    with session() as con:
        row = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "item não encontrado")
        if original and "en" not in (row["idioma"] or "").split(","):
            raise HTTPException(400, "este título não está no catálogo EN")
        episodes = []
        selected_episodes = []
        if row["tipo"] == "s":
            episodes = con.execute("SELECT temp,ep,fontes FROM episodes WHERE item_id=? AND versao=? "
                                    "ORDER BY temp,ep", (item_id, version)).fetchall()
            if not episodes:
                raise HTTPException(400, "sem episódios nesta versão")
            scope = str(body.get("alcance", "episodios"))
            if scope == "serie":
                selected_episodes = episodes
            elif scope in ("episodios", "temporada"):
                try:
                    season = int(body.get("temporada") or episodes[0]["temp"])
                except (ValueError, TypeError):
                    raise HTTPException(400, "temporada inválida")
                season_rows = [ep for ep in episodes if ep["temp"] == season]
                if not season_rows:
                    raise HTTPException(400, "temporada não encontrada")
                selected_episodes = (season_rows if scope == "temporada" else
                                     [ep for ep in season_rows if ep["ep"] in selecionar_episodios(
                                         body.get("episodios", "primeiro"), season_rows)])
            else:
                raise HTTPException(400, "alcance inválido")
        else:
            selected = set()
        title = core.limpar(f"{row['titulo']} ({row['ano']})" if row["ano"] else row["titulo"])
        media_folder = "series" if row["tipo"] == "s" else "movies"
        media_root = os.path.join(core.DESTINO, media_folder)
        created = []
        pending = []
        if row["tipo"] == "f":
            try:
                source_map = json.loads(row["fontes"] or "{}")
            except (TypeError, ValueError):
                source_map = {}
            sources = source_map.get(version) or []
            if not sources:
                raise HTTPException(400, "sem fonte nesta versão")
            job_title = f"{row['titulo']} [{version}]"
            directory = os.path.join(media_root, title)
            Path(directory).mkdir(parents=True, exist_ok=True)
            output_base = os.path.join(directory, f"{title} [{version}]")
            task_data = {"sources": sources, "output_base": output_base}
            if original:
                task_data.update({"version": "orig", "max_h": quality,
                                  "provider_offset": provider_offset, "legenda": legenda_ativa,
                                  "tmdb_id": row["tmdb_id"],
                                  "kind": "tv" if row["tipo"] == "s" else "movie"})
            task_json = json.dumps(task_data)
            cursor = con.execute("INSERT INTO jobs(titulo,status,task_json,item_id) VALUES(?, 'fila', ?, ?)",
                                 (job_title, task_json, item_id))
            job_id = cursor.lastrowid
            pending.append((job_id, job_title, sources, output_base))
            created.append(job_id)
        else:
            for episode in selected_episodes:
                try:
                    source_map = json.loads(episode["fontes"] or "[]")
                    sources = source_map.get(version, []) if isinstance(source_map, dict) else source_map
                except (TypeError, ValueError):
                    sources = []
                if not sources:
                    continue
                episode_title = f"{title} S{episode['temp']:02d}E{episode['ep']:02d} [{version}]"
                season_directory = os.path.join(media_root, title, f"Season {episode['temp']:02d}")
                Path(season_directory).mkdir(parents=True, exist_ok=True)
                file_title = f"{title} - S{episode['temp']:02d}E{episode['ep']:02d} [{version}]"
                output_base = os.path.join(season_directory, file_title)
                task_data = {"sources": sources, "output_base": output_base}
                if original:
                    task_data.update({"version": "orig", "max_h": quality,
                                      "provider_offset": provider_offset, "legenda": legenda_ativa,
                                      "tmdb_id": row["tmdb_id"], "kind": "tv",
                                      "season": int(episode["temp"]), "episode": int(episode["ep"])})
                task_json = json.dumps(task_data)
                cursor = con.execute("INSERT INTO jobs(titulo,status,task_json,item_id) VALUES(?, 'fila', ?, ?)",
                                     (episode_title, task_json, item_id))
                job_id = cursor.lastrowid
                pending.append((job_id, episode_title, sources, output_base))
                created.append(job_id)
        con.commit()
    for task in pending:
        enfileirar(task)
    if not created:
        raise HTTPException(400, "não há fontes para os episódios selecionados")
    return {"jobs": created}


@app.get("/api/jobs")
def jobs(req: Request, pagina: int = 0, limite: int = 25):
    checar(req)
    pagina = max(0, pagina)
    limite = max(1, min(100, limite))
    with session() as con:
        total = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE hidden=0 AND status!='concluido'"
        ).fetchone()[0]
        paginas = (total + limite - 1) // limite
        pagina = min(pagina, max(0, paginas - 1))
        rows = [dict(row) for row in con.execute(
            "SELECT * FROM jobs WHERE hidden=0 AND status!='concluido' ORDER BY CASE status "
            "WHEN 'baixando' THEN 0 WHEN 'resolvendo' THEN 1 WHEN 'pausando' THEN 2 "
            "WHEN 'cancelando' THEN 3 WHEN 'fila' THEN 4 WHEN 'pausado' THEN 5 "
            "WHEN 'erro' THEN 6 WHEN 'cancelado' THEN 7 ELSE 8 END, "
            "id DESC LIMIT ? OFFSET ?", (limite, pagina * limite))]
        active_rows = [dict(row) for row in con.execute(
            "SELECT * FROM jobs WHERE hidden=0 AND status IN "
            "('resolvendo','baixando','pausando','cancelando') ORDER BY id DESC")]
        counts = {row["status"]: row["total"] for row in
                  con.execute("SELECT status,COUNT(*) AS total FROM jobs WHERE hidden=0 GROUP BY status")}
    return {"jobs": rows, "ativos": active_rows, "counts": counts, "total": total, "pagina": pagina,
            "limite": limite, "paginas": paginas}


@app.get("/api/jobs/completed")
def completed_jobs(req: Request, pagina: int = 0, limite: int = 25):
    checar(req)
    pagina = max(0, pagina)
    limite = max(1, min(50, limite))
    with session() as con:
        total = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE hidden=0 AND status='concluido'").fetchone()[0]
        paginas = (total + limite - 1) // limite
        pagina = min(pagina, max(0, paginas - 1))
        rows = [dict(row) for row in con.execute(
            "SELECT id,titulo,arquivo,criado,aviso FROM jobs "
            "WHERE hidden=0 AND status='concluido' ORDER BY id DESC LIMIT ? OFFSET ?",
            (limite, pagina * limite))]
    return {"jobs": rows, "total": total, "pagina": pagina,
            "limite": limite, "paginas": paginas}


@app.get("/api/storage")
def storage(req: Request):
    checar(req)
    usage = shutil.disk_usage(core.DESTINO)
    used = usage.total - usage.free
    percent_used = (used / usage.total * 100) if usage.total else 0
    return {"pasta": core.DESTINO, "total": usage.total, "usado": used,
            "livre": usage.free, "percentual_usado": round(percent_used, 1)}


@app.post("/api/jobs/pause-all")
def pause_all_jobs(req: Request):
    checar(req)
    pausing = 0
    paused = 0
    with controle_lock:
        with session() as con:
            rows = con.execute(
                "SELECT id,status FROM jobs WHERE hidden=0 AND status IN ('fila','resolvendo','baixando')"
            ).fetchall()
            for row in rows:
                job_id, status = int(row["id"]), row["status"]
                if status == "fila":
                    con.execute("UPDATE jobs SET status='pausado' WHERE id=?", (job_id,))
                    paused += 1
                    continue
                control = controles.get(job_id)
                if control:
                    control.pause()
                    con.execute("UPDATE jobs SET status='pausando' WHERE id=?", (job_id,))
                    pausing += 1
                else:
                    con.execute("UPDATE jobs SET status='pausado' WHERE id=?", (job_id,))
                    paused += 1
    return {"ok": True, "pausados": paused, "pausando": pausing}


@app.post("/api/jobs/resume-all")
def resume_all_jobs(req: Request):
    checar(req)
    tasks = []
    errors = 0
    with session() as con:
        rows = con.execute(
            "SELECT id,titulo,task_json FROM jobs WHERE hidden=0 AND status='pausado' ORDER BY id"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["task_json"] or "")
                task = (int(row["id"]), row["titulo"], payload["sources"], payload["output_base"])
                con.execute("UPDATE jobs SET status='fila',erro=NULL WHERE id=?", (row["id"],))
                tasks.append(task)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                con.execute("UPDATE jobs SET status='erro',erro=? WHERE id=?",
                            ("dados do download não permitem retomada", row["id"]))
                errors += 1
    for task in tasks:
        enfileirar(task)
    return {"ok": True, "retomados": len(tasks), "erros": errors}


@app.post("/api/jobs/retry-all")
def retry_all_jobs(req: Request):
    checar(req)
    tasks = []
    errors = 0
    with session() as con:
        rows = con.execute(
            "SELECT id,titulo,task_json FROM jobs "
            "WHERE hidden=0 AND status='erro' ORDER BY id"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["task_json"] or "")
                sources = payload["sources"]
                output_base = payload["output_base"]
                if not isinstance(sources, list) or not sources or not isinstance(output_base, str):
                    raise ValueError("payload inválido")
                job_id = int(row["id"])
                updated = con.execute(
                    "UPDATE jobs SET status='fila',pct=0,erro=NULL,bytes_done=0,bytes_total=0,arquivo=NULL "
                    "WHERE id=? AND status='erro'", (job_id,)
                ).rowcount
                if updated:
                    tasks.append((job_id, row["titulo"], sources, output_base))
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                errors += 1
    for task in tasks:
        enfileirar(task)
    return {"ok": True, "retomados": len(tasks), "erros": errors}


@app.post("/api/jobs/{job_id}/pause")
def pause_job(req: Request, job_id: int):
    checar(req)
    with controle_lock:
        with session() as con:
            row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise HTTPException(404, "job não encontrado")
            status = row["status"]
            if status == "pausado":
                return {"ok": True, "status": status}
            if status == "fila":
                con.execute("UPDATE jobs SET status='pausado' WHERE id=?", (job_id,))
                return {"ok": True, "status": "pausado"}
            if status not in ("resolvendo", "baixando"):
                raise HTTPException(409, "este download não pode ser pausado agora")
            control = controles.get(job_id)
            if control:
                control.pause()
                next_status = "pausando"
            else:
                next_status = "pausado"
            con.execute("UPDATE jobs SET status=? WHERE id=?", (next_status, job_id))
    return {"ok": True, "status": next_status}


@app.post("/api/jobs/{job_id}/resume")
def resume_job(req: Request, job_id: int):
    checar(req)
    task = None
    with session() as con:
        row = con.execute("SELECT titulo,status,task_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "job não encontrado")
        if row["status"] != "pausado":
            raise HTTPException(409, "o download não está pausado")
        try:
            payload = json.loads(row["task_json"] or "")
            task = (job_id, row["titulo"], payload["sources"], payload["output_base"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise HTTPException(409, "dados do download não permitem retomada")
        con.execute("UPDATE jobs SET status='fila',erro=NULL WHERE id=?", (job_id,))
    if task:
        enfileirar(task)
    return {"ok": True, "status": "fila"}


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(req: Request, job_id: int):
    checar(req)
    try:
        body = await req.json()
    except (ValueError, TypeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    task = None
    with session() as con:
        row = con.execute("SELECT titulo,status,task_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "download não encontrado")
        if row["status"] != "erro":
            raise HTTPException(409, "só é possível tentar novamente downloads com erro")
        try:
            payload = json.loads(row["task_json"] or "")
            sources = payload["sources"]
            output_base = payload["output_base"]
            if not isinstance(sources, list) or not sources or not isinstance(output_base, str):
                raise ValueError("payload inválido")
            if body.get("outro_servidor") and payload.get("version") == "orig":
                payload["provider_offset"] = int(payload.get("provider_offset") or 0) + 1
                con.execute("UPDATE jobs SET task_json=? WHERE id=?",
                            (json.dumps(payload), job_id))
            if body.get("outra_qualidade") and payload.get("version") == "orig":
                current_quality = int(payload.get("max_h") or 1080)
                payload["max_h"] = {1080: 720, 720: 480, 480: 1080}.get(current_quality, 720)
                con.execute("UPDATE jobs SET task_json=? WHERE id=?",
                            (json.dumps(payload), job_id))
            task = (job_id, row["titulo"], sources, output_base)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise HTTPException(409, "dados do download não permitem nova tentativa")
        updated = con.execute(
            "UPDATE jobs SET status='fila',pct=0,erro=NULL,bytes_done=0,bytes_total=0,arquivo=NULL "
            "WHERE id=? AND status='erro'", (job_id,)).rowcount
        if not updated:
            raise HTTPException(409, "o download mudou de estado; atualize a lista")
    enfileirar(task)
    return {"ok": True, "status": "fila"}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(req: Request, job_id: int):
    checar(req)
    output_base = None
    active = False
    with controle_lock:
        with session() as con:
            row = con.execute("SELECT status,task_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise HTTPException(404, "job não encontrado")
            if row["status"] in ("concluido", "erro", "cancelado"):
                raise HTTPException(409, "este download já terminou")
            try:
                output_base = json.loads(row["task_json"] or "")["output_base"]
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                raise HTTPException(409, "dados do download não permitem limpeza segura")
            if row["status"] in ("resolvendo", "baixando", "pausando", "cancelando"):
                control = controles.get(job_id)
                if control:
                    control.cancel()
                    con.execute("UPDATE jobs SET status='cancelando' WHERE id=?", (job_id,))
                else:
                    con.execute("UPDATE jobs SET status='cancelado',pct=0,arquivo=NULL WHERE id=?",
                                (job_id,))
            else:
                con.execute("UPDATE jobs SET status='cancelado',pct=0,arquivo=NULL WHERE id=?", (job_id,))
    if output_base:
        # Se o worker ainda está ativo, ele limpa no próximo ponto de controle.
        with controle_lock:
            active = job_id in controles
        if not active:
            limpar_dados_job(output_base)
    return {"ok": True, "status": "cancelando" if active else "cancelado"}


@app.delete("/api/jobs/{job_id}")
def delete_job(req: Request, job_id: int):
    checar(req)
    with session() as con:
        row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "job não encontrado")
        if row["status"] in ("fila", "resolvendo", "baixando", "pausando", "pausado", "cancelando"):
            raise HTTPException(409, "não é possível apagar um download em andamento")
        con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    return {"ok": True}


@app.post("/api/jobs/clear")
async def clear_jobs(req: Request):
    checar(req)
    try:
        body = await req.json()
        scope = body.get("scope")
    except (ValueError, AttributeError):
        raise HTTPException(400, "escopo inválido")
    if scope not in ("completed", "list"):
        raise HTTPException(400, "escopo inválido")

    temporary_paths = []
    active_statuses = ("resolvendo", "baixando", "pausando", "cancelando")
    with controle_lock:
        with session() as con:
            if scope == "completed":
                rows = con.execute("SELECT id FROM jobs WHERE hidden=0 AND status='concluido'").fetchall()
            else:
                rows = con.execute(
                    "SELECT id,status,task_json FROM jobs WHERE hidden=0 AND status IN "
                    "('fila','pausado','concluido','erro','cancelado')").fetchall()
                for row in rows:
                    if row["status"] == "pausado":
                        try:
                            temporary_paths.append(json.loads(row["task_json"] or "")["output_base"])
                        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                            pass
            ids = [int(row["id"]) for row in rows]
            if ids:
                con.executemany(
                    "UPDATE jobs SET hidden=1,status=CASE WHEN status IN ('fila','pausado') "
                    "THEN 'cancelado' ELSE status END,pct=CASE WHEN status IN ('fila','pausado') "
                    "THEN 0 ELSE pct END WHERE id=?", [(job_id,) for job_id in ids])

    for output_base in temporary_paths:
        limpar_progresso_job(output_base)
    remaining = _contagens_jobs() if scope == "list" else {}
    return {"ok": True, "limpos": len(ids),
            "ativos_preservados": sum(count for status, count in remaining.items()
                                      if status in active_statuses)}


def _contagens_jobs() -> dict[str, int]:
    with session() as con:
        return {row["status"]: row["total"] for row in
                con.execute("SELECT status,COUNT(*) AS total FROM jobs WHERE hidden=0 GROUP BY status")}


@app.get("/api/arquivos")
def files(req: Request):
    checar(req)
    result = []
    for root, _, names in os.walk(core.DESTINO):
        for name in names:
            if name.lower().endswith((".mp4", ".mkv")):
                path = os.path.join(root, name)
                result.append({"nome": os.path.relpath(path, core.DESTINO),
                               "tamanho": os.path.getsize(path),
                               "url": ticket_media(os.path.relpath(path, core.DESTINO))})
    return {"arquivos": sorted(result, key=lambda entry: entry["nome"].casefold())}


def media_path(relative: str) -> str:
    root = Path(core.DESTINO).resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise HTTPException(404, "arquivo não encontrado")
    return str(candidate)


@app.get("/media/{caminho:path}")
def media(req: Request, caminho: str, expires: int = 0, sig: str = ""):
    header_ok = bool(TOKEN and req.headers.get("x-token") == TOKEN)
    cookie_ok = bool(TOKEN and validar_sessao_media(req.cookies.get("saimo_media", "")))
    ticket_ok = bool(TOKEN and validar_ticket(caminho, expires, sig))
    if TOKEN and not (header_ok or cookie_ok or ticket_ok):
        raise HTTPException(401, "token inválido ou link expirado")
    response = FileResponse(media_path(caminho))
    if ticket_ok and not cookie_ok:
        cookie, expires_at = criar_sessao_media()
        response.set_cookie("saimo_media", cookie, max_age=max(0, expires_at - int(time.time())),
                            httponly=True, secure=req.url.scheme == "https", samesite="lax",
                            path="/media")
    return response


@app.delete("/media/{caminho:path}")
def remove_media(req: Request, caminho: str):
    checar(req)
    os.remove(media_path(caminho))
    return {"ok": True}


@app.get("/api/config")
def config(req: Request):
    checar(req)
    with session() as con:
        item_count = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        poster_rows = con.execute(
            "SELECT poster FROM items WHERE poster IS NOT NULL AND poster!=''").fetchall()
    local_posters = sum(
        1 for row in poster_rows
        if row["poster"].startswith("/posters/")
        and (POSTERS / Path(row["poster"]).name).is_file()
    )
    remote_posters = sum(1 for row in poster_rows
                         if row["poster"].startswith(("http://", "https://")))
    return {"pasta": core.DESTINO, "qualidade": core.QUALIDADE,
            "existe_ffmpeg": shutil.which("ffmpeg") is not None,
            "catalogo_sincronizado": item_count > 0, "total_itens": item_count,
            "itens_com_capa": len(poster_rows), "capas_locais_encontradas": local_posters,
            "capas_remotas": remote_posters}


app.mount("/posters", StaticFiles(directory=str(POSTERS)), name="posters")
app.mount("/", StaticFiles(directory="static", html=True), name="ui")
