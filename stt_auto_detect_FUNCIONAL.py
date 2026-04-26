# stt_auto_detect_v7_short.py
# pip install sounddevice numpy curl_cffi
#
# Uso:
#   python stt_auto_detect_v7_short.py
#
# Para parar:
#   Ctrl+C

import os
import re
import json
import uuid
import wave
import queue
import tempfile
import threading
import collections
import audioop
import time

import sounddevice as sd
from curl_cffi import requests as cf_requests


TOKEN_FILE = "token.txt"
OUTPUT_FILE = "transcricao.txt"
URL = "https://chatgpt.com/backend-api/transcribe"

# ============================================================
# AUDIO
# ============================================================

SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_MS = 30

# ============================================================
# DETECTOR FIXO PARA FALAS CURTAS
# ============================================================

THRESHOLD = 260

# Pega "oi", "sim", "nao", "ta"
START_REQUIRED_MS = 180
SILENCE_END_MS = 700

# Guarda um pouco antes da fala
PRE_ROLL_MS = 500

# Envia automaticamente mesmo se a pessoa falar sem parar
SEND_EVERY_SECONDS = 5
OVERLAP_MS = 1200

# Baixos o suficiente pra falas curtas nao serem ignoradas
MIN_SEGMENT_MS = 500
MIN_SPEECH_MS = 180

# Filtros mais leves pra nao matar "oi"
MIN_PEAK_TO_SEND = 350
MIN_AVG_TO_SEND = 70

DEBUG_VOLUME = True
DEBUG_EVERY_MS = 300


# ============================================================
# TOKEN / DEVTOOLS
# ============================================================

def parse_devtools_block(texto: str) -> dict:
    linhas = [l.rstrip() for l in texto.strip().splitlines()]
    headers = {}
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


def extrair_credenciais(texto: str):
    headers = parse_devtools_block(texto)

    token = headers.get("authorization", "")
    cookie = headers.get("cookie", "")
    account = headers.get("chatgpt-account-id", "")
    device = headers.get("oai-device-id", "")
    language = headers.get("oai-language", "pt-BR")

    erros = []

    if not token:
        erros.append("Nao encontrei authorization.")

    if not cookie:
        erros.append("Nao encontrei cookie.")

    return token, cookie, account, device, language, erros


def salvar_token_txt(token, cookie, account, device, language):
    dados = {
        "authorization": token,
        "cookie": cookie,
        "chatgpt-account-id": account,
        "oai-device-id": device,
        "oai-language": language,
    }

    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)


def carregar_token_txt():
    if not os.path.exists(TOKEN_FILE):
        return "", "", "", "", "pt-BR"

    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        dados = json.load(f)

    return (
        dados.get("authorization", ""),
        dados.get("cookie", ""),
        dados.get("chatgpt-account-id", ""),
        dados.get("oai-device-id", ""),
        dados.get("oai-language", "pt-BR"),
    )


def importar_headers_interativo():
    print("\nPrimeira execucao: cole os Request Headers do DevTools.")
    print("Quando terminar, digite FIM em uma linha separada.\n")

    linhas = []

    while True:
        linha = input()
        if linha.strip().upper() == "FIM":
            break
        linhas.append(linha)

    texto = "\n".join(linhas)

    token, cookie, account, device, language, erros = extrair_credenciais(texto)

    if not token:
        print("\n".join(erros))
        print("\nNao deu pra salvar. Copia os Request Headers completos.")
        return False

    salvar_token_txt(token, cookie, account, device, language)

    print("\nCredenciais salvas em token.txt")
    print("Bearer:   " + token[:50] + "...")
    print("Cookie:   " + (cookie[:60] + "..." if cookie else "nao encontrado"))
    print("Account:  " + (account or "nao encontrado"))
    print("Device:   " + (device or "nao encontrado"))
    print("Language: " + language)

    return True


def garantir_token():
    token, _, _, _, _ = carregar_token_txt()

    if token:
        return True

    return importar_headers_interativo()


# ============================================================
# TEXTO / DEDUP
# ============================================================

texto_acumulado = ""
texto_lock = threading.Lock()


def remover_repeticao(previo: str, novo: str) -> str:
    previo = previo.strip()
    novo = novo.strip()

    if not previo or not novo:
        return novo

    previo_low = previo.lower()
    novo_low = novo.lower()

    max_overlap = min(len(previo_low), len(novo_low), 180)

    for n in range(max_overlap, 12, -1):
        if previo_low[-n:] == novo_low[:n]:
            return novo[n:].strip()

    return novo


def salvar_texto_acumulado(novo_texto: str):
    global texto_acumulado

    novo_texto = novo_texto.strip()

    if not novo_texto:
        return ""

    with texto_lock:
        limpo = remover_repeticao(texto_acumulado, novo_texto)

        if not limpo:
            return ""

        if texto_acumulado:
            texto_acumulado += " " + limpo
        else:
            texto_acumulado = limpo

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(texto_acumulado.strip() + "\n")

        return limpo


