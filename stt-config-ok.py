#!/usr/bin/env python3
# stt-config-ok.py
#
# STT CONFIG OK: continuo por turnos com configuracao aprovada:
# - microfone fica SEMPRE ouvindo
# - 1s de silencio fecha um turno
# - turno fechado entra numa fila
# - worker transcreve em paralelo, sem pausar a captura
# - imprime e salva em transcricao.txt na ordem
# - v3: nao corta por tempo, filtra teclado com start debounce e cooldown apos ruido curto
#
# CONFIG OK validada no teste:
#   --threshold 260 --continue-threshold 220 --start-required-ms 80
#   --continue-required-ms 90 --min-speech-ms 250 --silence-ms 800
#   --discard-cooldown-ms 250
#
# Resultado observado: pegou fala curta como "ruim", frases longas e contagem ate 50,
# com microfone sempre ouvindo e transcricao em fila.
#
# Instalar:
#   pip install sounddevice numpy curl_cffi
#
# Rodar:
#   python stt-config-ok.py
#   python stt-config-ok.py --threshold 360
#   python stt-config-ok.py --silence-ms 1000 --threshold 260

from __future__ import annotations

import argparse
import audioop
import collections
import json
import os
import queue
import re
import tempfile
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from typing import Any

import sounddevice as sd
from curl_cffi import requests as cf_requests

TOKEN_FILE = "token.txt"
OUTPUT_FILE = "transcricao.txt"
URL = "https://chatgpt.com/backend-api/transcribe"

SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_MS = 30
SAMPLE_WIDTH = 2

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)


# ============================================================
# TOKEN / HEADERS
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
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {str(k): str(v) for k, v in data.items() if v is not None}


def salvar_token_txt(dados: dict[str, str]) -> None:
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)


def importar_headers_interativo() -> dict[str, str] | None:
    print("\nCole os Request Headers do DevTools. Quando terminar, digite FIM.\n")
    linhas: list[str] = []
    while True:
        linha = input()
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
    print("Bearer:", dados.get("authorization", "")[:50] + "...")
    print("Cookie:", dados.get("cookie", "")[:60] + "...")
    return dados


def garantir_token() -> dict[str, str] | None:
    dados = carregar_token_txt()
    if dados.get("authorization") and dados.get("cookie"):
        return dados
    return importar_headers_interativo()


# ============================================================
# AUDIO / TURNOS
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
# TRANSCRIBE
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


def montar_body(audio_path: str, duration_ms: int) -> tuple[bytes, str]:
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
    body += b"Content-Type: audio/wav\r\n\r\n"
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


