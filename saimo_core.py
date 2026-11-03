#!/usr/bin/env python3
"""Configuração, resolução e download de mídia para o catálogo local."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

CATALOGO = os.getenv("SAIMO_CATALOGO", "https://filmes-saimo.pages.dev").rstrip("/")
PROXY = os.getenv("SAIMO_PROXY", "https://saimo-tv.pages.dev").rstrip("/")
DESTINO = str(Path(os.getenv("SAIMO_DOWNLOAD_DIR", "./downloads")).expanduser().resolve())
QUALIDADE = max(144, int(os.getenv("SAIMO_QUALITY", "1080")))
SEGMENTOS = max(1, min(32, int(os.getenv("SAIMO_SEGMENTOS", "8"))))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
REF_EMBED = "https://embedplayer2.xyz/"
TIMEOUT, TENTATIVAS, CHUNK = 30, 4, 1 << 16
SEM_PROXY = ("/api/curtitv", "curtitv://", "s23-cloudfront-net.lat", "s25-cloudfront-net.lat")
PRECISA_RESOLVE = ("dbonline.tech-cdn.top", "hubby-dmca.com", "desativado.invalid")
HOST_OK = (".77zzhf54vdll71.com", "dark-glade-b156.aa4f20382505.workers.dev")

_thread_sessions = threading.local()
Path(DESTINO).mkdir(parents=True, exist_ok=True)


class DownloadPaused(Exception):
    """Sinaliza uma pausa cooperativa sem remover o progresso parcial."""


class DownloadCancelled(Exception):
    """Sinaliza cancelamento; o worker remove os dados do job."""


class DownloadControl:
    def __init__(self):
        self._lock = threading.Lock()
        self._paused = False
        self._cancelled = False

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            self._paused = False

    def checkpoint(self) -> None:
        with self._lock:
            if self._cancelled:
                raise DownloadCancelled()
            if self._paused:
                raise DownloadPaused()


def session() -> requests.Session:
    """Cada worker/thread usa sua própria sessão HTTP."""
    value = getattr(_thread_sessions, "value", None)
    if value is None:
        value = requests.Session()
        value.headers.update({"User-Agent": UA, "Accept": "*/*"})
        _thread_sessions.value = value
    return value


def get(url: str, referer: str | None = None, binary: bool = False, stream: bool = False,
        cookies=None, extra_headers: dict[str, str] | None = None):
    headers = {"Referer": referer} if referer else {}
    if extra_headers:
        headers.update(extra_headers)
    last_error = "falha de rede"
    for attempt in range(TENTATIVAS):
        try:
            response = session().get(url, headers=headers, timeout=TIMEOUT, stream=stream,
                                     cookies=cookies)
            if response.status_code in (200, 206):
                return response if stream else (response.content if binary else response.text)
            last_error = f"HTTP {response.status_code}"
            response.close()
            if response.status_code in (403, 404, 410, 451):
                break
        except requests.RequestException as exc:
            last_error = type(exc).__name__
        if attempt < TENTATIVAS - 1:
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(last_error)


def prox(url: str, referer: str | None = None) -> str:
    target = f"{PROXY}/api/proxy?url={quote(url, safe='')}"
    return target + (f"&referer={quote(referer, safe='')}" if referer else "")


def usar_proxy(url: str) -> bool:
    low = url.lower()
    if any(marker in low for marker in SEM_PROXY):
        return False
    if low.startswith("http://"):
        return True
    path = urlparse(low).path
    return path.endswith((".txt", ".m3u8")) or "dadinhodetapioca.fun" in low


def referer_de(url: str) -> str | None:
    return REF_EMBED if "embedplayer2.xyz" in url else None


def resolver_stream(url: str) -> str | None:
    separator = "&" if "?" in url else "?"
    try:
        response = session().get(f"{url}{separator}json=1", timeout=TIMEOUT)
        if response.ok:
            result = response.json().get("url")
            return result if isinstance(result, str) and result.startswith(("http://", "https://")) else None
    except (requests.RequestException, ValueError, AttributeError):
        pass
    return None


def resolver_vod(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    if not any(host == candidate or host.endswith("." + candidate) for candidate in PRECISA_RESOLVE):
        return None
    try:
        response = session().get(f"{PROXY}/api/resolve-vod?url={quote(url, safe='')}", timeout=TIMEOUT)
        if not response.ok:
            return None
        candidate = (response.json() or {}).get("url")
        candidate_host = (urlparse(candidate or "").hostname or "").lower()
        if candidate and candidate.startswith("https://") and any(
                candidate_host == host.lstrip(".") or
                candidate_host.endswith(host if host.startswith(".") else "." + host)
                for host in HOST_OK):
            return candidate
    except (requests.RequestException, ValueError, AttributeError):
        pass
    return None


def resolver_fontes(fontes: list[str], control: DownloadControl | None = None) -> list[str]:
    resolved: list[str] = []
    for url in fontes:
        if control:
            control.checkpoint()
        if urlparse(url).path == "/api/stream":
            url = resolver_stream(url) or ""
            if not url:
                continue
        url = resolver_vod(url) or url
        if url not in resolved:
            resolved.append(url)
    if control:
        control.checkpoint()
    return resolved


def limpar(nome: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", nome)
    return re.sub(r"\s+", " ", cleaned).strip(" .")[:140] or "download"


def escolher_variante(master: str, qualidade: int) -> str | None:
    variants = []
    lines = master.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            match = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            height = int(match.group(2)) if match else 0
            for candidate in lines[index + 1:]:
                candidate = candidate.strip()
                if candidate and not candidate.startswith("#"):
                    variants.append((height, candidate))
                    break
    if not variants:
        return None
    variants.sort(key=lambda item: item[0])
    eligible = [item for item in variants if item[0] <= qualidade] or variants
    return eligible[-1][1]


def baixar_mp4(url: str, destino: str, cb=None, referer: str | None = None,
               control: DownloadControl | None = None) -> None:
    target = prox(url, referer) if usar_proxy(url) else url
    temporary = destino + ".part"
    offset = os.path.getsize(temporary) if os.path.exists(temporary) else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    if control:
        control.checkpoint()
    try:
        response = get(target, referer=referer, stream=True, extra_headers=headers)
        if offset and response.status_code != 206:
            response.close()
            offset = 0
            response = get(target, referer=referer, stream=True)
        with response, open(temporary, "ab" if offset else "wb") as output:
            length = int(response.headers.get("Content-Length") or 0)
            total = offset + length if length else 0
            done = offset
            for chunk in response.iter_content(CHUNK):
                if control:
                    control.checkpoint()
                if chunk:
                    output.write(chunk)
                    done += len(chunk)
                    if cb:
                        cb(done, total)
        os.replace(temporary, destino)
    except (DownloadPaused, DownloadCancelled):
        raise
    except Exception:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise


def baixar_hls(master_url: str, destino: str, cb=None, referer: str | None = None,
               control: DownloadControl | None = None) -> None:
    from concurrent.futures import ThreadPoolExecutor

    ref = referer or referer_de(master_url)
    master_target = prox(master_url, ref) if usar_proxy(master_url) else master_url
    master = get(master_target, referer=ref)
    playlist_url = master_url
    if "#EXT-X-STREAM-INF" in master:
        variant = escolher_variante(master, QUALIDADE)
        if not variant:
            raise RuntimeError("master sem variantes")
        playlist_url = urljoin(master_url, variant)
        playlist = get(prox(playlist_url, ref) if usar_proxy(playlist_url) else playlist_url, referer=ref)
    else:
        playlist = master
    segment_cookies = session().cookies.copy()
    encrypted = any(re.search(r"METHOD=(?!NONE(?:,|$))", line)
                    for line in playlist.splitlines() if line.startswith("#EXT-X-KEY:"))
    if encrypted:
        raise RuntimeError("playlist HLS criptografada não é suportada por este downloader")
    if "#EXT-X-MAP" in playlist or "#EXT-X-BYTERANGE" in playlist:
        raise RuntimeError("playlist HLS com mapa ou faixas de bytes não é suportada")
    segments = [urljoin(playlist_url, line.strip()) for line in playlist.splitlines()
                if line.strip() and not line.startswith("#")]
    if not segments:
        raise RuntimeError("playlist vazia")
    folder = destino + ".segs"
    os.makedirs(folder, exist_ok=True)

    completed = 0
    completed_lock = threading.Lock()

    def download_segment(item):
        nonlocal completed
        index, segment_url = item
        if control:
            control.checkpoint()
        path = os.path.join(folder, f"{index:06d}.ts")
        if not (os.path.exists(path) and os.path.getsize(path)):
            segment_target = prox(segment_url, ref) if usar_proxy(segment_url) else segment_url
            temporary = path + ".part"
            with get(segment_target, referer=ref, stream=True, cookies=segment_cookies) as response, open(temporary, "wb") as output:
                for chunk in response.iter_content(CHUNK):
                    if control:
                        control.checkpoint()
                    if chunk:
                        output.write(chunk)
            os.replace(temporary, path)
        if cb:
            with completed_lock:
                completed += 1
                cb(completed, len(segments))
        return path

    try:
        if control:
            control.checkpoint()
        with ThreadPoolExecutor(max_workers=SEGMENTOS) as pool:
            list(pool.map(download_segment, enumerate(segments)))
        concat = os.path.join(folder, "lista.txt")
        with open(concat, "w", encoding="utf-8") as output:
            for index in range(len(segments)):
                path = os.path.join(folder, f"{index:06d}.ts").replace("'", "'\\''")
                output.write(f"file '{path}'\n")
        process = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                                    "-i", concat, "-c", "copy", "-bsf:a", "aac_adtstoasc", destino])
        try:
            while process.poll() is None:
                if control:
                    try:
                        control.checkpoint()
                    except (DownloadPaused, DownloadCancelled):
                        try:
                            process.terminate()
                        except ProcessLookupError:
                            pass
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        raise
                time.sleep(0.2)
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, process.args)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    except FileNotFoundError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        if os.path.exists(destino):
            os.remove(destino)
        raise RuntimeError("ffmpeg não encontrado; instale ffmpeg para juntar vídeos HLS") from exc
    except (DownloadPaused, DownloadCancelled):
        if os.path.exists(destino):
            os.remove(destino)
        raise
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        if os.path.exists(destino):
            os.remove(destino)
        raise
    else:
        shutil.rmtree(folder, ignore_errors=True)


def baixar(fontes: list[str], destino_base: str, cb=None,
           control: DownloadControl | None = None) -> str | None:
    errors = []
    for url in fontes:
        parsed = urlparse(url.lower())
        output = destino_base + ".mp4"
        try:
            if control:
                control.checkpoint()
            if parsed.path.endswith((".m3u8", ".txt")):
                baixar_hls(url, output, cb, referer_de(url), control)
            elif parsed.path.endswith(".mpd"):
                errors.append("fonte DASH (.mpd) não suportada")
                continue
            else:
                baixar_mp4(url, output, cb, referer_de(url), control)
            return output
        except (DownloadPaused, DownloadCancelled):
            raise
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")
            if os.path.exists(output):
                os.remove(output)
    if errors:
        raise RuntimeError("; ".join(errors[-3:]))
    raise RuntimeError("nenhuma fonte de download disponível")
