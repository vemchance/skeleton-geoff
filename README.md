# Geoff

A talking tarot skeleton for a Halloween party. A microphone hears the table, Deepgram transcribes it, Claude decides what Geoff says, and ElevenLabs speaks it through a speaker hidden behind him. Two LEDs in his eye sockets flicker with his voice. It runs on a Raspberry Pi 4.

Two modes:
- **Idle:** Geoff sits at the party, listening. He answers when someone says his name, and every few minutes makes a dry remark of his own.
- **Tarot:** Geoff follows a reading card by card, using the spreads in `prompts/tarot_spreads.md`.

The mic shuts while Geoff is thinking or talking, so he never hears himself.

## How it works

```
USB mic -> Deepgram (streaming, nova-3) -> regex routing -> Claude (streaming)
        -> sentence splitter -> gender check -> ElevenLabs (streaming PCM) -> speaker + LED eyes
```

- **Listening.** Audio streams to Deepgram over a websocket, with card names, suits and "Geoff" as keyterms. An utterance is sent on once nobody has spoken for a moment, timed from the last interim word so a long sentence isn't cut in half.
- **Routing.** Plain regexes decide where speech goes: his name, start/stop/pause phrases, and card calls ("card two, the Tower, reversed"). Everything else in a reading is ignored as table chatter.
- **Thinking.** Claude replies with either words to speak or `<quiet>`. In tarot mode it ends a summary with a hidden `<end>`. System prompts are cached to cut latency.
- **Speaking.** Claude's reply is streamed and split into sentences, so the first one is spoken while the rest are still being written. Each sentence goes through a gendered-word check (Geoff can't see anyone, so he never guesses gender); a sentence that slips is rewritten by a second Claude call or dropped.
- **Party notes.** In idle mode a background call keeps up to ten short notes on the night (running jokes, games, costumes) for his unprompted remarks to draw on.

## Running a reading

1. Say "Geoff, time for a reading", then the querent's name, their question and the spread. Until the first card he hears everything and waits for a pause, so it can all be said in one go.
2. Call each card starting with "card" and the number: "Card one, the Three of Wands, upright". Upright is assumed if you leave it out. From the first card on, he only hears card calls, anything with his name in it, and replies straight after he speaks.
3. "Card two... um..." waits a few seconds for the rest, then he asks.
4. After the summary he hears everything again, so the querent can ask about it.
5. End with "Geoff, rest in peace", "Back in your box, Geoff", or, after the summary, "Thanks". After two minutes of silence he drifts back to idle by himself.

## Voice switches

These need "Geoff" in the sentence, except where noted.

| Say | What happens |
|---|---|
| "Geoff, time for a reading" / "read my cards" | Starts a reading |
| "Geoff, no reading" / "never mind" | Before the first card: back to idle |
| "Card one, ..." (no name needed) | Calls a card |
| "Geoff, rest in peace" / "Back in your box, Geoff" | Ends a reading, any time |
| "Thanks" / "Cheers" (no name needed) | Ends a reading, after the summary only |
| "Geoff, pause" / "take five" | Pauses a reading |
| "Geoff, we're back" / "carry on" | Resumes where it left off |
| "Geoff, shush" | No unprompted remarks for 15 minutes |
| "Geoff, wake up" | Remarks back on |

Anything else said with "Geoff" in it, he answers: introductions, how he died, jokes, toasts, ghost stories, costume ratings, predictions for the night.

Once someone has started talking to him by name, they can carry on for up to three replies without it, each starting within about 10 seconds of him finishing.

## Other controls

- **Phone status page:** the address is printed at startup (e.g. `http://192.168.1.20:8080`). Shows whether he's listening, muted, thinking, speaking or paused, what he last heard and said, and the party notes. Buttons for mute, mode, Quiet (only answers to his name), mic level, clear notes, restart and stop.
- **Keys:** Page Down mutes, Page Up switches mode. A presenter clicker works; on the Pi it's read directly via evdev, so no window needs focus.

## What you need

- Raspberry Pi 4 (2GB is enough), Python 3.9+
- A USB microphone and a speaker (the Pi's headphone jack to a speaker's aux-in is the simplest)
- Optional: two 5V LEDs with built-in resistors, on GPIO17 (pin 11) and GPIO27 (pin 13), each with its other leg to a ground pin
- API keys for Claude (console.anthropic.com), Deepgram (console.deepgram.com) and ElevenLabs (a paid plan, plus a voice made with Voice Design or picked from the Voice Library)

## Setup

```
cp config.example.toml config.toml   # then add your keys and voice ID
bash setup.sh                         # on the Pi
```

Or by hand on any machine:

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Test in steps, so you know which part fails:

```
python geoff.py --typed --no-voice   # Claude only: type what he hears, read the replies
python geoff.py --typed              # adds the voice
python geoff.py                      # the full thing, with the mic
python geoff.py --list-devices       # find mic and speaker names for config.toml
```

In typed mode, `/mode` switches mode, `/mute` toggles mute, `/remark` asks for a remark and `/notes` updates the party notes.

On the Pi, run it inside `tmux` over SSH so it survives a disconnect.

## Tweaking Geoff

Everything Geoff is told lives in `prompts/`:
- `geoff_persona.md`: his character and hard rules
- `tarot_rules.md`: how readings work
- `tarot_spreads.md`: our own position meanings for each spread
- `tarot_mode.md`: voice-specific rules for readings
- `idle_mode.md`: how he behaves between readings
- `notes.md`: instructions for the background party notes

Timings for idle mode are in the `[idle]` section of `config.toml`.

### Optional guest list

If `prompts/guests.md` exists, it's added to his persona and the names are added to Deepgram's keyterms. Put names under a `## The guests` heading, one per line:

```
## The guests

- Sam (they, say Sahm): notes for Geoff
```

The pronoun and the "say" spelling in brackets are optional. This file is in `.gitignore`: it holds notes about real people, so keep it out of the repo.

## Privacy

Tell guests Geoff is listening. Transcripts go to Deepgram and Claude, and his replies to ElevenLabs, but the app saves nothing to disk.

## Licence

MIT. See `LICENSE`.