# ============================================================
# STT
# ============================================================

def salvar_wav(frames: list[bytes]) -> tuple[str, int]:
    audio_bytes = b"".join(frames)

    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    temp_path = temp.name
    temp.close()

    with wave.open(temp_path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_bytes)

    duration_ms = int((len(audio_bytes) / 2 / SAMPLE_RATE) * 1000)

    return temp_path, duration_ms


def montar_body(audio_path: str, duration_ms: int):
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


def limpar_texto_resposta(data):
    if isinstance(data, dict):
        texto = data.get("text") or data.get("transcript") or ""
    else:
        texto = str(data)

    texto = texto.strip()

    if not texto:
        return ""

    if texto in ("{'text': ''}", '{"text": ""}'):
        return ""

    return texto


def texto_parece_lixo(texto: str) -> bool:
    texto_limpo = texto.strip().lower()

    if not texto_limpo:
        return True

    lixo_exato = {
        "bye.",
        "bye",
        "ok, well, here we go.",
        "ok, well, here we go",
        "here we go.",
        "here we go",
    }

    if texto_limpo in lixo_exato:
        return True

    # Nao filtra "ok" sozinho porque em portugues/uso real pode ser comando valido.
    return False


def transcrever_arquivo(audio_path: str, duration_ms: int):
    token, cookie, account_id, device_id, language = carregar_token_txt()

    if not token:
        return "[erro] Sem authorization no token.txt."

    body, boundary = montar_body(audio_path, duration_ms)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Authorization": token,
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "oai-language": language,
    }

    if cookie:
        headers["Cookie"] = cookie

    if account_id:
        headers["chatgpt-account-id"] = account_id

    if device_id:
        headers["oai-device-id"] = device_id

    try:
        resp = cf_requests.post(
            URL,
            headers=headers,
            data=body,
            impersonate="chrome",
            timeout=120,
        )

        if resp.status_code == 200:
            return limpar_texto_resposta(resp.json())

        if resp.status_code == 401:
            return "[401] Token expirado. Apague token.txt e rode de novo."

        if resp.status_code == 403:
            return "[403] Cloudflare bloqueou: " + resp.text[:300]

        return f"[Erro {resp.status_code}] {resp.text[:400]}"

    except Exception as e:
        return "[excecao] " + str(e)


# ============================================================
# AUDIO STATS
# ============================================================

def rms(frame: bytes) -> int:
    return audioop.rms(frame, 2)


def frame_stats(frames: list[bytes]) -> tuple[int, int]:
    if not frames:
        return 0, 0

    values = [rms(f) for f in frames]
    avg = int(sum(values) / len(values))
    peak = max(values)

    return avg, peak


def audio_callback(indata, frames, time_info, status, audio_queue):
    if status:
        print("Audio status:", status)

    audio_queue.put(bytes(indata))


def volume_bar(volume: int, threshold: int) -> str:
    size = min(40, int(volume / max(1, threshold) * 20))
    return "#" * size


# ============================================================
# TRANSCRICAO EM THREAD
# ============================================================

def transcrever_em_thread(segment_frames, contador, motivo):
    temp_path = None

    try:
        avg, peak = frame_stats(segment_frames)
        duration_ms_est = len(segment_frames) * FRAME_MS

        if duration_ms_est < MIN_SEGMENT_MS:
            print(f"\n[segmento {contador}] ignorado: curto demais ({duration_ms_est}ms)")
            return

        if peak < MIN_PEAK_TO_SEND:
            print(f"\n[segmento {contador}] ignorado: pico baixo demais avg={avg} peak={peak}")
            return

        if avg < MIN_AVG_TO_SEND:
            print(f"\n[segmento {contador}] ignorado: media baixa demais avg={avg} peak={peak}")
            return

        temp_path, duration_ms = salvar_wav(segment_frames)

        print(f"\n[segmento {contador}] enviando {duration_ms}ms motivo={motivo} avg={avg} peak={peak}...")
        texto = transcrever_arquivo(temp_path, duration_ms)

        if texto_parece_lixo(texto):
            print(f"\n--- Segmento {contador} ---")
            print(f"[ignorado: provavel alucinacao de ruido] {texto!r}")
            return

        texto_final = salvar_texto_acumulado(texto)

        if texto_final.strip():
            print(f"\n--- Segmento {contador} ---")
            print(texto_final.strip())
            print(f"\n[texto salvo em {OUTPUT_FILE}]")
        else:
            print(f"\n--- Segmento {contador} ---")
            print("[sem texto novo detectado]")

    except Exception as e:
        print("\nErro ao transcrever segmento:", str(e))

    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


# ============================================================
# LOOP PRINCIPAL
# ============================================================

def reset_estado():
    return {
        "triggered": False,
        "voiced_frames": [],
        "start_speech_frames": 0,
        "total_speech_frames": 0,
        "silence_frames": 0,
    }


