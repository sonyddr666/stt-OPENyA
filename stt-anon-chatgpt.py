#!/usr/bin/env python3
# stt-anon-chatgpt.py
#
# STT anonimo com fallback para cookies:
# 1. Tenta SEMPRE primeiro com backend-anon/transcribe (sem login)
# 2. Se der erro 401/403/429 repetido, faz fallback automatico para backend-api/transcribe com cookies
# 3. So pede cookies se o modo anonimo falhar e nao houver token.txt
#
# Instalar:
#   pip install sounddevice numpy curl_cffi
#   (para --format webm: instalar ffmpeg no sistema)
#
# Rodar:
#   python stt-anon-chatgpt.py
#   python stt-anon-chatgpt.py --format webm
#   python stt-anon-chatgpt.py --show-limits --raw

from __future__ import annotations

import argparse
import audioop
import collections
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from dataclasses import dataclass, field
from typing import Any

import sounddevice as sd
from curl_cffi import requests as cf_requests

# ============================================================
# URLS E CONFIG
# ============================================================
URL_ANON = "https://chatgpt.com/backend-anon/transcribe"
URL_AUTH = "https://chatgpt.com/backend-api/transcribe"
TOKEN_FILE = "token.txt"
OUTPUT_FILE = "transcricao.txt"

SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_MS = 30
SAMPLE_WIDTH = 2

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Quantos erros consecutivos no modo anon antes de tentar fallback
ANON_FAIL_THRESHOLD = 3

# ============================================================
# ESTADO GLOBAL DE AUTENTICACAO
# ============================================================

@dataclass
class AuthState:
    mode: str = "anon"            # "anon" ou "auth"
    token_data: dict = field(default_factory=dict)
    anon_fail_count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def device_id(self) -> str:
        if not hasattr(self, "_device_id"):
            self._device_id = str(uuid.uuid4())
        return self._device_id

    def session_id(self) -> str:
        if not hasattr(self, "_session_id"):
            self._session_id = str(uuid.uuid4())
        return self._session_id

    def report_anon_error(self, status_code: int) -> None:
        if status_code in (401, 403, 429):
            with self.lock:
                self.anon_fail_count += 1
                if self.anon_fail_count >= ANON_FAIL_THRESHOLD and self.mode == "anon":
                    print(
                        f"\n[auth] {self.anon_fail_count} erros {status_code} consecutivos no modo anon. "
                        "Ativando fallback para cookies.",
                        flush=True,
                    )
                    self._ativar_fallback()

    def report_anon_success(self) -> None:
        with self.lock:
            self.anon_fail_count = 0

    def _ativar_fallback(self) -> None:
        """Tenta carregar token.txt; se nao existir, pede interativamente."""
        dados = carregar_token_txt()
        if dados.get("authorization") and dados.get("cookie"):
            self.token_data = dados
            self.mode = "auth"
            print("[auth] token.txt carregado, usando backend-api com cookies.", flush=True)
        else:
            print("[auth] token.txt nao encontrado ou incompleto. Iniciando importacao interativa.", flush=True)
            dados = importar_headers_interativo()
            if dados:
                self.token_data = dados
                self.mode = "auth"
                print("[auth] Credenciais importadas, usando backend-api com cookies.", flush=True)
            else:
                print("[auth] Nao foi possivel obter credenciais. Continuando no modo anon.", flush=True)


AUTH_STATE = AuthState()

# ============================================================
# TOKEN / HEADERS (compatibilidade com stt-config-ok.py)
# ============================================================

def parse_devtools_block(texto: str) -> dict[str, str]:
    linhas = [l.rstrip() for l in texto.strip().splitlines()]
    headers: dict[str, str] = {}
    i = 0
    while i < len(linhas):
        linha = linhas[i].strip()
        if not linha:
            i += 1
            continue
        if linha in (":method", ":path", ":scheme", ":authority"):
            i += 2
            continue
        if i == 0 and "." in linha and " " not in linha:
            i += 1
            continue
        eh_chave = bool(re.match(r"^[a-z0-9:][a-z0-9\-]*$", linha.lower()))
        if eh_chave and i + 1 < len(linhas):
            valor = linhas[i + 1].strip()
            proximo_eh_chave = bool(re.match(r"^[a-z0-9:][a-z0-9\-]*$", valor.lower()))
            if not proximo_eh_chave:
                headers[linha.lower()] = valor
                i += 2
                continue
        if ": " in linha:
            k, _, v = linha.partition(": ")
            headers[k.strip().lower()] = v.strip()
        i += 1
    return headers


