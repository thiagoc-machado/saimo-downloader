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

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import saimo_core as core

load_dotenv()
DB = Path(os.getenv("SAIMO_DB", "./data/catalog.db")).expanduser().resolve()
TOKEN = os.getenv("SAIMO_TOKEN", "")
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


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=30, check_same_thread=False)
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
        con.executescript("""
        CREATE TABLE IF NOT EXISTS items(
          id INTEGER PRIMARY KEY, tipo TEXT NOT NULL, titulo TEXT NOT NULL,
          titulo_norm TEXT NOT NULL, ano TEXT NOT NULL DEFAULT '', letra TEXT,
          pedaco INTEGER DEFAULT 0, poster TEXT, generos TEXT DEFAULT '',
          sinopse TEXT DEFAULT '', fontes TEXT DEFAULT '{}', eps INTEGER DEFAULT 0,
          destaque INTEGER DEFAULT 0, UNIQUE(tipo, titulo, ano));
        CREATE TABLE IF NOT EXISTS episodes(
          item_id INTEGER NOT NULL, temp INTEGER NOT NULL, ep INTEGER NOT NULL,
          versao TEXT NOT NULL, fontes TEXT NOT NULL DEFAULT '[]',
          PRIMARY KEY(item_id, temp, ep, versao));
        CREATE INDEX IF NOT EXISTS ix_busca ON items(titulo_norm);
        CREATE INDEX IF NOT EXISTS ix_tipo ON items(tipo, letra);
        CREATE INDEX IF NOT EXISTS ix_episode_item ON episodes(item_id, temp, ep);
        """)
        con.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY, titulo TEXT NOT NULL, arquivo TEXT, status TEXT NOT NULL,
            pct REAL DEFAULT 0, erro TEXT, criado TEXT DEFAULT CURRENT_TIMESTAMP,
            task_json TEXT)""")
        columns = {row[1] for row in con.execute("PRAGMA table_info(jobs)")}
        if "task_json" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN task_json TEXT")
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
                              "WHERE status IN ('fila','resolvendo','baixando','pausando','pausado','cancelando')").fetchall()
        for row in pending:
            try:
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
            control.checkpoint()
            resolved = core.resolver_fontes(sources, control)
            if not resolved:
                con.execute("UPDATE jobs SET status='erro', erro=? WHERE id=?", ("sem fonte disponível", job_id))
                con.commit()
                continue

            last_progress = {"time": 0.0, "bytes": -1.0}

            def progress(done: int, total: int) -> None:
                control.checkpoint()
                percent = min(99.9, done / total * 100) if total else 0
                with progress_lock:
                    now = time.monotonic()
                    final_update = total > 0 and done >= total
                    if not final_update and done - last_progress["bytes"] < (1 << 20) and now - last_progress["time"] < 0.5:
                        return
                    con.execute("UPDATE jobs SET pct=?, status='baixando' WHERE id=?", (percent, job_id))
                    con.commit()
                    last_progress["time"] = now
                    last_progress["bytes"] = done

            result = core.baixar(resolved, output_base, progress, control)
            with controle_lock:
                control.checkpoint()
                if result:
                    relative = os.path.relpath(result, core.DESTINO)
                    con.execute("UPDATE jobs SET status='concluido', pct=100, arquivo=? WHERE id=?",
                                (relative, job_id))
                else:
                    con.execute("UPDATE jobs SET status='erro', erro=? WHERE id=?",
                                ("todas as fontes falharam", job_id))
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


@app.get("/api/items")
def items(req: Request, q: str = "", tipo: str = "", pagina: int = 0, limite: int = 60):
    checar(req)
    pagina = max(0, pagina)
    limite = max(1, min(100, limite))
    conditions, values = ["1=1"], []
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
            f"SELECT id,tipo,titulo,ano,poster,generos,eps,destaque FROM items WHERE {where} "
            "ORDER BY destaque DESC, titulo COLLATE NOCASE LIMIT ? OFFSET ?",
            values + [limite, pagina * limite])]
        total = con.execute(f"SELECT COUNT(*) FROM items WHERE {where}", values).fetchone()[0]
        catalog_count = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    return {"items": rows, "total": total, "pagina": pagina,
            "catalogo_sincronizado": catalog_count > 0}


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
    result = dict(row)
    try:
        result["fontes"] = json.loads(result.get("fontes") or "{}")
    except (TypeError, ValueError):
        result["fontes"] = {}
    for episode in episodes:
        try:
            episode["fontes"] = json.loads(episode["fontes"] or "[]")
        except (TypeError, ValueError):
            episode["fontes"] = []
    result["episodios"] = episodes
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
    if version not in ("dub", "leg"):
        raise HTTPException(400, "versão inválida")
    with session() as con:
        row = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "item não encontrado")
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
        directory = os.path.join(core.DESTINO, title)
        Path(directory).mkdir(parents=True, exist_ok=True)
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
            output_base = os.path.join(directory, f"{title} [{version}]")
            task_json = json.dumps({"sources": sources, "output_base": output_base})
            cursor = con.execute("INSERT INTO jobs(titulo,status,task_json) VALUES(?, 'fila', ?)",
                                 (job_title, task_json))
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
                output_base = os.path.join(directory, episode_title)
                task_json = json.dumps({"sources": sources, "output_base": output_base})
                cursor = con.execute("INSERT INTO jobs(titulo,status,task_json) VALUES(?, 'fila', ?)",
                                     (episode_title, task_json))
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
def jobs(req: Request):
    checar(req)
    with session() as con:
        rows = [dict(row) for row in con.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 80")]
    return {"jobs": rows}


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
