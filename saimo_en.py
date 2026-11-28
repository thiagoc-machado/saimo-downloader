#!/usr/bin/env python3
"""Resolvedores EN para o saimo: yt-dlp, vidsrc, vidlink e captura HLS via Playwright.

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
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote, urljoin, urlparse

import requests
import saimo_core as core

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TIMEOUT = (5, 25)
MAX_H = int(os.getenv("SAIMO_EN_MAX_H", "1080"))
YTDLP = os.getenv("SAIMO_YTDLP", "yt-dlp")
MAX_PROVIDERS = max(1, min(3, int(os.getenv("SAIMO_EN_PROVIDERS", "3"))))
MAX_CANDIDATOS = 8
BLOCO = int(os.getenv("SAIMO_EN_BLOCO", str(8 << 20)))
OS_API_URL = "https://api.opensubtitles.com/api/v1"
OS_API_KEY = (os.getenv("SAIMO_OPENSUBTITLES_API_KEY") or
              os.getenv("OPENSUBTITLES_API_KEY", "")).strip()
OS_UA = "Saimo Downloader v1.0"

INTERVALO_CAPTURA = float(os.getenv("SAIMO_EN_INTERVALO", "8"))
TENTATIVAS_429 = max(1, min(5, int(os.getenv("SAIMO_EN_429_RETRIES", "3"))))
TENTATIVAS_DOWNLOAD = max(1, min(4, int(os.getenv("SAIMO_EN_DL_RETRIES", "4"))))
ESTADO_PW = Path(os.getenv("SAIMO_EN_STATE", "./data/pw_state.json")).expanduser()
PROXY_PW = os.getenv("SAIMO_EN_PROXY", "").strip()
RE_HOTLINK = (403, 427, 428, 429)
RE_MIDIA = re.compile(r'https?://[^\s"\'<>\\]+?\.(?:m3u8|mp4)[^\s"\'<>\\]*')
RE_ROTA_MIDIA = re.compile(r"\.(?:mp4|m3u8)(?:\?|$)")
RE_SHARD = re.compile(r"^(bcdn)(\d+)\.(.+)$")
RE_FAIXA = re.compile(r"/(\d+)$")
IGNORAR_HEADERS = {"host", "content-length", "range", "connection", "accept-encoding",
                   "if-none-match", "if-modified-since", "if-range", "te"}


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


def _midias_html(texto: str) -> list[str]:
    achados = [a.replace("\\/", "/") for a in RE_MIDIA.findall(texto or "")]
    return list(dict.fromkeys(achados))


def _varrer_midias(obj, achados: list | None = None) -> list[str]:
    achados = achados if achados is not None else []
    if isinstance(obj, dict):
        for valor in obj.values():
            _varrer_midias(valor, achados)
    elif isinstance(obj, list):
        for valor in obj:
            _varrer_midias(valor, achados)
    elif isinstance(obj, str):
        achados.extend(_midias_html(obj))
    return list(dict.fromkeys(achados))


def _fluxo(dados: dict) -> dict:
    atual = dados
    for chave in ("stream", "data", "result"):
        if isinstance(atual.get(chave), dict):
            atual = atual[chave]
            break
    return atual


def _variantes_cdn(url: str) -> list[str]:
    partes = urlparse(url)
    encontrado = RE_SHARD.match(partes.hostname or "")
    if not encontrado:
        return [url]
    saida = [url]
    for numero in range(1, 5):
        if str(numero) == encontrado.group(2):
            continue
        novo = f"{encontrado.group(1)}{numero}.{encontrado.group(3)}"
        saida.append(partes._replace(netloc=novo).geturl())
    return saida


def _expandir(cand: dict) -> list[dict]:
    saida = [cand]
    for variante in _variantes_cdn(cand["url"])[1:]:
        saida.append(_norm(variante, dict(cand["headers"]), cand.get("quality"),
                           cand.get("provider", "")))
    return saida


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


# ------------------------------------------------------------------ hosts

EMBEDS: dict[str, Callable[[str, int, int, int], str]] = {
    "vidsrc": lambda k, t, s, e: (f"https://vidsrc.sh/embed/tv?tmdb={t}&season={s}&episode={e}"
                                  if k == "tv" else f"https://vidsrc.sh/embed/movie?tmdb={t}"),
    "vidlink": lambda k, t, s, e: (f"https://vidlink.pro/tv/{t}/{s}/{e}"
                                   if k == "tv" else f"https://vidlink.pro/movie/{t}"),
    "embed_su": lambda k, t, s, e: (f"https://embed.su/embed/tv/{t}/{s}/{e}"
                                    if k == "tv" else f"https://embed.su/embed/movie/{t}"),
    "moviesapi": lambda k, t, s, e: (f"https://moviesapi.club/tv/{t}-{s}-{e}"
                                     if k == "tv" else f"https://moviesapi.club/movie/{t}"),
}
ORIGENS = {"vidsrc": "https://vidsrc.sh", "vidlink": "https://vidlink.pro",
           "embed_su": "https://embed.su", "moviesapi": "https://moviesapi.club"}

_tls = threading.local()
_lock_captura = threading.Lock()
_ultimo_captura = 0.0
_cache_info: dict[tuple, tuple[float, bool | None]] = {}


def _vaga_captura() -> None:
    global _ultimo_captura
    with _lock_captura:
        espera = INTERVALO_CAPTURA - (time.monotonic() - _ultimo_captura)
        if espera > 0:
            time.sleep(espera)
        _ultimo_captura = time.monotonic()


def _reset_browser() -> None:
    navegador = getattr(_tls, "browser", None)
    try:
        if navegador is not None:
            navegador.close()
    except Exception:
        pass
    _tls.browser = None


def _obter_browser():
    navegador = getattr(_tls, "browser", None)
    if navegador is not None:
        try:
            if navegador.is_connected():
                return navegador
        except Exception:
            _reset_browser()
    from playwright.sync_api import sync_playwright
    playwright = getattr(_tls, "playwright", None) or sync_playwright().start()
    _tls.playwright = playwright
    opcoes = {"headless": True, "args": ["--no-sandbox", "--mute-audio",
                                         "--disable-dev-shm-usage", "--disable-gpu",
                                         "--no-first-run"]}
    if PROXY_PW:
        opcoes["proxy"] = {"server": PROXY_PW}
    _tls.browser = playwright.chromium.launch(**opcoes)
    return _tls.browser


def _abrir_contexto(navegador):
    estado = str(ESTADO_PW) if ESTADO_PW.is_file() else None
    return navegador.new_context(user_agent=UA, viewport={"width": 1280, "height": 720},
                                 locale="en-US", storage_state=estado)


def _abrir(pagina, url: str):
    for tentativa in range(1, TENTATIVAS_429 + 1):
        try:
            resposta = pagina.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            texto = str(exc)
            if any(str(codigo) in texto for codigo in RE_HOTLINK):
                if tentativa >= TENTATIVAS_429:
                    raise ResolveError("capture_http_429")
                time.sleep(6 * tentativa)
                continue
            if "imeout" in texto:
                raise ResolveError("capture_timeout")
            raise ResolveError("capture_navegacao", texto[:80])
        if resposta is not None and resposta.status in RE_HOTLINK:
            if tentativa >= TENTATIVAS_429:
                raise ResolveError("capture_http_429")
            espera = 6 * tentativa
            try:
                cabecalho = resposta.headers.get("retry-after")
                if cabecalho:
                    espera = max(espera, min(90, int(cabecalho)))
            except (ValueError, AttributeError):
                pass
            time.sleep(espera)
            continue
        return resposta
    raise ResolveError("capture_http_429")


def _info_ok(kind: str, tmdb: int, temporada: int = 0, episodio: int = 0) -> bool | None:
    chave = (kind, int(tmdb), int(temporada), int(episodio))
    agora = time.monotonic()
    guardado = _cache_info.get(chave)
    if guardado and agora - guardado[0] < 3600:
        return guardado[1]
    rota = (f"https://vidsrc.sh/info/tv/{tmdb}/{temporada}/{episodio}.json"
            if kind == "tv" else f"https://vidsrc.sh/info/movie/{tmdb}.json")
    resultado: bool | None = None
    try:
        r = requests.get(rota, headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code == 200:
            resultado = True
        elif r.status_code == 404:
            resultado = False
    except requests.RequestException:
        resultado = None
    _cache_info[chave] = (agora, resultado)
    return resultado


SELETORES = ("[class*=welcome] button", "[class*=consent] button", "[data-center-toggle]",
             ".pl-center-btn", "[data-play]", "button[class*=play]", "button[class*=Play]",
             "[class*=play-btn]", ".vjs-big-play-button")


def _clicar_play(pagina) -> bool:
    for quadro in pagina.frames:
        for seletor in SELETORES:
            try:
                quadro.click(seletor, timeout=350)
                return True
            except Exception:
                continue
        try:
            quadro.evaluate("document.querySelector('video')?.play()")
        except Exception:
            pass
    try:
        tamanho = pagina.viewport_size or {"width": 1280, "height": 720}
        pagina.mouse.click(tamanho["width"] // 2, tamanho["height"] // 2)
    except Exception:
        pass
    return False


def _colher_video(pagina, capturados: list) -> None:
    for quadro in pagina.frames:
        try:
            achados = quadro.evaluate(
                "Array.from(document.querySelectorAll('video,source'))"
                ".map(e => e.currentSrc || e.src || '')")
        except Exception:
            continue
        for url in achados or []:
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            if ".m3u8" not in url.lower() and ".mp4" not in url.lower():
                continue
            if any(url == par[0] for par in capturados):
                continue
            capturados.append((url, {"Referer": pagina.url or "", "Accept": "*/*",
                                     "User-Agent": UA, "Sec-Fetch-Dest": "video",
                                     "Sec-Fetch-Mode": "no-cors",
                                     "Sec-Fetch-Site": "cross-site"}))


def _desafio(pagina) -> bool:
    try:
        titulo = (pagina.title() or "").lower()
    except Exception:
        return False
    if "just a moment" in titulo or "atenc" in titulo:
        return True
    try:
        corpo = pagina.evaluate(
            "document.body ? document.body.innerText.slice(0,300).toLowerCase() : ''") or ""
    except Exception:
        return False
    return "429" in corpo or "too many requests" in corpo or "verifying" in corpo


def _cookies_para(ctx, url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return ""
    jar = []
    try:
        for cookie in ctx.cookies():
            dominio = str(cookie.get("domain") or "").lstrip(".").lower()
            if dominio and (host == dominio or host.endswith("." + dominio)):
                jar.append(f"{cookie['name']}={cookie['value']}")
    except Exception:
        return ""
    return "; ".join(jar)


def _validar(cand: dict) -> bool:
    """Testa a URL com 1 byte. Nunca levanta: True só em 200/206."""
    headers = dict(cand.get("headers") or {})
    headers["Range"] = "bytes=0-0"
    try:
        resposta = requests.get(cand["url"], headers=headers, stream=True,
                                timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return False
    try:
        if resposta.status_code not in (200, 206):
            return False
        next(resposta.iter_content(1), None)
        return True
    except (requests.RequestException, StopIteration):
        return False
    finally:
        resposta.close()


def _cabecalho_midia(referer: str) -> dict:
    return {"Referer": referer, "Origin": "https://vidlink.pro", "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9", "User-Agent": UA,
            "Sec-Fetch-Dest": "video", "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site"}


# ------------------------------------------------------------------ vidlink

def _vidlink_urls(kind: str, tmdb: int, temporada: int, episodio: int) -> list[str]:
    if kind == "tv":
        base = f"https://vidlink.pro/api/b/tv/{tmdb}/{temporada}/{episodio}"
    else:
        base = f"https://vidlink.pro/api/b/movie/{tmdb}"
    return [base, base + "?multiLang=0", base + "?type=" + kind]


def _legendas_de(fluxo: dict, base: str) -> list[dict]:
    saida = []
    for legenda in fluxo.get("captions") or []:
        if not isinstance(legenda, dict):
            continue
        uri = legenda.get("file") or legenda.get("url")
        if uri:
            saida.append({"lang": str(legenda.get("language") or "?"),
                          "url": urljoin(base, str(uri))})
    return saida


def _vidlink_api(src: dict, control, offset: int = 0) -> list[dict]:
    kind, tmdb = src["kind"], int(src["tmdb"])
    temporada = int(src.get("season") or 1)
    episodio = int(src.get("episode") or 1)
    cab = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
           "Referer": "https://vidlink.pro/", "Origin": "https://vidlink.pro"}
    dados = None
    ultimo = "vidlink_api"
    for url in _vidlink_urls(kind, tmdb, temporada, episodio):
        control.checkpoint()
        try:
            r = requests.get(url, headers=cab, timeout=TIMEOUT)
        except requests.RequestException as exc:
            ultimo = type(exc).__name__
            continue
        if r.status_code != 200:
            ultimo = f"http_{r.status_code}"
            continue
        try:
            corpo = r.json()
        except ValueError:
            corpo = None
        if isinstance(corpo, dict) and corpo:
            dados = corpo
            break
        ultimo = "vazio"
        time.sleep(0.6)
    if not dados:
        raise ResolveError("vidlink_api", str(ultimo))

    fluxo = _fluxo(dados)
    playlist = fluxo.get("playlist") or fluxo.get("source") or fluxo.get("url")
    if not playlist:
        raise ResolveError("vidlink_sem_fonte", str(list(fluxo)[:6]))
    referer = f"https://vidlink.pro/{'tv' if kind == 'tv' else 'movie'}/{tmdb}"
    if kind == "tv":
        referer += f"/{temporada}/{episodio}"
    cand = _norm(str(playlist), _cabecalho_midia(referer), 1080, "vidlink")
    cand["subs"] = _legendas_de(fluxo, str(playlist))
    return _expandir(cand)


def _vidlink_pela_pagina(src: dict, control) -> list[dict]:
    kind, tmdb = src["kind"], int(src["tmdb"])
    temporada = int(src.get("season") or 1)
    episodio = int(src.get("episode") or 1)
    if kind == "tv":
        pagina_url = f"https://vidlink.pro/tv/{tmdb}/{temporada}/{episodio}"
        api = f"/api/b/tv/{tmdb}/{temporada}/{episodio}"
    else:
        pagina_url = f"https://vidlink.pro/movie/{tmdb}"
        api = f"/api/b/movie/{tmdb}"

    _vaga_captura()
    ctx = None
    for _ in range(2):
        try:
            ctx = _abrir_contexto(_obter_browser())
            break
        except Exception:
            _reset_browser()
    if ctx is None:
        raise ResolveError("capture_browser")
    try:
        pagina = ctx.new_page()
        pagina.route(re.compile(r"(googletagmanager|histats|doubleclick|analytics)"),
                     lambda rota: rota.abort())
        pagina.route(RE_ROTA_MIDIA, lambda rota: rota.abort())
        _abrir(pagina, pagina_url)
        pagina.wait_for_timeout(2500)

        dados = None
        try:
            dados = pagina.evaluate(
                "async (u) => { try { const r = await fetch(u, {"
                "headers: {accept: 'application/json'}, credentials: 'include'});"
                " if (!r.ok) return null; const t = await r.text();"
                " return t ? JSON.parse(t) : null; } catch (e) { return null; } }", api)
        except Exception:
            dados = None

        if not isinstance(dados, dict) or not dados:
            try:
                html = pagina.content() or ""
            except Exception:
                html = ""
            urls = _midias_html(html)
            if not urls:
                raise ResolveError("vidlink_pagina_vazia")
            return _expandir(_norm(urls[0], _cabecalho_midia(pagina_url), 1080, "vidlink"))

        fluxo = _fluxo(dados)
        playlist = fluxo.get("playlist") or fluxo.get("source") or fluxo.get("url")
        if not playlist:
            raise ResolveError("vidlink_sem_fonte", str(list(fluxo)[:6]))
        cab = _cabecalho_midia(pagina_url)
        cookie = _cookies_para(ctx, str(playlist))
        if cookie:
            cab["Cookie"] = cookie
        cand = _norm(str(playlist), cab, 1080, "vidlink")
        cand["subs"] = _legendas_de(fluxo, str(playlist))
        return _expandir(cand)
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def _vidlink(src: dict, control, offset: int = 0) -> list[dict]:
    try:
        return _vidlink_api(src, control, offset)
    except ResolveError:
        return _vidlink_pela_pagina(src, control)


# ------------------------------------------------------------------ captura

def _captura_host(embed: str, origem: str, control) -> list[dict]:
    capturados: list[tuple[str, dict]] = []

    def guardar(url: str, headers: dict) -> None:
        if any(url == par[0] for par in capturados):
            return
        capturados.append((url, headers))

    def ao_pedir(requisicao) -> None:
        url = requisicao.url
        baixa = url.lower()
        if not (".m3u8" in baixa or ".mp4" in baixa):
            return
        headers = {c.title(): v for c, v in requisicao.headers.items()
                   if c.lower() not in IGNORAR_HEADERS}
        headers.setdefault("Referer", origem + "/")
        headers.setdefault("Origin", origem)
        headers.setdefault("Accept", "*/*")
        headers.setdefault("Sec-Fetch-Dest", "video")
        headers.setdefault("Sec-Fetch-Mode", "no-cors")
        headers.setdefault("Sec-Fetch-Site", "cross-site")
        guardar(url, headers)

    _vaga_captura()
    ctx = None
    ultima: Exception | None = None
    for _ in range(2):
        try:
            ctx = _abrir_contexto(_obter_browser())
            break
        except Exception as exc:
            ultima = exc
            _reset_browser()
    if ctx is None:
        raise ResolveError("capture_browser", type(ultima).__name__ if ultima else "")

    try:
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        pagina = ctx.new_page()
        pagina.route(re.compile(r"(googletagmanager|histats|doubleclick|analytics)"),
                     lambda rota: rota.abort())
        pagina.route(RE_ROTA_MIDIA, lambda rota: (ao_pedir(rota.request), rota.abort()))
        pagina.on("request", ao_pedir)

        try:
            _abrir(pagina, origem + "/")
            pagina.wait_for_timeout(900)
        except ResolveError:
            pass

        _abrir(pagina, embed)
        pagina.wait_for_timeout(1200)

        for _ in range(10):
            if capturados:
                break
            if not _desafio(pagina):
                break
            pagina.wait_for_timeout(1200)

        cab_pagina = {"Referer": embed, "Accept": "*/*", "User-Agent": UA,
                      "Sec-Fetch-Dest": "video", "Sec-Fetch-Mode": "no-cors",
                      "Sec-Fetch-Site": "cross-site"}
        for url in _midias_html(pagina.content() or ""):
            guardar(url, dict(cab_pagina))

        for _ in range(36):
            control.checkpoint()
            if capturados:
                break
            _clicar_play(pagina)
            _colher_video(pagina, capturados)
            pagina.wait_for_timeout(700)

        _colher_video(pagina, capturados)
        if not capturados:
            raise ResolveError("capture_sem_m3u8")

        ordenados = sorted(capturados, key=lambda par: (".m3u8" not in par[0].lower(),
                                                        ".mp4" not in par[0].lower()))
        escolhido: dict | None = None
        for master, headers in ordenados[:4]:
            cookie = _cookies_para(ctx, master)
            if cookie:
                headers["Cookie"] = cookie
            cand = _norm(master, headers, None, "capture")
            if _validar(cand):
                escolhido = cand
                break
            if escolhido is None:
                escolhido = cand
        if escolhido is None:
            raise ResolveError("capture_invalido")
        try:
            manifest = _get(escolhido["url"], headers=escolhido["headers"]).text
            for linha in manifest.splitlines():
                if "TYPE=SUBTITLES" in linha:
                    nome = re.search(r'NAME="([^"]*)"', linha)
                    uri = re.search(r'URI="([^"]+)"', linha)
                    if uri:
                        escolhido["subs"].append(
                            {"lang": nome.group(1) if nome else "?",
                             "url": urljoin(escolhido["url"], uri.group(1))})
        except (ResolveError, requests.RequestException):
            pass
        return _expandir(escolhido)
    except ResolveError:
        raise
    except Exception as exc:
        _reset_browser()
        raise ResolveError("capture_navegacao", type(exc).__name__)
    finally:
        try:
            ESTADO_PW.parent.mkdir(parents=True, exist_ok=True)
            ctx.storage_state(path=str(ESTADO_PW))
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass


# ------------------------------------------------------------------ resolvedores

def _ytdlp(src: dict, control, offset: int = 0) -> list[dict]:
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


def _vidsrc_info(src: dict, control) -> list[dict]:
    kind, tmdb = src["kind"], int(src["tmdb"])
    temporada = int(src.get("season") or 0)
    episodio = int(src.get("episode") or 0)
    rota = (f"https://vidsrc.sh/info/tv/{tmdb}/{temporada}/{episodio}.json"
            if kind == "tv" else f"https://vidsrc.sh/info/movie/{tmdb}.json")
    resposta = _get(rota, "https://vidsrc.sh/")
    if resposta.status_code != 200:
        raise ResolveError("vidsrc_info", str(resposta.status_code))
    try:
        dados = resposta.json()
    except ValueError as exc:
        raise ResolveError("vidsrc_info_json") from exc
    urls = _varrer_midias(dados)
    if not urls:
        raise ResolveError("vidsrc_info_vazio")
    cab = {"Referer": "https://vidsrc.sh/", "Origin": "https://vidsrc.sh",
           "Accept": "*/*", "User-Agent": UA}
    return [_norm(u, dict(cab), None, "vidsrc") for u in urls[:3]]


def _vidsrc(src: dict, control, offset: int = 0) -> list[dict]:
    kind, tmdb = src["kind"], int(src["tmdb"])
    temporada = int(src.get("season") or 0)
    episodio = int(src.get("episode") or 0)
    embed = EMBEDS["vidsrc"](kind, tmdb, temporada, episodio)
    try:
        pagina = _get(embed)
    except ResolveError:
        pagina = None
    if pagina is not None and pagina.status_code == 200:
        achado = re.search(r'data-id="([^"]+)"', pagina.text)
        if achado:
            control.checkpoint()
            rota = "movie" if kind == "movie" else "episode"
            try:
                lista = _get(f"https://vidsrc.sh/ajax/embed/{rota}/{achado.group(1)}/sources").json()
            except (ResolveError, ValueError):
                lista = {}
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
            if saida:
                return saida
        for url in _midias_html(pagina.text):
            cand = _norm(url, {"Referer": embed, "Origin": "https://vidsrc.sh",
                               "Accept": "*/*", "User-Agent": UA}, None, "vidsrc")
            if _validar(cand):
                return [cand]
    try:
        return _vidsrc_info(src, control)
    except ResolveError:
        return _capture(src, control, offset)


def _capture(src: dict, control, offset: int = 0) -> list[dict]:
    kind, tmdb = src["kind"], int(src["tmdb"])
    temporada = int(src.get("season") or 0)
    episodio = int(src.get("episode") or 0)
    erros: list[str] = []

    for nome, funcao, com_offset in (("vidlink_api", _vidlink_api, True),
                                     ("vidlink_pagina", _vidlink_pela_pagina, False)):
        try:
            return funcao(src, control, offset) if com_offset else funcao(src, control)
        except ResolveError as exc:
            erros.append(f"{nome}:{exc.code}")
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            erros.append(f"{nome}:{type(exc).__name__}")

    hosts = [h for h in EMBEDS if h != "vidlink"]
    if offset:
        hosts = hosts[offset % len(hosts):] + hosts[:offset % len(hosts)]
    for host in hosts:
        control.checkpoint()
        if host == "vidsrc" and _info_ok(kind, tmdb, temporada, episodio) is False:
            erros.append(f"{host}:sem_disponibilidade")
            continue
        embed = EMBEDS[host](kind, tmdb, temporada, episodio)
        try:
            return _captura_host(embed, ORIGENS[host], control)
        except ResolveError as exc:
            erros.append(f"{host}:{exc.code}")
        except (RuntimeError, OSError, KeyError) as exc:
            erros.append(f"{host}:{type(exc).__name__}")
    raise ResolveError("capture_sem_m3u8", ",".join(erros[-4:]))


RESOLVEDORES: dict[str, Callable[..., list[dict]]] = {
    "ytdlp": _ytdlp,
    "vidsrc": _vidsrc,
    "capture": _capture,
    "vidlink": _vidlink,
}


def resolver_fontes(sources: Iterable[dict], control, provider_offset: int = 0) -> list[dict]:
    saida: list[dict] = []
    descritores = [src for src in sources or [] if isinstance(src, dict)]
    if not descritores:
        raise ResolveError("sem_fontes", "lista de fontes vazia ou inválida")
    if provider_offset:
        inicio = provider_offset % len(descritores)
        descritores = descritores[inicio:] + descritores[:inicio]
    tentadas = 0
    ja_capturou = False
    erros: list[str] = []
    for src in descritores:
        if tentadas >= MAX_PROVIDERS:
            break
        provider = src.get("provider", "")
        if provider in ("vidsrc", "capture"):
            if ja_capturou:
                continue
            ja_capturou = True
        control.checkpoint()
        tentadas += 1
        fn = RESOLVEDORES.get(provider)
        try:
            if fn:
                saida.extend(fn(src, control, provider_offset))
            elif ".m3u8" in str(src.get("url", "")):
                saida.append(_norm(src["url"], src.get("headers")))
            else:
                raise ResolveError("provider_desconhecido", str(provider or "sem provider"))
        except (ResolveError, requests.RequestException, subprocess.SubprocessError,
                OSError, ValueError, KeyError, TypeError) as exc:
            erros.append(f"{provider or 'fonte'}: {str(exc)[:90]}")
            continue
        except (core.DownloadPaused, core.DownloadCancelled):
            raise
        except Exception as exc:
            erros.append(f"{provider or 'fonte'}: {type(exc).__name__}")
            continue
        if len(saida) >= MAX_CANDIDATOS:
            break
    if not saida and erros:
        raise ResolveError("sem_fontes", "; ".join(erros[-2:]))
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


def _headers_download(cand: dict) -> dict:
    headers = cand.get("headers") if isinstance(cand.get("headers"), dict) else {}
    cab = {k: v for k, v in headers.items() if v}
    cab.setdefault("Accept", "*/*")
    cab.setdefault("User-Agent", UA)
    if "Referer" not in cab:
        partes = urlparse(str(cand.get("url", "")))
        cab["Referer"] = f"{partes.scheme}://{partes.netloc}/"
    cab.setdefault("Origin", cab["Referer"].rstrip("/"))
    cab.pop("Accept-Encoding", None)
    return cab


def _espera_429(cabecalho: str | None, tentativa: int) -> float:
    espera = 12.0 * tentativa
    try:
        if cabecalho:
            espera = max(espera, min(120.0, float(cabecalho)))
    except (TypeError, ValueError):
        pass
    return espera


def _baixar_navegador(cand: dict, temporary: Path, progress, control) -> bool:
    """Baixa em blocos com Range pelo contexto do Chromium (mesma impressão TLS)."""
    try:
        from playwright.sync_api import Error as PwError
    except ImportError:
        return False
    ctx = None
    for _ in range(2):
        try:
            ctx = _abrir_contexto(_obter_browser())
            break
        except Exception:
            _reset_browser()
    if ctx is None:
        return False
    cab = _headers_download(cand)
    url = str(cand["url"])
    inicio = 0
    total = 0
    falhas = 0
    try:
        with temporary.open("wb") as arquivo:
            while True:
                control.checkpoint()
                cab["Range"] = f"bytes={inicio}-{inicio + BLOCO - 1}"
                try:
                    resposta = ctx.request.get(url, headers=cab, timeout=90000,
                                               fail_on_status_code=False)
                except PwError:
                    falhas += 1
                    if falhas >= 3:
                        return inicio > 0 and temporary.stat().st_size > 0
                    time.sleep(6 * falhas)
                    continue
                if resposta.status == 429 or resposta.status == 403:
                    falhas += 1
                    if falhas >= 4:
                        return inicio > 0 and temporary.stat().st_size > 0
                    time.sleep(_espera_429(resposta.headers.get("retry-after"), falhas))
                    continue
                if resposta.status not in (200, 206):
                    return inicio > 0 and temporary.stat().st_size > 0
                pedaco = resposta.body()
                if not pedaco:
                    break
                arquivo.write(pedaco)
                inicio += len(pedaco)
                achado = RE_FAIXA.search(resposta.headers.get("content-range") or "")
                if achado:
                    total = int(achado.group(1))
                progress(inicio, total or inicio, inicio, total or inicio)
                falhas = 0
                if len(pedaco) < BLOCO:
                    break
    except Exception:
        return inicio > 0 and temporary.stat().st_size > 0
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    return temporary.is_file() and temporary.stat().st_size > 0


def _baixar_ffmpeg(cand: dict, final: Path, temporary: Path, progress, control) -> bool:
    cab = _headers_download(cand)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-rw_timeout", "25000000", "-reconnect", "1", "-reconnect_streamed", "1",
           "-reconnect_delay_max", "8", "-headers", _format_headers(cab),
           "-i", str(cand["url"])]
    if cand.get("audio_url"):
        cmd += ["-rw_timeout", "25000000",
                "-headers", _format_headers(cand.get("audio_headers") or cab),
                "-i", str(cand["audio_url"]), "-map", "0:v:0", "-map", "1:a:0"]
    cmd += ["-c", "copy", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", str(temporary)]
    duration = float(cand.get("duration") or 0)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, bufsize=1)
    try:
        for linha in proc.stdout or []:
            linha = linha.strip()
            if linha.startswith("out_time_ms=") and duration > 0:
                try:
                    feito = int(linha.split("=", 1)[1])
                    total = int(duration * 1_000_000)
                    progress(min(feito, total), total, min(feito, total), total)
                except ValueError:
                    pass
            try:
                control.checkpoint()
            except Exception:
                proc.terminate()
                proc.wait()
                raise
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return proc.returncode == 0 and temporary.is_file() and temporary.stat().st_size > 0


def _baixar_requests(cand: dict, temporary: Path, progress, control) -> bool:
    cab = _headers_download(cand)
    cab["Range"] = "bytes=0-"
    for tentativa in range(1, 4):
        try:
            with requests.get(cand["url"], headers=cab, stream=True, timeout=TIMEOUT,
                              allow_redirects=True) as resposta:
                if resposta.status_code in (403, 429):
                    if tentativa < 3:
                        time.sleep(_espera_429(resposta.headers.get("retry-after"), tentativa))
                        continue
                    return False
                if resposta.status_code not in (200, 206):
                    return False
                total = int(resposta.headers.get("content-length") or 0)
                feito = 0
                with temporary.open("wb") as arquivo:
                    for pedaco in resposta.iter_content(1 << 18):
                        control.checkpoint()
                        if not pedaco:
                            continue
                        arquivo.write(pedaco)
                        feito += len(pedaco)
                        progress(feito, total or feito, feito, total or feito)
                return temporary.is_file() and temporary.stat().st_size > 0
        except (requests.RequestException, OSError):
            if tentativa >= 3:
                return False
            time.sleep(4 * tentativa)
    return False


def _baixar_ytdlp(cand: dict, alvo: Path, progress, control) -> str | None:
    cab = _headers_download(cand)
    cmd = [YTDLP, "-c", "--newline", "--no-warnings", "--no-playlist",
           "--socket-timeout", "20", "--retries", "3", "--fragment-retries", "3",
           "--progress-template", "download:D %(progress.downloaded_bytes)s "
           "%(progress.total_bytes)s %(progress.total_bytes_estimate)s",
           "-f", f"b[height<={cand.get('quality') or MAX_H}]/b",
           "-o", str(alvo) + ".%(ext)s"]
    if cab.get("Referer"):
        cmd += ["--referer", cab["Referer"]]
    if cab.get("Origin"):
        cmd += ["--add-header", f"Origin: {cab['Origin']}"]
    if cab.get("Cookie"):
        cmd += ["--add-header", f"Cookie: {cab['Cookie']}"]
    cmd.append(cand["url"])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    try:
        for linha in proc.stdout or []:
            try:
                control.checkpoint()
            except Exception:
                proc.send_signal(19)  # SIGSTOP
                proc.wait()
                raise
            medido = _progresso(linha.strip())
            if medido and medido[1]:
                progress(medido[0], medido[1], medido[0], medido[1])
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
    if proc.returncode != 0:
        return None
    for extensao in (".mp4", ".mkv", ".webm", ".m4a"):
        achado = Path(str(alvo) + extensao)
        if achado.is_file() and achado.stat().st_size:
            return str(achado)
    return None


def baixar_ytdlp(cand: dict, output_base: str, progress, control) -> str | None:
    """Baixa o candidato EN: navegador, ffmpeg, requests e yt-dlp em cascata."""
    alvo = Path(output_base)
    alvo.parent.mkdir(parents=True, exist_ok=True)
    progressivo = bool(re.search(r"\.(mp4|m4v|webm|mkv|m4a)(\?|$)",
                                 str(cand.get("url", "")).lower()))
    if cand.get("audio_url") or progressivo:
        final = Path(str(alvo) + ".mp4")
        temporary = Path(str(final) + ".part")
        for tentativa in range(1, TENTATIVAS_DOWNLOAD + 1):
            temporary.unlink(missing_ok=True)
            if _baixar_navegador(cand, temporary, progress, control):
                os.replace(temporary, final)
                return str(final)
            temporary.unlink(missing_ok=True)
            if _baixar_ffmpeg(cand, final, temporary, progress, control):
                os.replace(temporary, final)
                return str(final)
            temporary.unlink(missing_ok=True)
            if _baixar_requests(cand, temporary, progress, control):
                os.replace(temporary, final)
                return str(final)
            if tentativa < TENTATIVAS_DOWNLOAD:
                time.sleep(10 * tentativa)
        temporary.unlink(missing_ok=True)
        if cand.get("audio_url"):
            return None
    return _baixar_ytdlp(cand, alvo, progress, control)


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


def _imdb_id(tmdb: int, kind: str) -> str | None:
    chave = os.getenv("SAIMO_TMDB_API_KEY", "")
    if not chave:
        return None
    try:
        r = requests.get(f"https://api.themoviedb.org/3/{kind}/{tmdb}/external_ids",
                         params={"api_key": chave}, timeout=TIMEOUT)
        return (r.json() or {}).get("imdb_id")
    except (requests.RequestException, ValueError):
        return None


# ------------------------------------------------------------------ cli

def diagnostico(tmdb: int, kind: str, season: int = 1, episode: int = 1) -> int:
    for url in _vidlink_urls(kind, tmdb, season, episode):
        try:
            r = requests.get(url, headers={"User-Agent": UA,
                                           "Referer": "https://vidlink.pro/"}, timeout=TIMEOUT)
            print(json.dumps({"url": url, "status": r.status_code, "corpo": r.text[:200]}))
        except requests.RequestException as exc:
            print(json.dumps({"url": url, "erro": type(exc).__name__}))
    try:
        r = requests.get(f"https://vidsrc.sh/info/{kind}/{tmdb}.json",
                         headers={"User-Agent": UA}, timeout=TIMEOUT)
        print(json.dumps({"vidsrc_info": r.status_code, "corpo": r.text[:400]}))
    except requests.RequestException as exc:
        print(json.dumps({"vidsrc_info": type(exc).__name__}))
    for host, montar in EMBEDS.items():
        embed = montar(kind, tmdb, season, episode)
        try:
            r = requests.get(embed, headers={"User-Agent": UA}, timeout=TIMEOUT,
                             allow_redirects=True)
        except requests.RequestException as exc:
            print(json.dumps({"host": host, "erro": type(exc).__name__}))
            continue
        print(json.dumps({"host": host, "final": r.url, "status": r.status_code,
                          "data_id": bool(re.search(r'data-id="', r.text)),
                          "midias": _midias_html(r.text)[:3],
                          "bytes": len(r.text)}))
    return 0


def _cli() -> int:
    ap = argparse.ArgumentParser(description="teste de resolvedores EN")
    ap.add_argument("acao", choices=["teste", "baixar", "diagnostico"])
    ap.add_argument("--provider", default="ytdlp")
    ap.add_argument("--url", default="")
    ap.add_argument("--tmdb", type=int, default=0)
    ap.add_argument("--kind", default="movie")
    ap.add_argument("--season", type=int, default=1)
    ap.add_argument("--episode", type=int, default=1)
    ap.add_argument("--out", default="./teste")
    args = ap.parse_args()

    if args.acao == "diagnostico":
        return diagnostico(args.tmdb, args.kind, args.season, args.episode)

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
        for cand in cands:
            saida = baixar_ytdlp(cand, args.out, prog, Controle())
            if saida:
                print(saida)
                return 0
        print("falhou")
        return 1
    return 0 if cands else 1


if __name__ == "__main__":
    raise SystemExit(_cli())