def extrair_credenciais(texto: str) -> tuple[dict[str, str], list[str]]:
    headers = parse_devtools_block(texto)
    dados = {
        "authorization": headers.get("authorization", ""),
        "cookie": headers.get("cookie", ""),
        "chatgpt-account-id": headers.get("chatgpt-account-id", ""),
        "oai-device-id": headers.get("oai-device-id", ""),
        "oai-language": headers.get("oai-language", "pt-BR"),
    }
    erros: list[str] = []
    if not dados["authorization"]:
        erros.append("Nao encontrei authorization.")
    if not dados["cookie"]:
        erros.append("Nao encontrei cookie.")
    return dados, erros


def carregar_token_txt() -> dict[str, str]:
    if not os.path.exists(TOKEN_FILE):
        return {}
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items() if v is not None}
    except Exception:
        return {}


def salvar_token_txt(dados: dict[str, str]) -> None:
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)


def importar_headers_interativo() -> dict[str, str] | None:
    print("\nCole os Request Headers do DevTools (modo fallback). Quando terminar, digite FIM.\n")
    linhas: list[str] = []
    while True:
        try:
            linha = input()
        except EOFError:
            break
        if linha.strip().upper() == "FIM":
            break
        linhas.append(linha)
    dados, erros = extrair_credenciais("\n".join(linhas))
    if erros:
        print("\n".join(erros))
        print("Nao deu pra salvar. Copie os headers completos.")
        return None
    salvar_token_txt(dados)
    print("\nCredenciais salvas em token.txt")
    return dados


# ============================================================
# AUDIO UTILS
# ============================================================

def rms(frame: bytes) -> int:
    return audioop.rms(frame, SAMPLE_WIDTH)


def volume_bar(volume: int, threshold: int) -> str:
    size = min(40, int(volume / max(1, threshold) * 20))
    return "#" * size


def stats(frames: list[bytes]) -> tuple[int, int]:
    if not frames:
        return 0, 0
    vals = [rms(f) for f in frames]
    return int(sum(vals) / len(vals)), max(vals)


@dataclass
class Turn:
    idx: int
    frames: list[bytes]
    duration_ms: int
    speech_ms: int
    avg: int
    peak: int
    closed_reason: str
    created_at: float


class TranscriptStore:
    def __init__(self, output_file: str) -> None:
        self.output_file = output_file
        self.lock = threading.Lock()
        self.texto_acumulado = ""

    @staticmethod
    def clean_text(texto: str) -> str:
        return " ".join((texto or "").strip().split())

    def append(self, text: str) -> None:
        text = self.clean_text(text)
        if not text:
            return
        with self.lock:
            if self.texto_acumulado:
                self.texto_acumulado += " " + text
            else:
                self.texto_acumulado = text
            with open(self.output_file, "w", encoding="utf-8") as f:
                f.write(self.texto_acumulado.strip() + "\n")


# ============================================================
# AUDIO -> ARQUIVO
# ============================================================

def salvar_wav(frames: list[bytes]) -> tuple[str, int]:
    audio_bytes = b"".join(frames)
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    temp_path = temp.name
    temp.close()
    with wave.open(temp_path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_bytes)
    duration_ms = int((len(audio_bytes) / SAMPLE_WIDTH / SAMPLE_RATE) * 1000)
    return temp_path, duration_ms


def converter_para_webm(wav_path: str) -> str:
    webm_path = wav_path.replace(".wav", ".webm")
    resultado = subprocess.run(
        [
            "ffmpeg", "-y", "-i", wav_path,
            "-c:a", "libopus", "-b:a", "32k",
            "-application", "voip",
            webm_path,
        ],
        capture_output=True,
        timeout=30,
    )
    if resultado.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou: {resultado.stderr.decode()[:300]}")
    return webm_path


# ============================================================
# TRANSCRICAO COM FALLBACK AUTOMATICO
# ============================================================

def montar_body(audio_path: str, duration_ms: int, audio_format: str = "wav") -> tuple[bytes, str]:
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()
    mime = "audio/webm" if audio_format == "webm" else "audio/wav"
    filename = f"audio.{audio_format}"
    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    body += f"Content-Type: {mime}\r\n\r\n".encode()
    body += audio_bytes
    body += f"\r\n--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="duration_ms"\r\n\r\n'
    body += f"{int(duration_ms)}\r\n".encode()
    body += f"--{boundary}--\r\n".encode()
    return body, boundary


def limpar_texto_resposta(data: Any) -> str:
    if isinstance(data, dict):
        texto = data.get("text") or data.get("transcript") or ""
    else:
        texto = str(data)
    return " ".join(texto.strip().split())


