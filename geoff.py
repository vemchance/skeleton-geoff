#!/usr/bin/env python3
"""Geoff, the tarot-reading skeleton.

Listens through a microphone (Deepgram), thinks with Claude, and speaks with
ElevenLabs. Two modes:
  tarot - follows a reading, card by card
  idle  - listens to the room and occasionally makes a dry remark
Controls: voice phrases, two keys (a presenter clicker) and a status page for
your phone. See README.md for setup.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import os
import socket
import subprocess
import sys
import time
try:
    import tomllib  # Python 3.11+
except ImportError:  # Python 3.9/3.10 (e.g. Raspberry Pi OS Bullseye)
    import tomli as tomllib
from collections import deque
from pathlib import Path
from urllib.parse import urlencode

import httpx
import numpy as np
import sounddevice as sd
from aiohttp import web
from anthropic import AsyncAnthropic
from websockets.asyncio.client import connect as ws_connect

HERE = Path(__file__).resolve().parent
PROMPTS = HERE / "prompts"
QUIET = "<quiet>"
END = "<end>"  # Geoff adds this after a reading's summary
TTS_RATE = 24000  # ElevenLabs pcm_24000: 16-bit mono
NAME_RE = re.compile(r"\b(geoff|jeff|geoffrey|jeffrey)\b", re.I)
# "Geoff, time for a reading" and similar switch to tarot mode. Bare "read"
# isn't enough ("I can read you" shouldn't start one).
START_RE = re.compile(
    r"\b(time for (a|the|my|her|his|their|our|your) reading|"
    r"(do|start|begin|give) (a|the|my|her|his|their|our|your|us a|me a) reading|"
    r"(a|the) reading for|tarot time|"
    r"(read|do) (my|the|her|his|their|our|your|some) cards)\b", re.I)
# Said with his name before the first card: no reading after all.
CANCEL_RE = re.compile(
    r"\b(no reading|not (doing|having|wanting) a reading|"
    r"(don't|do not) want a reading|never ?mind|forget it)\b", re.I)
# Exit works any time in a reading, said with his name. Thanks works after
# the summary, with or without his name.
EXIT_RE = re.compile(r"\b(rest in peace|back in (your|the|yer) box|r\.?i\.?p)\b", re.I)
THANKS_RE = re.compile(r"\b(thank you|thanks|cheers)\b", re.I)
SHUSH_RE = re.compile(r"\b(shush|shh+|hush|quiet|shut up|pipe down)\b", re.I)
WAKE_RE = re.compile(r"\b(wake up|you can talk|speak up)\b", re.I)
# Said with his name, during a reading. Distinctive, so a querent's
# "hang on, Geoff, what does that mean?" doesn't pause anything.
PAUSE_RE = re.compile(r"\b(pause|take five|hold that thought)\b", re.I)
RESUME_RE = re.compile(r"\b(we're back|we are back|i'm back|i am back|resume|carry on)\b", re.I)
# "Card 1", "card two", "card number three"
CARD_CALL_RE = re.compile(
    r"\bcard\s+(?:number\s+)?(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.I)
# Enough of a card name to know the call is complete
CARD_NAME_RE = re.compile(
    r"\b(wands?|cups?|swords?|pentacles?|coins?|fool|magician|priestess|empress|"
    r"emperor|hierophant|lovers?|chariot|strength|hermit|wheel|fortune|justice|"
    r"hanged|death|temperance|devil|tower|star|moon|sun|judge?ment|world)\b", re.I)
# Safety net: a sentence with one of these is rewritten before it's spoken.
GENDER_RE = re.compile(
    r"\b(he|she|him|her|his|hers|himself|herself|man|men|woman|women|lad|lads|"
    r"lass|lasses|bloke|blokes|guy|guys|girl|girls|boy|boys|lady|ladies|"
    r"gentleman|gentlemen|sir|madam)\b", re.I)
REWRITE_SYSTEM = (
    "Rewrite the sentence so it has no gendered words (he, she, him, her, his, "
    "hers, himself, herself, man, woman, lad, lass, bloke, guy, girl, boy, lady, "
    "gentleman, sir, madam, or their plurals). Use the person's name, \"you\", "
    "\"they\" or \"someone\" instead, taking the name from the earlier words if "
    "needed. Change nothing else. Reply with the rewritten sentence only.")
# "Up from Leeds, I'm told." loses the "I'm told": he knows his
# friends himself, so he shouldn't announce he was told things.
_TOLD = (r"I['’]m told|I am told|I['’]m reliably informed|I gather|word has it|"
         r"so they tell me|they tell me")
_TOLD_ASIDE_RE = re.compile(
    r",\s*(?:so\s+)?(?:" + _TOLD + r"|I hear|I understand)(?=\s*[,.!?;…])", re.I)
_TOLD_START_RE = re.compile(r"^(?:so\s+)?(?:" + _TOLD + r")(?:\s+that)?,?\s+", re.I)


def drop_told(text: str) -> str:
    text = _TOLD_START_RE.sub("", _TOLD_ASIDE_RE.sub("", text)).strip()
    return text[:1].upper() + text[1:]


BOUNDARY_RE = re.compile(r"[.!?…][\"'”’)\]]*\s")
FOLLOWUP_NOTE = (f"Said just after you spoke, without your name. Answer only if "
                 f"it's clearly said to you; otherwise reply {QUIET}.")

MAJOR_ARCANA = [
    "The Fool", "The Magician", "The High Priestess", "The Empress",
    "The Emperor", "The Hierophant", "The Lovers", "The Chariot", "Strength",
    "The Hermit", "Wheel of Fortune", "Justice", "The Hanged Man", "Death",
    "Temperance", "The Devil", "The Tower", "The Star", "The Moon", "The Sun",
    "Judgement", "The World",
]
# Words Deepgram should listen out for (keep under its 500-token limit).
KEYTERMS = ["Geoff", *MAJOR_ARCANA, "Wands", "Cups", "Swords", "Pentacles",
            "Ace", "Page", "Knight", "Queen", "King", "reversed", "upright",
            "Celtic Cross", "Six-Card Cross", "Safe Passage"]


# For chat replies in idle, how long this one should be, picked at random so
# his replies vary like real conversation instead of settling into a habit.
CHAT_LENGTHS = [
    (0.6, "one sentence this time, and keep it short. Stop on the line that lands."),
    (0.3, "one sentence, or two if the second is genuinely the better joke."),
    (0.1, "your call on length this time, but no speeches."),
]


def random_length() -> str:
    r = random.random()
    for weight, text in CHAT_LENGTHS:
        if r < weight:
            return text
        r -= weight
    return CHAT_LENGTHS[0][1]


async def run_together(*coros, stop_on_first=False):
    """Run tasks side by side (Python 3.9-friendly stand-in for TaskGroup).
    If one fails, the rest are cancelled and the error is raised. With
    stop_on_first, the first task to finish for any reason stops the rest."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    pending = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                if not t.cancelled() and t.exception() is not None:
                    raise t.exception()
            if stop_on_first:
                return
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def clean(text: str) -> str:
    """Make Claude's text safe to speak: no <quiet>, no markdown symbols."""
    text = text.replace(QUIET, "").replace(END, "")
    text = re.sub(r"[#*_`]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def guest_names() -> list[str]:
    """Names from the optional prompts/guests.md ("- Name: notes" lines under
    the "##" heading), so Deepgram listens out for them. Extra spellings can
    follow in brackets after "say": "- Sam (say Sahm): ...". """
    path = PROMPTS / "guests.md"
    if not path.exists():
        return []
    names = []
    in_list = False  # names are only read below the "## The guests" heading
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_list = True
            continue
        if not in_list:
            continue
        m = re.match(r"\s*-\s*([^:(]+?)\s*(?:\(([^)]*)\))?\s*:", line)
        if m:
            names.append(m.group(1))
            for alt in re.findall(r"\bsay (\w+)", m.group(2) or ""):
                names.append(alt)
    return names


def build_prompts() -> tuple[str, str, str]:
    def read(name):
        return (PROMPTS / name).read_text(encoding="utf-8")
    persona = read("geoff_persona.md")
    if (PROMPTS / "guests.md").exists():
        persona += "\n\n" + read("guests.md")
    tarot = "\n\n".join([read("tarot_rules.md"), read("tarot_spreads.md"),
                         persona, read("tarot_mode.md")])
    idle = "\n\n".join([persona, read("idle_mode.md")])
    notes = read("notes.md")
    names = guest_names()
    if names:
        notes += ("\n\nGuests' names, so you can recognise them: "
                  + ", ".join(names) + ".")
    return tarot, idle, notes


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "localhost"


class Player:
    """Plays 16-bit mono PCM. Reopens the device if it drops (e.g. Bluetooth)."""

    def __init__(self, device):
        self.device = device or None
        self.stream = None
        self.out_rate = TTS_RATE

    def open(self):
        try:
            stream = sd.RawOutputStream(samplerate=TTS_RATE, channels=1,
                                        dtype="int16", device=self.device)
            self.out_rate = TTS_RATE
        except Exception:
            # Device won't take 24 kHz: open at its own rate and resample.
            rate = int(sd.query_devices(self.device, "output")["default_samplerate"])
            stream = sd.RawOutputStream(samplerate=rate, channels=1,
                                        dtype="int16", device=self.device)
            self.out_rate = rate
        stream.start()
        self.stream = stream

    def close(self):
        if self.stream is not None:
            try:
                self.stream.close()
            except Exception:
                pass
        self.stream = None

    def _resample(self, data: bytes) -> bytes:
        if self.out_rate == TTS_RATE:
            return data
        x = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        n_out = int(len(x) * self.out_rate / TTS_RATE)
        y = np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x)
        return y.astype(np.int16).tobytes()

    def write(self, data: bytes):
        for attempt in range(3):
            try:
                if self.stream is None:
                    self.open()
                self.stream.write(self._resample(data))
                return
            except Exception as e:
                log(f"speaker problem ({e}); reconnecting")
                self.close()
                try:  # make PortAudio rescan devices
                    sd._terminate()
                    sd._initialize()
                except Exception:
                    pass
                time.sleep(2)
        log("speaker unavailable; skipping audio")


