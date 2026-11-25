#!/usr/bin/env python3
"""Resolvedores EN para o saimo: yt-dlp, vidsrc e captura HLS via Playwright.

Retorna lista de candidatos {"url","headers","quality","provider","subs"}.
Nunca abre browser quando há caminho por requests. Uso interno apenas.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote, urljoin

import requests
import saimo_core as core

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TIMEOUT = (5, 25)
MAX_H = int(os.getenv("SAIMO_EN_MAX_H", "1080"))
YTDLP = os.getenv("SAIMO_YTDLP", "yt-dlp")
MAX_PROVIDERS = max(1, min(2, int(os.getenv("SAIMO_EN_PROVIDERS", "2"))))
MAX_CANDIDATOS = 4
OS_API_URL = "https://api.opensubtitles.com/api/v1"
OS_API_KEY = (os.getenv("SAIMO_OPENSUBTITLES_API_KEY") or
              os.getenv("OPENSUBTITLES_API_KEY", "")).strip()
OS_UA = "Saimo Downloader v1.0"

class ResolveError(Exception):
    def __init__(self, code: str, detalhe: str = ""):
        super().__init__(f"{code}: {detalhe}"[:300])
        self.code = code


def _get(url: str, referer: str | None = None, **kw) -> requests.Response:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    headers.update(kw.pop("headers", {}) or {})
    if referer:
        headers["Referer"] = referer
    last = None
    for tentativa in range(2):
        try:
            r = requests.get(url, headers=headers, timeout=TIMEOUT, **kw)
            if r.status_code < 500:
                return r
            last = ResolveError("http_5xx", str(r.status_code))
        except requests.RequestException as exc:
            last = ResolveError("rede", type(exc).__name__)
        time.sleep(0.6 * (tentativa + 1))
    raise last or ResolveError("rede")


def _norm(url: str, headers: dict | None = None, quality: int | None = None,
          provider: str = "") -> dict:
    if url.startswith("//"):
        url = "https:" + url
    return {"url": url, "headers": headers or {}, "quality": quality or 0,
            "provider": provider, "subs": []}


def _m3u8s(texto: str) -> list[str]:
    achados = re.findall(r'https?://[^\s"\'\\<>]+?\.m3u8[^\s"\'\\<>]*', texto)
    return list(dict.fromkeys(a.replace("\\/", "/") for a in achados))


def _rc4(chave: list[int], dados: str) -> str:
    """RC4 do vidplay. Entrada base64, saída URL."""
    import base64
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + chave[i % len(chave)]) % 256
        s[i], s[j] = s[j], s[i]
    bruto = base64.b64decode(dados)
    i = j = 0
    saida = bytearray()
    for byte in bruto:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        saida.append(byte ^ s[(s[i] + s[j]) % 256])
    return saida.decode("utf-8", "replace")


# ---------------------------------------------------------------- resolvedores

def _ytdlp(src: dict, control) -> list[dict]:
    url = str(src["url"])
    altura = max(144, min(int(src.get("max_h") or MAX_H), MAX_H))
    base = [YTDLP, "-J", "--no-warnings", "--no-playlist",
            "--socket-timeout", "20", "--extractor-retries", "1",
            "--retries", "1", "--fragment-retries", "1"]
    control.checkpoint()
    seletor = f"b[height<={altura}]/bv*[height<={altura}]+ba"
    try:
        proc = subprocess.run(base + ["-f", seletor, url],
                              capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise ResolveError("ytdlp_execucao", type(exc).__name__) from exc
    if proc.returncode:
        raise ResolveError("ytdlp_vazio", proc.stderr[-200:])
    try:
        dados = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ResolveError("ytdlp_json") from exc
    limite = int(src.get("max_h") or MAX_H)
    if dados.get("url") and int(dados.get("height") or 0) <= limite:
        return [_norm(dados["url"], dados.get("http_headers"), dados.get("height"), "ytdlp")]
    pedacos = dados.get("requested_formats") or []
    if len(pedacos) == 2:
        video = next((f for f in pedacos if f.get("vcodec") != "none"), None)
        audio = next((f for f in pedacos if f.get("acodec") != "none"), None)
        if video and audio:
            if int(video.get("height") or 0) > limite:
                raise ResolveError("ytdlp_qualidade")
            cand = _norm(video["url"], video.get("http_headers"), video.get("height"), "ytdlp")
            cand["audio_url"] = audio["url"]
            cand["audio_headers"] = audio.get("http_headers", {})
            cand["duration"] = dados.get("duration")
            return [cand]
    formatos = [f for f in dados.get("formats") or []
                if f.get("url") and f.get("vcodec") != "none"
                and (f.get("height") or 0) <= limite]
    if formatos:
        melhor = max(formatos, key=lambda f: f.get("height") or 0)
        return [_norm(melhor["url"], melhor.get("http_headers"), melhor.get("height"), "ytdlp")]
    raise ResolveError("ytdlp_vazio")


def _vidplay(hash_: str, control, embed: str) -> list[dict]:
    ref = embed
    pagina = _get(f"https://vidplay.online/rcp/{hash_}", ref)
    if pagina.status_code != 200:
        raise ResolveError("vidplay_pagina", str(pagina.status_code))
    chave = re.search(r"const\s+keys?\s*=\s*\[([^\]]+)\]", pagina.text)
    ident = re.search(r'data-id="([^"]+)"', pagina.text)
    if not chave or not ident:
        raise ResolveError("vidplay_layout")
    control.checkpoint()
    info = _get(f"https://vidplay.online/mediainfo/{ident.group(1)}", ref)
    dados = info.json()
    fonte = (dados.get("result") or {}).get("sources") or []
    if not fonte:
        raise ResolveError("vidplay_sem_fonte")
    k = [int(v) for v in chave.group(1).split(",")]
    return [_norm(_rc4(k, item["file"]), {"Referer": ref}, None, "vidplay")
            for item in fonte if item.get("file")]


def _generico(url: str, control, embed: str) -> list[dict]:
    pagina = _get(url, embed)
    urls = _m3u8s(pagina.text)
    if not urls:
        raise ResolveError("sem_m3u8")
    return [_norm(u, {"Referer": embed}, None, "generico") for u in urls]


SERVIDORES: dict[str, Callable[[str, object, str], list[dict]]] = {
    "vidplay": _vidplay,
    "cloudmoon": lambda h, c, e: _generico(f"https://cloudmoon.xyz/embed/{h}", c, e),
    "v2": lambda h, c, e: _generico(f"https://v2.vidsrc.me/embed/{h}", c, e),
    "mp4upload": lambda h, c, e: _generico(f"https://www.mp4upload.com/embed-{h}.html", c, e),
}


def _vidsrc(src: dict, control) -> list[dict]:
    kind, tmdb = src["kind"], int(src["tmdb"])
    embed = (f"https://vidsrc.to/embed/tv/{tmdb}/{src['season']}/{src['episode']}"
             if kind == "tv" else f"https://vidsrc.to/embed/movie/{tmdb}")
    pagina = _get(embed)
    achado = re.search(r'data-id="([^"]+)"', pagina.text)
    if not achado:
        raise ResolveError("vidsrc_layout")
    control.checkpoint()
    rota = "movie" if kind == "movie" else "episode"
    lista = _get(f"https://vidsrc.to/ajax/embed/{rota}/{achado.group(1)}/sources").json()
    saida: list[dict] = []
    for servidor in (lista.get("result") or [])[:2]:
        control.checkpoint()
        fn = SERVIDORES.get(str(servidor.get("title", "")).lower())
        if not fn:
            continue
        try:
            saida.extend(fn(servidor["id"], control, embed))
        except (ResolveError, requests.RequestException, KeyError, ValueError):
            continue
        if saida:
            break
    if not saida:
        raise ResolveError("vidsrc_sem_fonte")
    return saida


def _capture(src: dict, control) -> list[dict]:
    from playwright.sync_api import sync_playwright
    kind, tmdb = src["kind"], int(src["tmdb"])
    embed = (f"https://vidsrc.sh/embed/tv/{tmdb}/{src['season']}/{src['episode']}"
             if kind == "tv" else f"https://vidsrc.sh/embed/movie/{tmdb}")
    capturados: list[str] = []
    control.checkpoint()
    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True, args=["--mute-audio", "--no-sandbox"])
        try:
            ctx = navegador.new_context(user_agent=UA, viewport={"width": 1280, "height": 720})
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            pagina = ctx.new_page()
            pagina.route(re.compile(r"(googletagmanager|histats|doubleclick|analytics)"),
                         lambda rota: rota.abort())
            pagina.on("request", lambda r: capturados.append(r.url)
                      if ".m3u8" in r.url and r.url not in capturados else None)
            pagina.goto(embed, wait_until="domcontentloaded", timeout=30000)
            for _ in range(24):
                control.checkpoint()
                if capturados:
                    break
                for seletor in (".vjs-big-play-button", "[data-play]", "button[class*=play]",
                                "video"):
                    try:
                        pagina.click(seletor, timeout=900)
                    except Exception:
                        pass
                pagina.wait_for_timeout(600)
        finally:
            navegador.close()
    if not capturados:
        raise ResolveError("capture_sem_m3u8")
    master = next((u for u in capturados if "master" in u), capturados[0])
    cand = _norm(master, {"Referer": embed, "Origin": "https://vidsrc.sh"},
                 None, "capture")
    try:
        manifest = _get(master, headers=cand["headers"]).text
        for linha in manifest.splitlines():
            if "TYPE=SUBTITLES" in linha:
                nome = re.search(r'NAME="([^"]*)"', linha)
                uri = re.search(r'URI="([^"]+)"', linha)
                if uri:
                    cand["subs"].append({"lang": (nome.group(1) if nome else "?"),
                                         "url": urljoin(master, uri.group(1))})
    except (ResolveError, requests.RequestException):
        pass
    return [cand]


RESOLVEDORES: dict[str, Callable[[dict, object], list[dict]]] = {
    "ytdlp": _ytdlp,
    "vidsrc": _vidsrc,
    "capture": _capture,
}


def resolver_fontes(sources: Iterable[dict], control, provider_offset: int = 0) -> list[dict]:
    saida: list[dict] = []
    descritores = [src for src in sources or [] if isinstance(src, dict)]
    if descritores and provider_offset:
        inicio = provider_offset % len(descritores)
        descritores = descritores[inicio:] + descritores[:inicio]
    tentadas = 0
    for src in descritores:
        if tentadas >= MAX_PROVIDERS:
            break
        control.checkpoint()
        tentadas += 1
        fn = RESOLVEDORES.get(src.get("provider", ""))
        try:
            if fn:
                saida.extend(fn(src, control))
            elif ".m3u8" in str(src.get("url", "")):
                saida.append(_norm(src["url"], src.get("headers")))
        except (ResolveError, requests.RequestException, subprocess.SubprocessError,
                OSError, ValueError, KeyError):
            continue
        except (core.DownloadPaused, core.DownloadCancelled):
            raise
        except Exception:
            continue
        if len(saida) >= MAX_CANDIDATOS:
            break
    return sorted(saida, key=lambda c: c.get("quality") or 0, reverse=True)


# ------------------------------------------------------------------ downloader

def _progresso(linha: str) -> tuple[int, int] | None:
    """Formato: 'D <baixado> <total> <estimado>'."""
    if not linha.startswith("D "):
        return None
    partes = linha.split()
    if len(partes) < 3:
        return None
    try:
        baixado = int(partes[1] or 0)
        total = int(partes[2] or 0) or int(partes[3] or 0)
    except (ValueError, IndexError):
        return None
    return baixado, total


def baixar_ytdlp(cand: dict, output_base: str, progress, control) -> str | None:
    """Baixa candidato EN, usando ffmpeg para unir fluxos separados e yt-dlp nos demais."""
    alvo = Path(output_base)
    alvo.parent.mkdir(parents=True, exist_ok=True)
    headers = cand.get("headers") if isinstance(cand.get("headers"), dict) else {}
    audio_headers = cand.get("audio_headers") if isinstance(cand.get("audio_headers"), dict) else headers
    if cand.get("audio_url"):
        final = Path(str(alvo) + ".mp4")
        temporary = Path(str(final) + ".part")
        temporary.unlink(missing_ok=True)
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-rw_timeout", "25000000"]
        if headers:
            cmd += ["-headers", _format_headers(headers)]
        cmd += ["-i", str(cand["url"]), "-rw_timeout", "25000000"]
        if audio_headers:
            cmd += ["-headers", _format_headers(audio_headers)]
        cmd += ["-i", str(cand["audio_url"]), "-map", "0:v:0", "-map", "1:a:0",
                "-c", "copy", "-movflags", "+faststart", "-progress", "pipe:1",
                "-nostats", "-f", "mp4", str(temporary)]
    else:
        cmd = [YTDLP, "-c", "--newline", "--no-warnings", "--no-playlist",
               "--socket-timeout", "20", "--retries", "1", "--fragment-retries", "1",
               "--progress-template", "download:D %(progress.downloaded_bytes)s "
               "%(progress.total_bytes)s %(progress.total_bytes_estimate)s",
               "--merge-output-format", "mp4", "--remux-video", "mp4",
               "-o", str(alvo) + ".%(ext)s"]
        for key, value in headers.items():
            if value is not None and "\n" not in str(value) and "\r" not in str(value):
                cmd += ["--add-headers", f"{key}:{value}"]
        cmd.append(cand["url"])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    duration = float(cand.get("duration") or 0)
    try:
        for linha in proc.stdout or []:
            control.checkpoint()
            medido = _progresso(linha.strip())
            if medido and medido[1]:
                progress(medido[0], medido[1], medido[0], medido[1])
            elif cand.get("audio_url") and linha.startswith("out_time_ms=") and duration > 0:
                try:
                    done = int(linha.split("=", 1)[1])
                    total = int(duration * 1_000_000)
                    progress(min(done, total), total, min(done, total), total)
                except ValueError:
                    pass
        control.checkpoint()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    if proc.returncode != 0:
        return None
    if cand.get("audio_url"):
        if temporary.is_file() and temporary.stat().st_size:
            os.replace(temporary, final)
            return str(final)
        return None
    for extensao in (".mp4", ".mkv", ".webm", ".m4a"):
        achado = Path(str(alvo) + extensao)
        if achado.is_file() and achado.stat().st_size:
            return str(achado)
    return None


def _format_headers(headers: dict) -> str:
    return "".join(f"{key}: {str(value).replace(chr(13), '').replace(chr(10), '')}\r\n"
                   for key, value in headers.items() if value is not None)


# ------------------------------------------------------------------ legendas

def buscar_legenda(tmdb: int, kind: str, season: int = 0, episode: int = 0) -> str | None:
    if not OS_API_KEY:
        return None
    params = {"languages": "pt-br", "tmdb_id": int(tmdb)}
    if kind == "tv":
        params["season_number"] = int(season)
        params["episode_number"] = int(episode)
    try:
        resultados = _opensubtitles("GET", "/subtitles", params=params).get("data") or []
    except (ResolveError, requests.RequestException, ValueError, AttributeError):
        return None
    candidatos = []
    for resultado in resultados:
        atributos = resultado.get("attributes") if isinstance(resultado, dict) else None
        arquivos = atributos.get("files") if isinstance(atributos, dict) else None
        if not arquivos or not isinstance(arquivos, list):
            continue
        idioma = str(atributos.get("language") or "").lower()
        if idioma.startswith("pt"):
            candidatos.append((int(atributos.get("download_count") or 0), arquivos[0]))
    if not candidatos:
        return None
    candidatos.sort(key=lambda item: item[0], reverse=True)
    arquivo = candidatos[0][1]
    try:
        resposta = _opensubtitles("POST", "/download",
                                  body={"file_id": arquivo["file_id"], "sub_format": "srt"})
        url = str(resposta.get("link") or "")
        if not url.startswith(("https://", "http://")):
            return None
        data = _get(url).content
        if zipfile.is_zipfile(BytesIO(data)):
            with zipfile.ZipFile(BytesIO(data)) as archive:
                name = next((n for n in archive.namelist()
                             if n.lower().endswith((".srt", ".vtt", ".sub"))), None)
                if not name:
                    return None
                data = archive.read(name)
        return data.decode("utf-8-sig", "replace")
    except (ResolveError, requests.RequestException, ValueError, KeyError,
            OSError, zipfile.BadZipFile):
        return None


def _opensubtitles(method: str, path: str, params: dict | None = None,
                   body: dict | None = None) -> dict:
    if not OS_API_KEY:
        return {}
    headers = {"Api-Key": OS_API_KEY, "Accept": "application/json", "User-Agent": OS_UA}
    last_error = "OpenSubtitles indisponível"
    for tentativa in range(2):
        try:
            response = requests.request(method, f"{OS_API_URL}{path}",
                                         params=sorted((params or {}).items()),
                                         json=body, headers=headers, timeout=TIMEOUT)
            if response.status_code == 429 and tentativa == 0:
                time.sleep(1)
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = type(exc).__name__
            if tentativa == 0:
                time.sleep(0.5)
        except ValueError as exc:
            raise ResolveError("opensubtitles_json") from exc
    raise ResolveError("opensubtitles", last_error)


# ------------------------------------------------------------------ cli

def _cli() -> int:
    ap = argparse.ArgumentParser(description="teste de resolvedores EN")
    ap.add_argument("acao", choices=["teste", "baixar"])
    ap.add_argument("--provider", default="ytdlp")
    ap.add_argument("--url", default="")
    ap.add_argument("--tmdb", type=int, default=0)
    ap.add_argument("--kind", default="movie")
    ap.add_argument("--season", type=int, default=1)
    ap.add_argument("--episode", type=int, default=1)
    ap.add_argument("--out", default="./teste")
    args = ap.parse_args()

    class Controle:
        def checkpoint(self): pass

    if args.provider == "ytdlp":
        src = {"provider": "ytdlp", "url": args.url}
    else:
        src = {"provider": args.provider, "kind": args.kind, "tmdb": args.tmdb,
               "season": args.season, "episode": args.episode}
    try:
        cands = resolver_fontes([src], Controle())
    except Exception as exc:
        print(f"falhou: {exc}", file=sys.stderr)
        return 1
    for c in cands:
        print(json.dumps({k: c[k] for k in ("provider", "quality", "url")}, ensure_ascii=False))
    if args.acao == "baixar" and cands:
        def prog(d, t, *_):
            print(f"\r{d * 100 / t:5.1f}%", end="", flush=True)
        print()
        print(baixar_ytdlp(cands[0], args.out, prog, Controle()) or "falhou")
    return 0 if cands else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