def _headers_anon(boundary: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "oai-device-id": AUTH_STATE.device_id(),
        "oai-session-id": AUTH_STATE.session_id(),
        "oai-language": "pt-BR",
    }


def _headers_auth(boundary: str, token_data: dict[str, str]) -> dict[str, str]:
    h = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Authorization": token_data["authorization"],
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "oai-language": token_data.get("oai-language", "pt-BR"),
    }
    if token_data.get("cookie"):
        h["Cookie"] = token_data["cookie"]
    if token_data.get("chatgpt-account-id"):
        h["chatgpt-account-id"] = token_data["chatgpt-account-id"]
    if token_data.get("oai-device-id"):
        h["oai-device-id"] = token_data["oai-device-id"]
    return h


def transcrever_arquivo(
    audio_path: str, duration_ms: int, audio_format: str = "wav"
) -> tuple[str, int, int, str]:
    """
    Tenta primeiro modo anon. Se falhar com erro de auth/rate-limit repetido,
    o AuthState muda para 'auth' automaticamente e a proxima chamada ja usa cookies.
    """
    body, boundary = montar_body(audio_path, duration_ms, audio_format)

    with AUTH_STATE.lock:
        current_mode = AUTH_STATE.mode
        token_data = dict(AUTH_STATE.token_data)

    if current_mode == "anon":
        url = URL_ANON
        headers = _headers_anon(boundary)
        mode_label = "anon"
    else:
        url = URL_AUTH
        headers = _headers_auth(boundary, token_data)
        mode_label = "auth"

    t0 = time.perf_counter()
    resp = cf_requests.post(url, headers=headers, data=body, impersonate="chrome", timeout=120)
    t1 = time.perf_counter()
    elapsed_ms = int((t1 - t0) * 1000)

    raw = resp.text[:600]
    status = resp.status_code

    if status == 200:
        try:
            texto = limpar_texto_resposta(resp.json())
        except Exception:
            texto = resp.text.strip()
        if mode_label == "anon":
            AUTH_STATE.report_anon_success()
        return texto, status, elapsed_ms, raw

    if mode_label == "anon":
        AUTH_STATE.report_anon_error(status)

    return f"[Erro {status}] {resp.text[:400]}", status, elapsed_ms, raw


def mostrar_limites(show_raw: bool = False) -> None:
    """Consulta os limites de dictation no backend-anon."""
    url = "https://chatgpt.com/backend-anon/me"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "oai-device-id": AUTH_STATE.device_id(),
        "oai-session-id": AUTH_STATE.session_id(),
    }
    try:
        resp = cf_requests.get(url, headers=headers, impersonate="chrome", timeout=30)
        print(f"[limits] status={resp.status_code}", flush=True)
        if show_raw:
            print(f"[limits raw] {resp.text[:800]}", flush=True)
        if resp.status_code == 200:
            try:
                data = resp.json()
                dictation = data.get("dictation_limit") or data.get("limits", {})
                print(f"[limits] {dictation}", flush=True)
            except Exception:
                pass
    except Exception as exc:
        print(f"[limits erro] {exc}", flush=True)


# ============================================================
# WORKER DE TRANSCRICAO
# ============================================================

def transcription_worker(
    turn_queue: queue.Queue,
    store: TranscriptStore,
    stop_event: threading.Event,
    args: argparse.Namespace,
) -> None:
    while not stop_event.is_set() or not turn_queue.empty():
        try:
            turn = turn_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        if turn is None:
            turn_queue.task_done()
            break

        wav_path = None
        audio_path = None
        try:
            wav_path, duration_ms = salvar_wav(turn.frames)
            audio_path = wav_path
            audio_format = "wav"

            if args.format == "webm":
                try:
                    audio_path = converter_para_webm(wav_path)
                    audio_format = "webm"
                except Exception as exc:
                    print(f"[webm] conversao falhou ({exc}), usando wav.", flush=True)

            qsize = turn_queue.qsize()
            with AUTH_STATE.lock:
                mode_label = AUTH_STATE.mode
            print(
                f"\n[turno {turn.idx}] transcrevendo dur={duration_ms}ms "
                f"fala={turn.speech_ms}ms avg={turn.avg} peak={turn.peak} "
                f"fila={qsize} modo={mode_label} fmt={audio_format}",
                flush=True,
            )

            texto, status, latency_ms, raw = transcrever_arquivo(audio_path, duration_ms, audio_format)
            ratio = latency_ms / max(1, duration_ms)

            if status == 200 and texto:
                print(f"[turno {turn.idx}] {texto}", flush=True)
                print(f"[http] {latency_ms}ms audio={duration_ms}ms ratio={ratio:.2f}x", flush=True)
                store.append(texto)
            elif status == 200:
                print(f"[turno {turn.idx}] sem texto", flush=True)
            else:
                print(f"[turno {turn.idx}] erro: {texto}", flush=True)

            if args.raw:
                print(f"[raw {turn.idx}] {raw[:600]}", flush=True)

        except Exception as exc:
            print(f"\n[worker erro turno {turn.idx}] {exc}", flush=True)
        finally:
            for p in set(filter(None, [wav_path, audio_path])):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            turn_queue.task_done()