class Eyes:
    """Two LEDs that follow the loudness of his voice: off when he's silent,
    brighter as he gets louder. If there are no LEDs (a PC, say), it does
    nothing."""

    def __init__(self, cfg: dict):
        self.leds = []
        self.level = 0.0
        self.full = cfg.get("loudness", 3000)  # voice level for full brightness
        if not cfg.get("enabled", True):
            return
        try:
            from gpiozero import PWMLED
            self.leds = [PWMLED(pin) for pin in cfg.get("pins", [17, 27])]
            log("LED eyes ready")
        except Exception as e:
            log(f"no LED eyes ({type(e).__name__}: {e})")

    def follow(self, pcm: bytes):
        """Set the brightness from one short slice of his voice."""
        if not self.leds or len(pcm) < 2:
            return
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        loud = float(np.sqrt(np.mean(x * x)))
        target = min(1.0, max(0.0, (loud - 150) / self.full))
        # Brighten at once, fade a little more slowly, so it flickers
        # with the words rather than strobing.
        self.level = target if target > self.level else 0.5 * self.level + 0.5 * target
        self._set(self.level)

    def off(self):
        self.level = 0.0
        if self.leds:
            self._set(0.0)

    def _set(self, value: float):
        try:
            for led in self.leds:
                led.value = value
        except Exception:
            pass