def transcrever_arquivo(audio_path: str, duration_ms: int, token_data: dict[str, str]) -> tuple[str, int, int, str]:
    body, boundary = montar_body(audio_path, duration_ms)

    headers = {
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
        headers["Cookie"] = token_data["cookie"]
    if token_data.get("chatgpt-account-id"):
        headers["chatgpt-account-id"] = token_data["chatgpt-account-id"]
    if token_data.get("oai-device-id"):
        headers["oai-device-id"] = token_data["oai-device-id"]

    t0 = time.perf_counter()
    resp = cf_requests.post(URL, headers=headers, data=body, impersonate="chrome", timeout=120)
    t1 = time.perf_counter()
    elapsed_ms = int((t1 - t0) * 1000)

    raw = resp.text[:600]
    if resp.status_code == 200:
        try:
            texto = limpar_texto_resposta(resp.json())
        except Exception:
            texto = resp.text.strip()
        return texto, resp.status_code, elapsed_ms, raw

    return f"[Erro {resp.status_code}] {resp.text[:400]}", resp.status_code, elapsed_ms, raw


def transcription_worker(
    turn_queue: queue.Queue[Turn | None],
    token_data: dict[str, str],
    store: TranscriptStore,
    stop_event: threading.Event,
    show_raw: bool,
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
        try:
            wav_path, duration_ms = salvar_wav(turn.frames)
            qsize = turn_queue.qsize()
            print(
                f"\n[turno {turn.idx}] transcrevendo dur={duration_ms}ms "
                f"fala={turn.speech_ms}ms avg={turn.avg} peak={turn.peak} fila={qsize}",
                flush=True,
            )
            texto, status, latency_ms, raw = transcrever_arquivo(wav_path, duration_ms, token_data)
            ratio = latency_ms / max(1, duration_ms)

            if status == 200 and texto:
                print(f"[turno {turn.idx}] {texto}", flush=True)
                print(f"[http] {latency_ms}ms audio={duration_ms}ms ratio={ratio:.2f}x", flush=True)
                store.append(texto)
            elif status == 200:
                print(f"[turno {turn.idx}] sem texto", flush=True)
            else:
                print(f"[turno {turn.idx}] erro: {texto}", flush=True)

            if show_raw:
                print(f"[raw {turn.idx}] {raw[:600]}", flush=True)
        except Exception as exc:
            print(f"\n[worker erro turno {turn.idx}] {exc}", flush=True)
        finally:
            if wav_path and os.path.exists(wav_path):
                try:
                    os.remove(wav_path)
                except Exception:
                    pass
            turn_queue.task_done()


# ============================================================
# CAPTURA CONTINUA
# ============================================================

def audio_callback(indata, frames, time_info, status, audio_queue: queue.Queue[bytes]) -> None:  # noqa: ANN001
    if status:
        print("\nAudio status:", status, flush=True)
    try:
        audio_queue.put_nowait(bytes(indata))
    except queue.Full:
        # Se isso acontecer, o processamento local travou. Evita bloquear callback de audio.
        pass


def run_capture(args: argparse.Namespace, token_data: dict[str, str]) -> None:
    frame_samples = int(SAMPLE_RATE * FRAME_MS / 1000)
    audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=args.audio_queue_size)
    turn_queue: queue.Queue[Turn | None] = queue.Queue()
    stop_event = threading.Event()
    store = TranscriptStore(args.output)

    worker = threading.Thread(
        target=transcription_worker,
        args=(turn_queue, token_data, store, stop_event, args.raw),
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

    pre_roll: collections.deque[bytes] = collections.deque(maxlen=pre_roll_count)
    noise_values: collections.deque[int] = collections.deque(maxlen=noise_window_frames)

    recording = False
    frames: list[bytes] = []
    turn_idx = 0
    start_count = 0
    silence_count = 0
    speech_frames = 0
    voice_run = 0
    closed_count = 0
    dropped_short = 0
    discard_cooldown_frames = 0

    print("STT CONFIG OK iniciado.")
    max_info = "desligado" if not args.max_turn_seconds or args.max_turn_seconds <= 0 else f"{args.max_turn_seconds}s"
    print(f"threshold={threshold}, continue={continue_threshold}, silence={args.silence_ms}ms, pre_roll={args.pre_roll_ms}ms")
    print(f"continue_required={args.continue_required_ms}ms, min_speech={args.min_speech_ms}ms, max_turn={max_info}")
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

                    # Por padrao, threshold fixo. Se usar --adaptive, ele so aumenta ate o maximo configurado.
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
                        frames = list(pre_roll)
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

                frames.append(frame)
                is_speech_raw = volume >= continue_threshold

                # Anti-teclado / anti-clique:
                # pico isolado acima do threshold NAO zera o silencio.
                # So continua a fala depois de alguns frames consecutivos.
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

                duration_ms = len(frames) * FRAME_MS
                speech_ms = speech_frames * FRAME_MS

                if args.meter:
                    print(
                        f"\rgravando turno {turn_idx} {duration_ms/1000:>4.1f}s "
                        f"vol={volume:<5} {'VOZ' if is_confirmed_speech else ('pico' if is_speech_raw else '...'):<4} {volume_bar(volume, threshold):<40}",
                        end="",
                        flush=True,
                    )

                should_close_silence = silence_count >= silence_frames_limit and duration_ms >= args.min_ms
                should_close_max = bool(max_turn_frames and len(frames) >= max_turn_frames)

                if should_close_silence or should_close_max:
                    reason = "silencio" if should_close_silence else "max_turn_seconds"
                    avg, peak = stats(frames)
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
                            frames=list(frames),
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

                    # Volta para IDLE sem parar o stream. O pre_roll continua recebendo frames.
                    recording = False
                    frames = []
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
    p = argparse.ArgumentParser(description="STT CONFIG OK: continuo por turnos com configuracao aprovada: microfone sempre ouvindo, transcricao em fila.")
    p.add_argument("--threshold", type=int, default=260, help="Threshold fixo para iniciar fala. Teste 260 ou 360.")
    p.add_argument("--continue-threshold", type=int, default=220, help="Threshold para continuar fala durante o turno. Config OK usa 220 para nao perder fala curta.")
    p.add_argument("--silence-ms", type=int, default=800, help="Silencio para fechar turno. Config OK usa 800ms.")
    p.add_argument("--pre-roll-ms", type=int, default=600, help="Audio antes do gatilho para nao comer comeco.")
    p.add_argument("--start-required-ms", type=int, default=80, help="Tempo minimo acima do threshold para iniciar. Config OK usa 80ms para pegar fala curta.")
    p.add_argument("--min-ms", type=int, default=700, help="Duracao minima do turno para enviar.")
    p.add_argument("--min-speech-ms", type=int, default=250, help="Tempo minimo real de voz dentro do turno. Config OK usa 250ms para aceitar falas curtas.")
    p.add_argument("--max-turn-seconds", type=float, default=0.0, help="Fecha por tempo maximo. Padrao 0 = desligado, para nao cortar frase no meio.")
    p.add_argument("--continue-required-ms", type=int, default=90, help="Tempo acima do continue-threshold para contar como fala real. Config OK usa 90ms.")
    p.add_argument("--discard-cooldown-ms", type=int, default=250, help="Depois de descartar ruido curto, ignora novos gatilhos por esse tempo, sem parar o microfone. Config OK usa 250ms.")
    p.add_argument("--output", default=OUTPUT_FILE, help="Arquivo de saida acumulada.")
    p.add_argument("--meter", action="store_true", default=True, help="Mostra medidor de volume.")
    p.add_argument("--no-meter", dest="meter", action="store_false", help="Desliga medidor de volume.")
    p.add_argument("--raw", action="store_true", help="Mostra resposta raw curta da API.")
    p.add_argument("--audio-queue-size", type=int, default=5000, help="Buffer interno do microfone em frames.")

    # Modo adaptativo opcional, mas limitado para nao repetir a tragedia do thr=1800.
    p.add_argument("--adaptive", action="store_true", help="Usa threshold adaptativo limitado por threshold-cap.")
    p.add_argument("--threshold-mult", type=float, default=1.4, help="Multiplicador do ruido no modo adaptive.")
    p.add_argument("--threshold-cap", type=int, default=420, help="Maximo permitido para threshold adaptativo.")
    p.add_argument("--continue-ratio", type=float, default=0.65, help="Ratio do threshold para continuar no modo adaptive.")
    p.add_argument("--noise-window-ms", type=int, default=900, help="Janela de ruido para modo adaptive/medidor.")
    return p.parse_args()


def main() -> None:
    token_data = garantir_token()
    if not token_data:
        return
    args = parse_args()
    run_capture(args, token_data)


if __name__ == "__main__":
    main()
