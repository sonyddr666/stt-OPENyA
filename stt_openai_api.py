#!/usr/bin/env python3
"""Continuous speech-to-text using the official OpenAI API.

This version does not use ChatGPT browser cookies. It reads OPENAI_API_KEY from
an environment variable and keeps the same turn-based microphone capture style:

    pip install sounddevice openai
    export OPENAI_API_KEY="your_api_key_here"
    python stt_openai_api.py

On Windows PowerShell:

    setx OPENAI_API_KEY "your_api_key_here"
    python stt_openai_api.py
"""

from __future__ import annotations

import argparse
import audioop
import collections
import os
import queue
import tempfile
import threading
import time
import wave
from dataclasses import dataclass

import sounddevice as sd
from openai import OpenAI

OUTPUT_FILE = "transcricao.txt"
SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_MS = 30
SAMPLE_WIDTH = 2


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


class OpenAITranscriber:
    def __init__(self, model: str, language: str | None, prompt: str | None) -> None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY nao encontrada no ambiente.")
        self.client = OpenAI()
        self.model = model
        self.language = language
        self.prompt = prompt

    def transcribe(self, audio_path: str) -> str:
        with open(audio_path, "rb") as audio_file:
            kwargs = {
                "model": self.model,
                "file": audio_file,
            }
            if self.language:
                kwargs["language"] = self.language
            if self.prompt:
                kwargs["prompt"] = self.prompt
            result = self.client.audio.transcriptions.create(**kwargs)
        return getattr(result, "text", "") or ""


def transcription_worker(
    turn_queue: queue.Queue[Turn | None],
    transcriber: OpenAITranscriber,
    store: TranscriptStore,
    stop_event: threading.Event,
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
            t0 = time.perf_counter()
            texto = transcriber.transcribe(wav_path)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            ratio = elapsed_ms / max(1, duration_ms)

            if texto:
                print(f"[turno {turn.idx}] {texto}", flush=True)
                print(f"[api] {elapsed_ms}ms audio={duration_ms}ms ratio={ratio:.2f}x", flush=True)
                store.append(texto)
            else:
                print(f"[turno {turn.idx}] sem texto", flush=True)
        except Exception as exc:
            print(f"\n[worker erro turno {turn.idx}] {exc}", flush=True)
        finally:
            if wav_path and os.path.exists(wav_path):
                try:
                    os.remove(wav_path)
                except Exception:
                    pass
            turn_queue.task_done()


def audio_callback(indata, frames, time_info, status, audio_queue: queue.Queue[bytes]) -> None:  # noqa: ANN001
    if status:
        print("\nAudio status:", status, flush=True)
    try:
        audio_queue.put_nowait(bytes(indata))
    except queue.Full:
        pass


def run_capture(args: argparse.Namespace) -> None:
    transcriber = OpenAITranscriber(args.model, args.language, args.prompt)
    frame_samples = int(SAMPLE_RATE * FRAME_MS / 1000)
    audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=args.audio_queue_size)
    turn_queue: queue.Queue[Turn | None] = queue.Queue()
    stop_event = threading.Event()
    store = TranscriptStore(args.output)

    worker = threading.Thread(
        target=transcription_worker,
        args=(turn_queue, transcriber, store, stop_event),
        daemon=True,
    )
    worker.start()

    pre_roll_count = max(1, int(args.pre_roll_ms / FRAME_MS))
    silence_frames_limit = max(1, int(args.silence_ms / FRAME_MS))
    start_required_frames = max(1, int(args.start_required_ms / FRAME_MS))
    continue_required_frames = max(1, int(args.continue_required_ms / FRAME_MS))
    noise_window_frames = max(1, int(args.noise_window_ms / FRAME_MS))

    max_turn_frames = None
    if args.max_turn_seconds and args.max_turn_seconds > 0:
        max_turn_frames = max(1, int(args.max_turn_seconds * 1000 / FRAME_MS))

    pre_roll: collections.deque[bytes] = collections.deque(maxlen=pre_roll_count)
    noise_values: collections.deque[int] = collections.deque(maxlen=noise_window_frames)

    recording = False
    frames: list[bytes] = []
    turn_idx = 0
    start_count = 0
    silence_count = 0
    speech_frames = 0
    voice_run = 0
    dropped_short = 0
    discard_cooldown_frames = 0

    print("STT OpenAI API iniciado.")
    print(f"model={args.model}, language={args.language or 'auto'}")
    print(f"threshold={args.threshold}, continue={args.continue_threshold}, silence={args.silence_ms}ms")
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
                    noise_avg = int(sum(noise_values) / max(1, len(noise_values))) if noise_values else 0

                    if discard_cooldown_frames > 0:
                        discard_cooldown_frames -= 1
                        start_count = 0
                    elif volume >= args.threshold:
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
                            f"\rvol={volume:<5} noise={noise_avg:<5} thr={args.threshold:<5} "
                            f"{'VOZ' if volume >= args.threshold else '...':<3} {volume_bar(volume, args.threshold):<40}",
                            end="",
                            flush=True,
                        )
                    continue

                frames.append(frame)
                is_speech_raw = volume >= args.continue_threshold
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
                        f"vol={volume:<5} {'VOZ' if is_confirmed_speech else ('pico' if is_speech_raw else '...'):<4} {volume_bar(volume, args.threshold):<40}",
                        end="",
                        flush=True,
                    )

                should_close_silence = silence_count >= silence_frames_limit and duration_ms >= args.min_ms
                should_close_max = bool(max_turn_frames and len(frames) >= max_turn_frames)

                if should_close_silence or should_close_max:
                    reason = "silencio" if should_close_silence else "max_turn_seconds"
                    avg, peak = stats(frames)

                    if duration_ms < args.min_ms or speech_ms < args.min_speech_ms:
                        dropped_short += 1
                        discard_cooldown_frames = max(0, int(args.discard_cooldown_ms / FRAME_MS))
                        print(
                            f"\n[ignorado] turno curto/ruido dur={duration_ms}ms fala={speech_ms}ms "
                            f"descartados={dropped_short}",
                            flush=True,
                        )
                    else:
                        turn_queue.put(
                            Turn(
                                idx=turn_idx,
                                frames=list(frames),
                                duration_ms=duration_ms,
                                speech_ms=speech_ms,
                                avg=avg,
                                peak=peak,
                                closed_reason=reason,
                                created_at=time.time(),
                            )
                        )
                        print(
                            f"\n[turno {turn_idx}] fechado por {reason}: dur={duration_ms}ms "
                            f"fala={speech_ms}ms avg={avg} peak={peak} fila={turn_queue.qsize()}",
                            flush=True,
                        )

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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="STT continuo usando a API oficial da OpenAI.")
    p.add_argument("--model", default="gpt-4o-mini-transcribe", help="Modelo de transcricao.")
    p.add_argument("--language", default="pt", help="Idioma ISO, exemplo: pt. Use vazio para autodetectar.")
    p.add_argument("--prompt", default=None, help="Prompt opcional de contexto para transcricao.")
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
    p.add_argument("--audio-queue-size", type=int, default=5000)
    p.add_argument("--noise-window-ms", type=int, default=900)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.language == "":
        args.language = None
    run_capture(args)


if __name__ == "__main__":
    main()