class Geoff:
    def __init__(self, cfg: dict, typed: bool, no_voice: bool):
        self.cfg = cfg
        self.typed = typed
        self.no_voice = no_voice
        self.tarot_system, self.idle_system, self.notes_system = build_prompts()

        controls = cfg.get("controls", {})
        self.muted = False if typed else controls.get("start_muted", True)
        self.followup_seconds = controls.get("followup_seconds", 10)
        self.followup_turns = controls.get("followup_turns", 3)
        self.mode = "idle"
        self.phase = ""  # "", "thinking" or "speaking"
        self.tail = cfg.get("audio", {}).get("tail_seconds", 0.8)

        self.reading: list[dict] = []           # tarot conversation
        self.room: deque = deque()              # [time, text, seen] in idle mode
        self.new_words = 0
        self.addressed = False
        self.recent_remarks: deque = deque(maxlen=8)
        self.last_spoke = 0.0
        self.followup_until = 0.0    # next thing said before this goes to Geoff
        self.followups_left = 0      # unnamed replies allowed before his name is needed
        self.next_check = 0.0
        self.reading_finished = False
        self.stage = "setup"         # tarot: setup, cards or after
        self.held = ""               # tarot: card call waiting for its name
        self.gathered: list[str] = []  # tarot setup: pieces of a long sentence
        self.gather_id = 0
        self.last_voice = 0.0        # when speech was last being transcribed
        self.speech_began = 0.0      # when the utterance now being heard began
        self.addressed_by_name = False  # idle: the pending reply was asked for by name
        self.shushed_until = 0.0     # idle: no unprompted remarks before this
        self.paused = False          # tarot: reader has stepped away
        self.quiet = False           # idle: phone "Quiet" button, answers only when named
        self.notes = ""              # idle: background notes on the party so far
        self.notes_buffer: list[str] = []  # lines heard since the notes were last updated
        self.notes_gen = 0           # bumped when notes are cleared

        self.pending: list[str] = []            # transcript pieces
        self.pending_since = 0.0                # when the first piece arrived
        self.last_heard = ""
        self.last_said = ""

        self.lock = asyncio.Lock()
        self.tasks: set = set()
        self.loop = None
        self.audio_q: asyncio.Queue = asyncio.Queue(maxsize=400)
        self.in_rate = 16000

        a = cfg["anthropic"]
        self.model = a.get("model", "claude-sonnet-5-5")
        # Newer models think before answering by default, which adds delay
        # and uses up max_tokens. Turn up-front thinking off where allowed.
        default_thinking = ("between_tools" if "sonnet-5-5" in self.model else
                            "disabled" if "haiku" in self.model else "")
        thinking = a.get("thinking", default_thinking)

        def extras(effort):
            d = {}
            if thinking:
                d["thinking"] = {"type": thinking}
            if effort:
                d["output_config"] = {"effort": effort}
            return d

        self.tarot_extras = extras(a.get("effort", "low"))
        self.idle_extras = extras(a.get("idle_effort", "low"))
        workspace = a.get("workspace_id", "")
        self.claude = AsyncAnthropic(
            api_key=cfg["anthropic"]["api_key"],
            # Organisation-level keys must name a workspace on each request.
            default_headers={"anthropic-workspace-id": workspace} if workspace else None)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(20, read=30))
        self.player = Player(cfg.get("audio", {}).get("output_device"))
        self.mic_stream = None
        audio = cfg.get("audio", {})
        self.mixer_card = audio.get("mixer_card", "GoMic")       # for the mic level buttons
        self.mixer_control = audio.get("mixer_control", "Mic")
        self.mic_pct = None  # mic level as a percentage, if it can be read
        self.eyes = Eyes(cfg.get("eyes", {}))

    # ---- small helpers ---------------------------------------------------

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def listening(self) -> bool:
        return not self.muted and not self.phase

    def in_followup(self, began: float = 0.0) -> bool:
        """Was this said in the window after Geoff spoke? Judged by when the
        speaker started talking, so a long reply still counts."""
        began = began or time.monotonic()
        return self.last_spoke <= began < self.followup_until

    def status(self) -> str:
        if self.phase:
            return self.phase
        if self.mode == "tarot" and self.paused:
            return "paused"
        return "muted" if self.muted else "listening"

    def toggle_mute(self):
        self.muted = not self.muted
        log("MIC MUTED" if self.muted else "MIC ON")

    def toggle_quiet(self):
        self.quiet = not self.quiet
        log("QUIET: only answers to his name" if self.quiet else "QUIET OFF: remarks back on")

    def toggle_mode(self):
        self.mode = "idle" if self.mode == "tarot" else "tarot"
        if self.mode == "tarot":
            self.reading.clear()
        self.stage = "setup"
        self.held = ""
        self.gathered.clear()
        self.gather_id += 1
        self.followup_until = 0.0
        self.paused = False
        self.pending.clear()
        log(f"MODE: {self.mode.upper()}")

    # ---- what Geoff hears ------------------------------------------------

    def on_utterance(self, text: str, began: float = 0.0):
        text = text.strip()
        if not text:
            return
        began = began or time.monotonic()
        self.last_heard = text
        log(f"heard [{self.mode}]:", text)
        named = bool(NAME_RE.search(text))
        if self.mode == "idle" and named:
            if START_RE.search(text) and not CANCEL_RE.search(text):
                self.toggle_mode()  # voice start; the same words begin the reading
            elif SHUSH_RE.search(text):
                self.shushed_until = time.monotonic() + self.cfg.get(
                    "idle", {}).get("shush_seconds", 900)
                log("SHUSHED: no unprompted remarks for a while")
            elif WAKE_RE.search(text):
                self.shushed_until = 0.0
                log("AWAKE: remarks back on")
        if self.mode == "tarot":
            self.tarot_heard(text, began)
        else:
            self.room.append([time.monotonic(), text, False])
            self.notes_buffer.append(text)
            del self.notes_buffer[:-200]
            self.new_words += len(text.split())
            if named:
                self.addressed = True
                self.addressed_by_name = True
            elif self.in_followup(began):
                self.addressed = True

    def tarot_heard(self, text: str, began: float = 0.0):
        """Stages of a reading:
        setup - before the first card, everything goes to Geoff. He waits
                for a pause in speech first, so a long sentence arrives whole.
        cards - only card calls ("card two, ..."), anything with his name in
                it, and whatever is said just after he speaks. The rest is
                table chatter and is ignored.
        after - after the summary, everything goes to him again, for
                follow-up questions, until an exit phrase or a timeout."""
        named = bool(NAME_RE.search(text))
        if ((named and EXIT_RE.search(text)) or
                (self.stage == "after" and THANKS_RE.search(text))):
            self.held = ""
            self.gathered.clear()
            self.paused = False
            self.spawn(self.tarot_turn(text, closing=True))
            return
        if named and self.stage == "setup" and CANCEL_RE.search(text):
            self.held = ""
            self.gathered.clear()
            self.gather_id += 1
            log("no reading after all")
            self.spawn(self.tarot_turn(text, closing=True, note=(
                "There's no reading after all. Acknowledge it in a few words; "
                "you go back to sitting at the party.")))
            return
        if self.paused:
            if named and RESUME_RE.search(text):
                self.paused = False
                log("RESUMED")
                self.spawn(self.tarot_turn(text, note=(
                    "The reader is back. Welcome them back in one line and say "
                    "where the reading had got to.")))
            else:
                log("paused: ignored")
            return
        if named and PAUSE_RE.search(text):
            self.held = ""
            self.gathered.clear()
            self.paused = True
            log("PAUSED")
            self.spawn(self.tarot_turn(text, note=(
                "The reader needs to step away and has paused the reading. "
                "Acknowledge it in one short line. You'll wait.")))
            return
        if self.held:  # a card call was waiting for its card name
            text = f"{self.held} {text}"
            self.held = ""
        is_card = bool(CARD_CALL_RE.search(text))
        if self.stage == "setup" and not is_card:
            self.gather(text)
            return
        if is_card and self.gathered:  # setup talk ran straight into a card
            text = " ".join(self.gathered + [text])
            self.gathered.clear()
            self.gather_id += 1
        if self.stage != "cards" and not is_card:
            self.spawn(self.tarot_turn(text))
            return
        if is_card:
            self.stage = "cards"
            if not CARD_NAME_RE.search(text):
                # "Card two... um..." - wait briefly for the rest.
                self.held = text
                self.spawn(self.release_held(text))
                return
            self.spawn(self.tarot_turn(text))
        elif named:
            self.spawn(self.tarot_turn(text))
        elif self.in_followup(began):
            # A reply without his name: a few in a row are allowed, then the
            # name is needed again, so he can't drift into table chatter.
            self.spawn(self.tarot_turn(text, note=FOLLOWUP_NOTE, followup="continue"))
        else:
            log("ignored as chatter")

    async def release_held(self, text: str):
        await asyncio.sleep(self.cfg.get("deepgram", {}).get("card_wait_seconds", 4))
        if self.held == text:  # nothing more came; let Geoff ask
            self.held = ""
            await self.tarot_turn(text)

    def gather(self, text: str):
        """Setup talk: collect pieces until the speaker pauses."""
        self.gathered.append(text)
        self.gather_id += 1
        self.last_voice = time.monotonic()
        self.spawn(self.release_gathered(self.gather_id))

    async def release_gathered(self, gid: int):
        wait = self.cfg.get("deepgram", {}).get("setup_wait_seconds", 1.5)
        while True:
            await asyncio.sleep(0.25)
            if gid != self.gather_id:
                return  # more speech came; a newer wait has taken over
            if not self.pending and time.monotonic() - self.last_voice >= wait:
                break
        text = " ".join(self.gathered)
        self.gathered.clear()
        if text:
            await self.tarot_turn(text)

    def flush_pending(self):
        text = " ".join(self.pending)
        self.pending.clear()
        began, self.speech_began = self.speech_began, 0.0
        self.on_utterance(text, began)

    def _enqueue_audio(self, chunk: bytes):
        try:
            self.audio_q.put_nowait(chunk)
        except asyncio.QueueFull:
            pass

    def start_mic(self):
        device = self.cfg.get("audio", {}).get("input_device") or None
        info = sd.query_devices(device, "input")
        self.in_rate = int(info["default_samplerate"])
        log(f"microphone: {info['name']} at {self.in_rate} Hz")

        def callback(indata, frames, t, status):
            if self.listening():
                self.loop.call_soon_threadsafe(self._enqueue_audio, bytes(indata))

        stream = sd.RawInputStream(samplerate=self.in_rate, channels=1,
                                   dtype="int16", device=device,
                                   blocksize=int(self.in_rate * 0.05),
                                   callback=callback)
        stream.start()
        return stream

    async def stt_loop(self):
        dg = self.cfg["deepgram"]
        params = [
            ("model", dg.get("model", "nova-3")),
            ("language", dg.get("language", "en")),
            ("encoding", "linear16"),
            ("sample_rate", str(self.in_rate)),
            ("channels", "1"),
            ("smart_format", "true"),
            ("interim_results", "true"),
            ("utterance_end_ms", str(dg.get("utterance_end_ms", 1500))),
            ("endpointing", "500"),
        ] + [("keyterm", term) for term in KEYTERMS + guest_names()]
        url = "wss://api.deepgram.com/v1/listen?" + urlencode(params)
        headers = {"Authorization": f"Token {dg['api_key']}"}
        while True:
            try:
                async with ws_connect(url, additional_headers=headers) as ws:
                    log("transcription connected")
                    await run_together(self._dg_send(ws), self._dg_recv(ws),
                                       stop_on_first=True)
            except Exception as e:
                log(f"transcription dropped ({e}); reconnecting")
                await asyncio.sleep(2)

    async def _dg_send(self, ws):
        was_listening = False
        last_sent = time.monotonic()
        while True:
            try:
                chunk = await asyncio.wait_for(self.audio_q.get(), 0.5)
            except asyncio.TimeoutError:
                chunk = None
            listening = self.listening()
            now = time.monotonic()
            if chunk is not None and listening:
                await ws.send(chunk)
                last_sent = now
            if was_listening and not listening:
                # Mic just closed: get the last words transcribed now.
                await ws.send(json.dumps({"type": "Finalize"}))
                while not self.audio_q.empty():
                    self.audio_q.get_nowait()
            was_listening = listening
            if now - last_sent > 4:
                await ws.send(json.dumps({"type": "KeepAlive"}))
                last_sent = now
            # Send what's been heard once the speaker has stopped: no words,
            # finished or partial, for a moment. Timed from the last word, not
            # the last finished chunk, so a long sentence isn't cut in half.
            # The cap stops constant chatter holding a "Geoff, ..." back forever.
            dg = self.cfg.get("deepgram", {})
            if self.pending and (
                    now - self.last_voice > dg.get("pause_seconds", 2.5) or
                    now - self.pending_since > dg.get("max_utterance_seconds", 12)):
                self.flush_pending()

    async def _dg_recv(self, ws):
        async for raw in ws:
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "Results":
                alts = msg.get("channel", {}).get("alternatives") or [{}]
                text = alts[0].get("transcript", "")
                if text:
                    self.last_voice = time.monotonic()  # someone is still talking
                    if not self.speech_began:
                        self.speech_began = self.last_voice
                if msg.get("is_final") and text:
                    if not self.pending:
                        self.pending_since = time.monotonic()
                    self.pending.append(text)
                    if msg.get("from_finalize"):
                        self.flush_pending()
            elif kind == "UtteranceEnd" and self.pending:
                self.flush_pending()

    async def typed_input(self):
        log("Typed mode: type what Geoff hears. /mode switches mode, /mute toggles mute, /remark asks for a remark, /notes updates the notes.")
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                return
            line = line.strip()
            if line == "/mode":
                self.toggle_mode()
            elif line == "/mute":
                self.toggle_mute()
            elif line == "/remark":
                self.spawn(self.idle_turn(False))
            elif line == "/notes":
                self.spawn(self.update_notes(show=True))
            elif line:
                self.on_utterance(line)

    # ---- what Geoff says -------------------------------------------------

    async def tarot_turn(self, text: str, closing: bool = False, note: str = "",
                         followup: str = "open"):
        async with self.lock:
            if self.mode != "tarot":
                return
            if closing and not note:
                note = ("The reader has ended the reading. Give a one-sentence "
                        "send-off in character, then you go back to resting.")
            if note:
                text += f"\n\n({note})"
            self.reading.append({"role": "user", "content": text})
            reply = await self.respond(
                self.tarot_system, self.reading, self.tarot_extras,
                max_tokens=600 if (closing or note) else 1500,
                fallback="" if closing else "The veil is thick tonight. Say that again.",
                followup=followup)
            self.reading.append({"role": "assistant", "content": reply or QUIET})
            if self.mode != "tarot":
                return
            if closing:
                self.toggle_mode()  # back to idle
            elif self.reading_finished and self.stage != "after":
                self.stage = "after"  # summary given: open for follow-up chat
                log("reading done; listening for follow-ups")

    async def idle_turn(self, named: bool):
        """named: someone is talking to him, so he gets the room's recent
        lines and replies to the new ones. Otherwise it's a remark of his
        own: a standalone line, not a reply, with the party notes as
        background (the room's lines are stale by then)."""
        async with self.lock:
            if self.mode != "idle":
                return
            items = list(self.room)
            if named:
                earlier = [t for _, t, seen in items if seen]
                new = [t for _, t, seen in items if not seen]
                if not new:
                    self.addressed = self.addressed_by_name = False
                    return
                msg = ("What the microphone picked up in the last few minutes, oldest "
                       "first. It is transcribed automatically, may be garbled, and "
                       "speakers aren't labelled. Lines starting \"You:\" are what "
                       "you said.")
                if earlier:
                    msg += ("\n\nEarlier, already heard (you've dealt with all of this; "
                            "never answer it again):\n")
                    msg += "\n".join(f"- {t}" for t in earlier)
                msg += "\n\nNew since you last listened:\n"
                msg += "\n".join(f"- {t}" for t in new)
            else:
                msg = "Time for a remark of your own."
            if self.notes and not named:
                msg += ("\n\nParty notes so far (background only; never quote them "
                        "or treat them as just said):\n" + self.notes)
            if self.recent_remarks:
                msg += "\n\nThings you've already said tonight (don't repeat them):\n"
                msg += "\n".join(f"- {r}" for r in self.recent_remarks)
            if named:
                msg += ("\n\nSomeone has just said your name, or spoken straight "
                        "after you did, so they may be talking to you. Respond only "
                        "to the new lines. If they asked you for something (an "
                        "introduction, a joke, a toast, a story), do it. If the new "
                        f"lines aren't for you, reply {QUIET}.")
                msg += (f"\n\nReply with {QUIET} or what you say. If they asked "
                        f"you for something (a story, a toast, an introduction, "
                        f"predictions), give it its usual length. Otherwise: "
                        f"{random_length()}")
            else:
                msg += ("\n\nSay one standalone line, short, that makes sense on its "
                        "own. It isn't a reply to anyone.")
                self.new_words = 0
            msg += ("\n\nReply with only the words you say aloud, never a comment "
                    "on the request or these lines.")
            for item in items:
                item[2] = True
            # A reply to someone who used his name starts a conversation; a
            # reply to an unnamed line continues it; his own remarks don't
            # start one, so he can't drift into the room's chatter.
            followup = ("open" if named and self.addressed_by_name else
                        "continue" if named else "close")
            self.addressed = False
            self.addressed_by_name = False
            reply = await self.respond(self.idle_system,
                                       [{"role": "user", "content": msg}],
                                       self.idle_extras,
                                       max_tokens=1000 if named else 600,
                                       followup=followup)
            if reply:
                self.recent_remarks.append(reply)
                self.room.append([time.monotonic(), f"You: {reply}", True])
                self.notes_buffer.append(f"You: {reply}")

    async def update_notes(self, show: bool = False):
        """Rewrite the party notes from what's been heard since last time."""
        if not self.notes_buffer:
            return
        lines, self.notes_buffer = self.notes_buffer, []
        gen = self.notes_gen
        msg = ("Current notes:\n" + (self.notes or "(none)") +
               "\n\nNew lines from the microphone, oldest first:\n" +
               "\n".join(f"- {t}" for t in lines))
        try:
            r = await self.claude.messages.create(
                model=self.model, max_tokens=800,
                system=[{"type": "text", "text": self.notes_system,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": msg}],
                extra_body=self.idle_extras)
            text = "".join(getattr(b, "text", "") for b in r.content).strip()
        except Exception as e:
            log(f"notes error: {type(e).__name__}: {e!r}")
            if gen == self.notes_gen:
                self.notes_buffer = lines + self.notes_buffer
            return
        if gen != self.notes_gen:
            return  # cleared while updating
        kept = []
        for ln in text.splitlines():
            ln = re.sub(r"^[\s\-*•–·\d.)]+", "", ln).strip()
            if ln and not ln.endswith(":") and ln.lower() != "(none)":
                kept.append("- " + ln)
        self.notes = "\n".join(kept[:10])
        log(f"notes updated ({len(kept[:10])} lines)"
            + (":\n" + (self.notes or "(none)") if show else ""))

    def clear_notes(self):
        self.notes = ""
        self.notes_buffer.clear()
        self.notes_gen += 1
        log("NOTES CLEARED")

    async def notes_loop(self):
        every = self.cfg.get("idle", {}).get("notes_seconds", 180)
        while True:
            await asyncio.sleep(every)
            await self.update_notes()

    def schedule_next_check(self, c: dict):
        self.next_check = time.monotonic() + random.uniform(
            c.get("remark_min_seconds", 120), c.get("remark_max_seconds", 900))

    async def idle_loop(self):
        c = self.cfg.get("idle", {})
        if not c.get("enabled", True):
            return
        self.schedule_next_check(c)
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            if (self.mode == "tarot" and self.stage == "after" and not self.phase
                    and not self.paused
                    and now - self.last_spoke > c.get("after_reading_seconds", 120)):
                log("nobody's talking to him; back to idle")
                self.toggle_mode()
            if self.mode != "idle" or self.muted or self.phase:
                continue
            while self.room and now - self.room[0][0] > c.get("window_seconds", 180):
                self.room.popleft()
            if not self.room:
                self.addressed = self.addressed_by_name = False
            since_spoke = now - self.last_spoke
            if self.addressed and since_spoke >= c.get("reply_when_named_after_seconds", 1):
                await self.idle_turn(True)
                continue
            if now < self.next_check:
                continue
            if since_spoke < c.get("remark_after_spoke_seconds", 60):
                continue  # he's just been talking: wait, then remark
            self.schedule_next_check(c)
            if (now < self.shushed_until or self.quiet
                    or self.new_words < c.get("min_new_words", 12)):
                continue  # shushed, Quiet on, or the room is near silent
            await self.idle_turn(False)

    async def respond(self, system: str, messages: list, extras: dict,
                      max_tokens: int, fallback: str = "",
                      followup: str = "open") -> str:
        """Stream Claude's reply, speaking each sentence as it arrives.
        Returns the spoken text, or "" if Geoff stayed quiet. Afterwards,
        the next thing said may count as a reply to him (see followup:
        "open" starts a conversation, "continue" carries one on, "close"
        ends it, for his own unprompted remarks)."""
        self.phase = "thinking"
        self.last_said = ""
        self.reading_finished = False
        sentences: asyncio.Queue = asyncio.Queue()
        speaker = self.spawn(self.speak_from(sentences))
        full, buf = "", ""
        decided = quiet = emitted = False
        try:
            # The instructions are the same every time, so cache them:
            # Claude reads a cached prompt faster.
            async with self.claude.messages.stream(
                    model=self.model, max_tokens=max_tokens,
                    system=[{"type": "text", "text": system,
                             "cache_control": {"type": "ephemeral"}}],
                    messages=messages, extra_body=extras) as stream:
                async for piece in stream.text_stream:
                    full += piece
                    if not decided:
                        s = full.strip()
                        if not s or (len(s) < len(QUIET) and QUIET.startswith(s)):
                            continue  # might still turn out to be <quiet>
                        decided = True
                        quiet = s.startswith(QUIET)
                        buf = "" if quiet else full
                    elif not quiet:
                        buf += piece
                    if not quiet:
                        buf, emitted = self.emit_sentences(buf, sentences, emitted)
                try:
                    final = await stream.get_final_message()
                    if final.stop_reason == "max_tokens":
                        log("warning: reply cut off by max_tokens")
                except Exception:
                    pass
            if not decided:
                s = full.strip()
                quiet = not s or QUIET.startswith(s)
                buf = "" if quiet else full
            if not quiet and clean(buf):
                sentences.put_nowait(clean(buf))
        except Exception as e:
            log(f"Claude error: {type(e).__name__}: {e!r}")
            quiet = not emitted  # if he'd already started talking, it counts
            if fallback and not emitted:
                sentences.put_nowait(fallback)
        finally:
            sentences.put_nowait(None)
            await speaker
            await asyncio.sleep(self.tail)  # let the speaker finish before listening
            self.phase = ""
        if quiet:
            log("geoff stays quiet")
            if followup == "continue":
                self.followup_until = 0.0  # it wasn't for him: conversation over
            return ""
        self.reading_finished = END in full
        self.last_spoke = time.monotonic()
        if followup == "open":
            self.followups_left = self.followup_turns
        elif followup == "continue":
            self.followups_left -= 1
        else:
            self.followups_left = 0
        self.followup_until = (self.last_spoke + self.followup_seconds
                               if self.followups_left > 0 else 0.0)
        return self.last_said or clean(full)

    def emit_sentences(self, buf: str, q: asyncio.Queue, emitted: bool):
        """Send complete sentences to the voice. The first goes out at once;
        later ones are grouped so the voice doesn't sound choppy."""
        ends = [m.end() for m in BOUNDARY_RE.finditer(buf)]
        if not ends:
            return buf, emitted
        cut = ends[-1]
        if emitted and cut < 120:
            return buf, emitted
        text = clean(buf[:cut])
        if text:
            q.put_nowait(text)
        return buf[cut:], True

    async def degender(self, text: str) -> str:
        """Rewrite a sentence that slipped a gendered pronoun in. If that
        fails, the sentence is dropped rather than spoken as it was."""
        log("gendered word, rewriting:", text)
        msg = f"Earlier words: {self.last_said or '(none)'}\n\nSentence: {text}"
        try:
            r = await asyncio.wait_for(self.claude.messages.create(
                model=self.model, max_tokens=300, system=REWRITE_SYSTEM,
                messages=[{"role": "user", "content": msg}],
                extra_body=self.idle_extras), 6)
            new = clean("".join(getattr(b, "text", "") for b in r.content))
        except Exception as e:
            log(f"rewrite failed ({type(e).__name__}); sentence dropped")
            return ""
        if not new or GENDER_RE.search(new):
            log("rewrite still gendered; sentence dropped")
            return ""
        return new

    async def speak_from(self, q: asyncio.Queue):
        while (text := await q.get()) is not None:
            text = drop_told(text)
            if not text:
                continue
            if GENDER_RE.search(text):
                text = await self.degender(text)
                if not text:
                    continue
            self.phase = "speaking"
            self.last_said = (self.last_said + " " + text).strip()
            log("geoff:", text)
            try:
                await self.tts(text)
            except Exception as e:
                log(f"voice error: {e}")
            finally:
                self.eyes.off()

    def play(self, data: bytes):
        """Play audio in short slices, setting the eyes for each slice just
        before it goes to the speaker, so they move with the words."""
        step = TTS_RATE // 25 * 2  # 40 ms of 16-bit audio
        for i in range(0, len(data), step):
            part = data[i:i + step]
            self.eyes.follow(part)
            self.player.write(part)

    async def tts(self, text: str):
        if self.no_voice:
            return
        el = self.cfg["elevenlabs"]
        body = {"text": text, "model_id": el.get("model_id", "eleven_flash_v2_5")}
        if el.get("voice_settings"):
            body["voice_settings"] = el["voice_settings"]
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{el['voice_id']}/stream"
        async with self.http.stream("POST", url,
                                    params={"output_format": f"pcm_{TTS_RATE}"},
                                    headers={"xi-api-key": el["api_key"]},
                                    json=body) as r:
            if r.status_code != 200:
                detail = (await r.aread())[:300]
                log(f"ElevenLabs error {r.status_code}: {detail!r}")
                return
            rest = b""
            async for chunk in r.aiter_bytes():
                data = rest + chunk
                n = len(data) - len(data) % 2  # whole 16-bit samples only
                rest = data[n:]
                if n:
                    await asyncio.to_thread(self.play, data[:n])

    # ---- controls --------------------------------------------------------

    async def key_loop(self):
        controls = self.cfg.get("controls", {})
        mute_key = controls.get("mute_key", "page_down")
        mode_key = controls.get("mode_key", "page_up")
        if sys.platform.startswith("linux"):
            await self._keys_evdev(mute_key, mode_key)
        else:
            self._keys_pynput(mute_key, mode_key)

    def _keys_pynput(self, mute_key: str, mode_key: str):
        try:
            from pynput import keyboard
        except ImportError:
            log("pynput not installed; use the status page to control Geoff")
            return

        def resolve(name):
            return getattr(keyboard.Key, name, None) or keyboard.KeyCode.from_char(name)

        mute, mode = resolve(mute_key), resolve(mode_key)

        def on_press(key):
            if key == mute:
                self.loop.call_soon_threadsafe(self.toggle_mute)
            elif key == mode:
                self.loop.call_soon_threadsafe(self.toggle_mode)

        keyboard.Listener(on_press=on_press, daemon=True).start()
        log(f"keys: {mute_key} = mute, {mode_key} = mode")

    async def _keys_evdev(self, mute_key: str, mode_key: str):
        # Reads the clicker directly, so it works on a Pi with no screen.
        try:
            import evdev
            from evdev import ecodes
        except ImportError:
            log("evdev not installed; use the status page to control Geoff")
            return

        def code(name):
            return ecodes.ecodes["KEY_" + name.upper().replace("_", "")]

        actions = {code(mute_key): self.toggle_mute, code(mode_key): self.toggle_mode}
        log(f"keys: {mute_key} = mute, {mode_key} = mode")
        watching: set = set()

        async def read(dev):
            try:
                async for ev in dev.async_read_loop():
                    if ev.type == ecodes.EV_KEY and ev.value == 1 and ev.code in actions:
                        actions[ev.code]()
            except OSError:
                log(f"lost key device: {dev.name}")
            finally:
                watching.discard(dev.path)

        warned = False
        while True:  # rescan, so a clicker plugged in later is picked up
            paths = evdev.list_devices()
            if not paths and not warned:
                log("no input devices visible; is this user in the 'input' group?")
                warned = True
            for path in paths:
                if path in watching:
                    continue
                try:
                    dev = evdev.InputDevice(path)
                except OSError:
                    continue
                if set(dev.capabilities().get(ecodes.EV_KEY, [])) & actions.keys():
                    watching.add(path)
                    log(f"watching keys on: {dev.name}")
                    self.spawn(read(dev))
            await asyncio.sleep(10)

    async def web_server(self):
        async def page(request):
            return web.Response(text=STATUS_HTML, content_type="text/html")

        async def state(request):
            return web.json_response({
                "status": self.status(), "mode": self.mode, "muted": self.muted,
                "quiet": self.quiet, "notes": self.notes, "heard": self.last_heard,
                "said": self.last_said, "mic_level": self.mic_pct})

        async def mute(request):
            self.toggle_mute()
            return await state(request)

        async def quiet(request):
            self.toggle_quiet()
            return await state(request)

        async def clear_notes(request):
            self.clear_notes()
            return await state(request)

        async def mode(request):
            self.toggle_mode()
            return await state(request)

        async def mic_up(request):
            await asyncio.to_thread(self.change_mic_level, 5)
            return await state(request)

        async def mic_down(request):
            await asyncio.to_thread(self.change_mic_level, -5)
            return await state(request)

        async def restart(request):
            self.loop.call_later(0.5, self.shut_down, True)  # after the reply is sent
            return await state(request)

        async def stop(request):
            self.loop.call_later(0.5, self.shut_down, False)
            return await state(request)

        app = web.Application()
        app.add_routes([web.get("/", page), web.get("/state", state),
                        web.post("/mute", mute), web.post("/mode", mode),
                        web.post("/quiet", quiet),
                        web.post("/clear_notes", clear_notes),
                        web.post("/mic_up", mic_up), web.post("/mic_down", mic_down),
                        web.post("/restart", restart), web.post("/stop", stop)])
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        port = self.cfg.get("web", {}).get("port", 8080)
        await web.TCPSite(runner, "0.0.0.0", port).start()
        log(f"status page: http://{local_ip()}:{port}")
        await asyncio.Event().wait()

    # ---- mic level and restart, for the phone page -------------------------

    def read_mic_level(self):
        try:
            out = subprocess.run(["amixer", "-c", self.mixer_card, "sget",
                                  self.mixer_control], capture_output=True,
                                 text=True, timeout=3).stdout
            m = re.search(r"\[(\d+)%\]", out)
            self.mic_pct = int(m.group(1)) if m else None
        except Exception as e:
            log(f"can't read mic level ({e})")
            self.mic_pct = None

    def change_mic_level(self, step: int):
        sign = "+" if step > 0 else "-"
        try:
            subprocess.run(["amixer", "-c", self.mixer_card, "sset",
                            self.mixer_control, f"{abs(step)}%{sign}"],
                           capture_output=True, timeout=3)
            # Save it so it survives a reboot. -n: never wait for a password.
            saved = subprocess.run(["sudo", "-n", "alsactl", "store"],
                                   capture_output=True, timeout=5).returncode == 0
        except Exception as e:
            log(f"can't change mic level ({e})")
            saved = False
        self.read_mic_level()
        log(f"mic level now {self.mic_pct}%" + ("" if saved else " (not saved for reboot)"))

    def shut_down(self, restart: bool):
        log("RESTARTING" if restart else "STOPPING (from the phone page)")
        try:
            if self.mic_stream is not None:
                self.mic_stream.close()
        except Exception:
            pass
        self.eyes.off()
        self.player.close()
        if restart:
            # Start this same program afresh, with the same options.
            os.execv(sys.executable, [sys.executable] + sys.argv)
        os._exit(0)

    # ---- start -----------------------------------------------------------

    async def run(self):
        self.loop = asyncio.get_running_loop()
        if not self.no_voice:
            await asyncio.to_thread(self.player.open)
        mic = None
        if not self.typed:
            mic = self.mic_stream = self.start_mic()
            await asyncio.to_thread(self.read_mic_level)
        log(f"Geoff is awake. Mode: {self.mode}. Mic {'muted' if self.muted else 'on'}.")
        try:
            await run_together(
                self.web_server(), self.idle_loop(), self.notes_loop(), self.key_loop(),
                self.typed_input() if self.typed else self.stt_loop())
        finally:
            if mic is not None:
                mic.close()
            self.eyes.off()
            self.player.close()


STATUS_HTML = """<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Geoff</title>
<style>
  body { margin: 0; padding: 16px; background: #111; color: #eee;
         font-family: system-ui, sans-serif; }
  #status { font-size: 2.2rem; font-weight: 700; text-align: center;
            padding: 24px 8px; border-radius: 14px; margin-bottom: 12px; }
  .listening { background: #1f6f3a; } .muted { background: #555; }
  .thinking { background: #8a6d1d; } .speaking { background: #6b2a8a; }
  .paused { background: #1d4f8a; }
  #mode { text-align: center; font-size: 1.3rem; margin-bottom: 16px; }
  button { width: 100%; font-size: 1.4rem; padding: 18px; margin-bottom: 12px;
           border: 0; border-radius: 12px; background: #333; color: #eee; }
  .label { color: #999; font-size: .85rem; margin-top: 14px; }
  .text { font-size: 1rem; min-height: 1.2em; }
  .row { display: flex; gap: 12px; align-items: center; }
  .row button { width: 30%; }
  #miclevel { flex: 1; text-align: center; font-size: 1.3rem; }
  .danger { background: #6b1d1d; }
</style></head><body>
<div id="status">...</div>
<div id="mode"></div>
<button onclick="act('mute', this)">Mute / unmute mic</button>
<button onclick="act('quiet', this)" id="quiet">Quiet: off</button>
<button onclick="act('mode', this)">Switch mode</button>
<div class="row">
  <button onclick="act('mic_down', this)">&minus;</button>
  <div id="miclevel">Mic level</div>
  <button onclick="act('mic_up', this)">+</button>
</div>
<div class="label">Last heard</div><div class="text" id="heard"></div>
<div class="label">Last said</div><div class="text" id="said"></div>
<div class="label">Party notes</div><div class="text" id="notes" style="white-space: pre-wrap"></div>
<button onclick="act('clear_notes', this)" style="margin-top: 12px">Clear notes</button>
<button onclick="if (confirm('Restart Geoff? Takes about 15 seconds.')) act('restart', this)"
        style="margin-top: 28px">Restart Geoff</button>
<button class="danger" onclick="if (confirm('Stop Geoff? You will need SSH to start him again.')) act('stop', this)">Stop Geoff</button>
<script>
function show(s) {
  const el = document.getElementById('status');
  el.textContent = s.status.toUpperCase();
  el.className = s.status;
  document.getElementById('mode').textContent =
    s.mode === 'tarot' ? 'Tarot mode' : 'Idle mode';
  document.getElementById('quiet').textContent = s.quiet
    ? 'Quiet: ON (only answers to his name)' : 'Quiet: off';
  document.getElementById('heard').textContent = s.heard;
  document.getElementById('said').textContent = s.said;
  document.getElementById('notes').textContent = s.notes || '(none yet)';
  document.getElementById('miclevel').textContent =
    s.mic_level == null ? 'Mic level: ?' : 'Mic level: ' + s.mic_level + '%';
}
async function poll() {
  try { show(await (await fetch('/state')).json()); }
  catch (e) {  // Geoff isn't answering: restarting, stopped or off
    const el = document.getElementById('status');
    el.textContent = 'NOT RUNNING';
    el.className = 'muted';
  }
}
async function act(what, btn) {
  const label = btn.textContent;
  btn.textContent = 'Sending...';  // the Wi-Fi can be slow; shows the press landed
  try { show(await (await fetch('/' + what, {method: 'POST'})).json()); }
  catch (e) { btn.textContent = label; }
  if (what !== 'quiet') btn.textContent = label;
}
poll(); setInterval(poll, 1000);
</script></body></html>"""


def check_config(cfg: dict, typed: bool, no_voice: bool):
    needed = [("anthropic", "api_key")]
    if not typed:
        needed.append(("deepgram", "api_key"))
    if not no_voice:
        needed += [("elevenlabs", "api_key"), ("elevenlabs", "voice_id")]
    missing = [f"[{s}] {k}" for s, k in needed if not cfg.get(s, {}).get(k)]
    if missing:
        sys.exit("Missing in config.toml: " + ", ".join(missing))


async def start(cfg: dict, typed: bool, no_voice: bool):
    # Build Geoff inside the running event loop (needed on Python 3.9).
    await Geoff(cfg, typed, no_voice).run()


def main():
    ap = argparse.ArgumentParser(description="Geoff, the tarot-reading skeleton")
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--typed", action="store_true",
                    help="type what Geoff hears instead of using the microphone")
    ap.add_argument("--no-voice", action="store_true",
                    help="print Geoff's replies instead of speaking them")
    ap.add_argument("--list-devices", action="store_true",
                    help="list audio devices and exit")
    args = ap.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return
    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)
    check_config(cfg, args.typed, args.no_voice)
    try:
        asyncio.run(start(cfg, args.typed, args.no_voice))
    except KeyboardInterrupt:
        print("\nGeoff goes back to sleep.")


if __name__ == "__main__":
    main()