# ============================================================
# CAPTURA CONTINUA
# ============================================================

def audio_callback(indata, frames, time_info, status, audio_queue: queue.Queue) -> None:
    if status:
        print("\nAudio status:", status, flush=True)
    try:
        audio_queue.put_nowait(bytes(indata))
    except queue.Full:
        pass


def run_capture(args: argparse.Namespace) -> None:
    frame_samples = int(SAMPLE_RATE * FRAME_MS / 1000)
    audio_queue: queue.Queue = queue.Queue(maxsize=args.audio_queue_size)
    turn_queue: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    store = TranscriptStore(args.output)

    if args.show_limits:
        mostrar_limites(show_raw=args.raw)

    worker = threading.Thread(
        target=transcription_worker,
        args=(turn_queue, store, stop_event, args),
        daemon=True,
    )
    worker.start()

    pre_roll_count = max(1, int(args.pre_roll_ms / FRAME_MS))
    silence_frames_limit = max(1, int(args.silence_ms / FRAME_MS))
    min_frames = max(1, int(args.min_ms / FRAME_MS))
    max_turn_frames = None
    if args.max_turn_seconds and args.max_turn_seconds > 0:
        max_turn_frames = max(1, int(args.max_turn_seconds * 1000 / FRAME_MS))
    start_required_frames = max(1, int(args.start_required_ms / FRAME_MS))
    continue_required_frames = max(1, int(args.continue_required_ms / FRAME_MS))
    noise_window_frames = max(1, int(args.noise_window_ms / FRAME_MS))

    threshold = args.threshold
    continue_threshold = args.continue_threshold

    pre_roll: collections.deque = collections.deque(maxlen=pre_roll_count)
    noise_values: collections.deque = collections.deque(maxlen=noise_window_frames)

    recording = False
    frames_buf: list[bytes] = []
    turn_idx = 0
    start_count = 0
    silence_count = 0
    speech_frames = 0
    voice_run = 0
    closed_count = 0
    dropped_short = 0
    discard_cooldown_frames = 0

    print("STT Anon ChatGPT iniciado.")
    print(f"Modo inicial: anon ({URL_ANON})")
    print(f"Fallback auto: backend-api com cookies apos {ANON_FAIL_THRESHOLD} erros 401/403/429")
    print(f"threshold={threshold}, continue={continue_threshold}, silence={args.silence_ms}ms")
    print("Microfone sempre ouvindo. Ctrl+C para sair.\n")

    try:
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=frame_samples,
            dtype="int16",
            channels=CHANNELS,
            callback=lambda indata, f, t, status: audio_callback(indata, f, t, status, audio_queue),
        ):
            while True:
                frame = audio_queue.get()
                volume = rms(frame)

                if not recording:
                    pre_roll.append(frame)
                    noise_values.append(volume)

                    in_discard_cooldown = discard_cooldown_frames > 0
                    if in_discard_cooldown:
                        discard_cooldown_frames -= 1

                    if args.adaptive:
                        noise_avg = int(sum(noise_values) / max(1, len(noise_values)))
                        adaptive_thr = int(noise_avg * args.threshold_mult)
                        threshold = min(args.threshold_cap, max(args.threshold, adaptive_thr))
                        continue_threshold = max(args.continue_threshold, int(threshold * args.continue_ratio))
                    else:
                        noise_avg = int(sum(noise_values) / max(1, len(noise_values))) if noise_values else 0
                        threshold = args.threshold
                        continue_threshold = args.continue_threshold

                    if in_discard_cooldown:
                        start_count = 0
                    elif volume >= threshold:
                        start_count += 1
                    else:
                        start_count = 0

                    if start_count >= start_required_frames:
                        recording = True
                        frames_buf = list(pre_roll)
                        silence_count = 0
                        speech_frames = start_count
                        voice_run = start_count
                        turn_idx += 1
                        print(f"\n[fala detectada #{turn_idx}]", flush=True)

                    if args.meter:
                        print(
                            f"\rvol={volume:<5} noise={noise_avg:<5} thr={threshold:<5} "
                            f"{'VOZ' if volume >= threshold else '...':<3} {volume_bar(volume, threshold):<40}",
                            end="",
                            flush=True,
                        )
                    continue

                frames_buf.append(frame)
                is_speech_raw = volume >= continue_threshold

                if is_speech_raw:
                    voice_run += 1
                else:
                    voice_run = 0

                is_confirmed_speech = voice_run >= continue_required_frames
                if is_confirmed_speech:
                    silence_count = 0
                    speech_frames += 1
                else:
                    silence_count += 1

                duration_ms = len(frames_buf) * FRAME_MS
                speech_ms = speech_frames * FRAME_MS

                if args.meter:
                    print(
                        f"\rgravando turno {turn_idx} {duration_ms/1000:>4.1f}s "
                        f"vol={volume:<5} {'VOZ' if is_confirmed_speech else ('pico' if is_speech_raw else '...'):<4} {volume_bar(volume, threshold):<40}",
                        end="",
                        flush=True,
                    )

                should_close_silence = silence_count >= silence_frames_limit and duration_ms >= args.min_ms
                should_close_max = bool(max_turn_frames and len(frames_buf) >= max_turn_frames)

                if should_close_silence or should_close_max:
                    reason = "silencio" if should_close_silence else "max_turn_seconds"
                    avg, peak = stats(frames_buf)
                    closed_count += 1

                    if duration_ms < args.min_ms or speech_ms < args.min_speech_ms:
                        dropped_short += 1
                        discard_cooldown_frames = max(0, int(args.discard_cooldown_ms / FRAME_MS))
                        print(
                            f"\n[ignorado] turno curto/ruido dur={duration_ms}ms fala={speech_ms}ms "
                            f"descartados={dropped_short} cooldown={args.discard_cooldown_ms}ms",
                            flush=True,
                        )
                    else:
                        turn = Turn(
                            idx=turn_idx,
                            frames=list(frames_buf),
                            duration_ms=duration_ms,
                            speech_ms=speech_ms,
                            avg=avg,
                            peak=peak,
                            closed_reason=reason,
                            created_at=time.time(),
                        )
                        turn_queue.put(turn)
                        print(
                            f"\n[turno {turn_idx}] fechado por {reason}: dur={duration_ms}ms "
                            f"fala={speech_ms}ms avg={avg} peak={peak} fila={turn_queue.qsize()}",
                            flush=True,
                        )

                    recording = False
                    frames_buf = []
                    silence_count = 0
                    speech_frames = 0
                    voice_run = 0
                    start_count = 0
                    pre_roll.clear()

    except KeyboardInterrupt:
        print("\nEncerrando... aguardando fila de transcricao terminar.")
    finally:
        stop_event.set()
        turn_queue.put(None)
        try:
            turn_queue.join()
        except Exception:
            pass
        print(f"Pronto. Texto salvo em {args.output}")


