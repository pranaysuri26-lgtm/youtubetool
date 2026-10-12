"""Local speech-to-text behind one interface. Backends:

- whisper_cpp   : reuses an existing whisper.cpp install (whisper-cli) + ggml model — preferred on the Mac.
- sherpa_onnx   : sherpa-onnx transducer models (tested here with NVIDIA Parakeet-TDT 0.6B v2 int8, CPU).
- faster_whisper: if the user already has it installed.

Every backend returns the same structure: segments [{start,end,text}] and words [{start,end,word,conf}].
Audio is decoded once with FFmpeg to 16 kHz mono; results are cached by file hash + backend + model.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import wave
from pathlib import Path

import numpy as np

from ..util import ToolError, run, which


def decode_pcm16k(path: str | Path, start: float | None = None, end: float | None = None,
                  stream_index: int | None = None) -> np.ndarray:
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if end:
        cmd += ["-t", f"{end - (start or 0):.3f}"]
    if stream_index is not None:
        cmd += ["-map", f"0:{stream_index}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
    import subprocess
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise ToolError("ffmpeg could not decode audio", output=p.stderr.decode(errors="replace")[-2000:])
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _chunks(audio: np.ndarray, sr: int = 16000, target: float = 24.0, window: float = 4.0):
    """Split long audio at the quietest 50 ms frame near each target boundary (no mid-word cuts)."""
    n = len(audio)
    pos = 0
    hop = int(0.05 * sr)
    while pos < n:
        end = pos + int(target * sr)
        if end >= n:
            yield pos, n
            return
        lo, hi = end - int(window * sr), min(n, end + int(window * sr))
        seg = audio[lo:hi]
        frames = len(seg) // hop
        energy = (seg[:frames * hop].reshape(frames, hop) ** 2).mean(axis=1)
        cut = lo + int(np.argmin(energy)) * hop + hop // 2
        yield pos, cut
        pos = cut


class Backend:
    name = "base"
    model = ""

    def transcribe(self, audio: np.ndarray) -> dict:
        raise NotImplementedError


class SherpaOnnx(Backend):
    name = "sherpa_onnx"

    def __init__(self, model_dir: str | Path, threads: int = 4):
        import sherpa_onnx
        d = Path(model_dir).expanduser()
        if not d.exists():
            raise ToolError(f"sherpa-onnx model folder not found: {d}",
                            hint="Run `shiftws setup --models` or set transcription.sherpa_model_dir.")
        self.model = d.name

        def pick(prefix):
            c = sorted(d.glob(f"{prefix}*.int8.onnx")) or sorted(d.glob(f"{prefix}*.onnx"))
            if not c:
                raise ToolError(f"{prefix} model missing in {d}")
            return str(c[0])
        kw = dict(encoder=pick("encoder"), decoder=pick("decoder"), joiner=pick("joiner"),
                  tokens=str(d / "tokens.txt"), num_threads=threads)
        if "nemo" in d.name or "parakeet" in d.name:
            kw["model_type"] = "nemo_transducer"
        self.rec = sherpa_onnx.OfflineRecognizer.from_transducer(**kw)

    def transcribe(self, audio: np.ndarray) -> dict:
        sr = 16000
        segments, words = [], []
        for a, b in _chunks(audio, sr):
            piece = audio[a:b]
            if len(piece) < sr * 0.3 or float(np.abs(piece).max(initial=0)) < 0.003:
                continue
            s = self.rec.create_stream()
            s.accept_waveform(sr, piece)
            self.rec.decode_stream(s)
            r = s.result
            off = a / sr
            durs = list(r.durations) if getattr(r, "durations", None) else []
            cw = _merge_tokens(list(r.tokens), list(r.timestamps), durs, off, (b - a) / sr)
            words += cw
            text = r.text.strip()
            if text:
                segments += _sentences(cw, text)
        return {"segments": segments, "words": words}


def _merge_tokens(tokens, stamps, durs, off, chunk_len):
    """BPE pieces → words. A piece starting with a space (or ▁) begins a new word."""
    out = []
    for i, (tok, ts) in enumerate(zip(tokens, stamps)):
        start = off + float(ts)
        end = start + (float(durs[i]) if i < len(durs) and durs[i] else 0.08)
        new_word = tok.startswith((" ", "▁")) or not out
        piece = tok.replace("▁", " ")
        if re.fullmatch(r"\s*[.,!?;:]+", piece) and out:
            out[-1]["word"] += piece.strip()
            out[-1]["end"] = max(out[-1]["end"], end)
            continue
        if new_word:
            out.append({"word": piece.strip(), "start": round(start, 3), "end": round(end, 3), "conf": None})
        else:
            out[-1]["word"] += piece
            out[-1]["end"] = round(end, 3)
    for i in range(len(out) - 1):
        out[i]["end"] = round(min(out[i]["end"], out[i + 1]["start"]), 3) if out[i]["end"] > out[i + 1]["start"] \
            else out[i]["end"]
    return [w for w in out if w["word"]]


def _sentences(words, text):
    """Group words into caption-sized segments at sentence punctuation or long pauses."""
    segs, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        gap = words[i + 1]["start"] - w["end"] if i + 1 < len(words) else 0
        if w["word"][-1:] in ".!?" or gap > 0.7 or len(cur) >= 18:
            segs.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": " ".join(x["word"] for x in cur)})
            cur = []
    if cur:
        segs.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": " ".join(x["word"] for x in cur)})
    return segs


class WhisperCpp(Backend):
    """Reuses an installed whisper.cpp (`whisper-cli`, formerly `main`). Word-level JSON via -ojf -ml 1."""
    name = "whisper_cpp"

    def __init__(self, binary: str, model: str, threads: int = 4):
        self.bin = binary
        self.model_path = str(Path(model).expanduser())
        if not Path(self.model_path).exists():
            raise ToolError(f"whisper.cpp model not found: {self.model_path}")
        self.model = Path(self.model_path).name
        self.threads = threads

    def transcribe(self, audio: np.ndarray) -> dict:
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "a.wav"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
                w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
            base = Path(td) / "out"
            run([self.bin, "-m", self.model_path, "-f", str(wav), "-t", str(self.threads), "-ojf", "-ml", "1",
                 "-of", str(base), "-np"], timeout=3600)
            data = json.loads((base.with_suffix(".json")).read_text())
        words = []
        for item in data.get("transcription", []):
            t = item.get("text", "")
            if not t.strip():
                continue
            words.append({"word": t.strip(), "start": item["offsets"]["from"] / 1000,
                          "end": item["offsets"]["to"] / 1000, "conf": None})
        return {"segments": _sentences(words, ""), "words": words}


class FasterWhisper(Backend):
    name = "faster_whisper"

    def __init__(self, model: str = "base.en", threads: int = 4):
        from faster_whisper import WhisperModel
        self.model = model
        self.m = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=threads)

    def transcribe(self, audio: np.ndarray) -> dict:
        segs, _ = self.m.transcribe(audio, word_timestamps=True, vad_filter=True)
        segments, words = [], []
        for s in segs:
            segments.append({"start": s.start, "end": s.end, "text": s.text.strip()})
            for w in s.words or []:
                words.append({"word": w.word.strip(), "start": w.start, "end": w.end, "conf": w.probability})
        return {"segments": segments, "words": words}


_CACHE: dict = {}


def get_backend(cfg) -> Backend:
    t = cfg.get("transcription", {})
    choice = t.get("backend", "auto")
    threads = int(t.get("threads", 4))
    key = (choice, json.dumps(t, sort_keys=True))
    if key in _CACHE:
        return _CACHE[key]
    errors = []
    order = [choice] if choice != "auto" else ["whisper_cpp", "sherpa_onnx", "faster_whisper"]
    for name in order:
        try:
            if name == "whisper_cpp":
                binary = t.get("whisper_cpp_bin") or which("whisper-cli") or which("whisper-cpp")
                if not binary or not t.get("whisper_cpp_model"):
                    raise ToolError("whisper.cpp not configured")
                b = WhisperCpp(binary, t["whisper_cpp_model"], threads)
            elif name == "sherpa_onnx":
                d = t.get("sherpa_model_dir") or _default_sherpa_dir(cfg)
                if not d:
                    raise ToolError("no sherpa-onnx model installed")
                b = SherpaOnnx(d, threads)
            elif name == "faster_whisper":
                b = FasterWhisper(t.get("faster_whisper_model", "base.en"), threads)
            else:
                raise ToolError(f"unknown backend {name}")
            _CACHE[key] = b
            return b
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
    raise ToolError("No local transcription backend is available",
                    hint="Configure whisper.cpp (whisper_cpp_bin + whisper_cpp_model) or run `shiftws setup --models`.",
                    output="; ".join(errors))


def _default_sherpa_dir(cfg) -> str | None:
    for base in (Path(cfg.models_dir), Path.home() / ".local/share/shiftws/models"):
        c = sorted(base.glob("sherpa-onnx-nemo-parakeet*")) + sorted(base.glob("sherpa-onnx-*"))
        if c:
            return str(c[0])
    return None


def to_srt(segments: list[dict]) -> str:
    def ts(s):
        ms = int(round(s * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return "\n".join(f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{s['text']}\n" for i, s in enumerate(segments, 1))