def loop_auto_detect():
    frame_samples = int(SAMPLE_RATE * FRAME_MS / 1000)
    blocksize = frame_samples

    pre_roll_frames_count = max(1, PRE_ROLL_MS // FRAME_MS)
    silence_frames_limit = max(1, SILENCE_END_MS // FRAME_MS)
    start_required_frames = max(1, START_REQUIRED_MS // FRAME_MS)
    min_speech_frames = max(1, MIN_SPEECH_MS // FRAME_MS)
    send_every_frames = max(1, int(SEND_EVERY_SECONDS * 1000 / FRAME_MS))
    overlap_frames_count = max(1, OVERLAP_MS // FRAME_MS)

    pre_roll = collections.deque(maxlen=pre_roll_frames_count)
    audio_queue = queue.Queue()

    estado = reset_estado()
    contador = 1
    last_debug = 0
    threshold = THRESHOLD

    print("\nSTT auto detector v7 SHORT iniciado.")
    print(f"Threshold fixo: {threshold}")
    print(f"Envia falas curtas e chunks a cada {SEND_EVERY_SECONDS}s com overlap de {OVERLAP_MS}ms.")
    print("Fecha por silencio tambem.")
    print("Para sair: Ctrl+C\n")

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=blocksize,
        dtype="int16",
        channels=CHANNELS,
        callback=lambda indata, frames, time_info, status: audio_callback(
            indata, frames, time_info, status, audio_queue
        ),
    ):
        while True:
            frame = audio_queue.get()

            if len(frame) != frame_samples * 2:
                continue

            volume = rms(frame)
            is_speech = volume >= threshold

            now = time.time() * 1000
            if DEBUG_VOLUME and now - last_debug >= DEBUG_EVERY_MS:
                flag = "VOZ" if is_speech else "..."
                print(
                    f"\rvolume={volume:<5} threshold={threshold:<5} {flag:<3} {volume_bar(volume, threshold):<40}",
                    end="",
                    flush=True,
                )
                last_debug = now

            # -----------------------------
            # ESPERANDO FALA
            # -----------------------------
            if not estado["triggered"]:
                pre_roll.append(frame)

                if is_speech:
                    estado["start_speech_frames"] += 1
                else:
                    estado["start_speech_frames"] = 0

                if estado["start_speech_frames"] >= start_required_frames:
                    estado["triggered"] = True
                    estado["voiced_frames"] = list(pre_roll)
                    estado["total_speech_frames"] = estado["start_speech_frames"]
                    estado["silence_frames"] = 0
                    print("\n[fala detectada]", flush=True)

            # -----------------------------
            # GRAVANDO SEGMENTO
            # -----------------------------
            else:
                estado["voiced_frames"].append(frame)

                if is_speech:
                    estado["total_speech_frames"] += 1
                    estado["silence_frames"] = 0
                else:
                    estado["silence_frames"] += 1

                should_send_by_time = len(estado["voiced_frames"]) >= send_every_frames
                should_send_by_silence = estado["silence_frames"] >= silence_frames_limit

                if should_send_by_time or should_send_by_silence:
                    segment_duration_ms = len(estado["voiced_frames"]) * FRAME_MS
                    speech_duration_ms = estado["total_speech_frames"] * FRAME_MS

                    valid_by_speech = estado["total_speech_frames"] >= min_speech_frames
                    valid_by_duration = segment_duration_ms >= MIN_SEGMENT_MS

                    if valid_by_speech and valid_by_duration:
                        segment = estado["voiced_frames"][:]
                        motivo = "tempo" if should_send_by_time else "silencio"

                        t = threading.Thread(
                            target=transcrever_em_thread,
                            args=(segment, contador, motivo),
                            daemon=True,
                        )
                        t.start()

                        contador += 1
                    else:
                        print(
                            f"\n[ignorado] fala curta/ruido "
                            f"segmento={segment_duration_ms}ms "
                            f"fala={speech_duration_ms}ms"
                        )

                    # Se enviou por tempo, continua com overlap
                    if should_send_by_time and not should_send_by_silence:
                        keep = estado["voiced_frames"][-overlap_frames_count:]

                        estado["triggered"] = True
                        estado["voiced_frames"] = keep[:]
                        estado["start_speech_frames"] = 0
                        estado["total_speech_frames"] = len(keep)
                        estado["silence_frames"] = 0
                        pre_roll.clear()

                        print("\n[chunk enviado por tempo, continuando com overlap]", flush=True)

                    # Se fechou por silencio, espera nova fala
                    else:
                        estado = reset_estado()
                        pre_roll.clear()

                        print("\n[segmento fechado por silencio]", flush=True)


def main():
    print("STT Terminal auto detector v7 SHORT")

    if not garantir_token():
        return

    try:
        loop_auto_detect()
    except KeyboardInterrupt:
        print("\nEncerrado pelo usuario.")


if __name__ == "__main__":
    main()