# ============================================================
# CLI
# ============================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="STT anonimo ChatGPT com fallback automatico para cookies."
    )
    p.add_argument("--format", choices=["wav", "webm"], default="wav",
                   help="Formato de audio enviado. webm requer ffmpeg.")
    p.add_argument("--show-limits", action="store_true",
                   help="Consulta limites de dictation antes de iniciar.")
    p.add_argument("--threshold", type=int, default=260)
    p.add_argument("--continue-threshold", type=int, default=220)
    p.add_argument("--silence-ms", type=int, default=800)
    p.add_argument("--pre-roll-ms", type=int, default=600)
    p.add_argument("--start-required-ms", type=int, default=80)
    p.add_argument("--min-ms", type=int, default=700)
    p.add_argument("--min-speech-ms", type=int, default=250)
    p.add_argument("--max-turn-seconds", type=float, default=0.0)
    p.add_argument("--continue-required-ms", type=int, default=90)
    p.add_argument("--discard-cooldown-ms", type=int, default=250)
    p.add_argument("--output", default=OUTPUT_FILE)
    p.add_argument("--meter", action="store_true", default=True)
    p.add_argument("--no-meter", dest="meter", action="store_false")
    p.add_argument("--raw", action="store_true")
    p.add_argument("--audio-queue-size", type=int, default=5000)
    p.add_argument("--adaptive", action="store_true")
    p.add_argument("--threshold-mult", type=float, default=1.4)
    p.add_argument("--threshold-cap", type=int, default=420)
    p.add_argument("--continue-ratio", type=float, default=0.65)
    p.add_argument("--noise-window-ms", type=int, default=900)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    run_capture(args)


if __name__ == "__main__":
    main()
