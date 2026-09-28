"""One voice for every movement sound.

BL2's audio is Wwise, packed into `.pck` soundbanks, and there is no supported
way to hand the engine a new file. So these sounds go around the engine and
play through Windows with `winsound`. The costs: 16-bit PCM `.wav` only, no
positional audio, and the in-game volume sliders don't apply.

`winsound.PlaySound` is one voice per process - a new async sound replaces the
one playing rather than mixing with it. Everything therefore goes through
`play()`, which arbitrates:

- A sound with strictly higher priority interrupts whatever is playing.
- Anything else is dropped while the current sound is inside a short guard
  window, which stops two presses a frame apart from stuttering.
- Past the guard window the newest sound wins, so cues stay responsive.

`winsound` has no volume control, and the Windows wave-out volume is per
device, so gain is applied by scaling samples into a cached copy on first use.
The copy is also what makes zipped installs work: winsound needs a real file,
and a clip inside a `.sdkmod` isn't one.
"""

from __future__ import annotations

import array
import io
import threading
import time
import wave
import zlib
from pathlib import Path
from typing import Any, ClassVar

from unrealsdk import logging

from . import options, sounds
from .common import TAG, Warn

try:
    import winsound
except ImportError:  # pragma: no cover - non-Windows
    winsound = None  # type: ignore[assignment]

# How long a clip owns the voice against equal-or-lower priority.
GUARD_SECONDS: float = 0.12


def level_for(key: str) -> int:
    """Effective volume for one event, 0-100: master times the event's own."""
    master = options.num(options.master_volume)
    own = options.num(options.clip_volumes[key])
    return max(0, min(100, round(master * own / 100.0)))


def _render(key: str, name: str, level: int) -> tuple[str, float] | None:
    """Gain-scale a clip into the cache. Returns (path, seconds) or None."""
    data = sounds.read(key, name)
    if data is None:
        Warn.once(f"missing:{key}:{name}", f"sound not found: {key}/{name}.wav")
        return None

    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            params = reader.getparams()
            frames = reader.readframes(params.nframes)
    except (wave.Error, EOFError) as ex:
        Warn.once(
            f"bad:{key}:{name}",
            f"{key}/{name}.wav isn't a PCM wav ({ex}). Convert it to 16-bit PCM,"
            " e.g. with Audacity: File > Export > WAV (Microsoft) 16-bit PCM.",
        )
        return None
    if params.sampwidth != 2:
        Warn.once(
            f"width:{key}:{name}",
            f"{key}/{name}.wav is {params.sampwidth * 8}-bit - only 16-bit wavs are supported.",
        )
        return None

    seconds = params.nframes / float(params.framerate or 1)
    digest = f"{zlib.crc32(data):08x}"
    target = sounds.CACHE_DIR / f"{key}-{digest}@{level}.wav"
    if target.is_file():
        return str(target), seconds

    samples = array.array("h")
    samples.frombytes(frames)
    if level < 100:
        scale = level / 100.0
        for i, sample in enumerate(samples):
            samples[i] = int(sample * scale)

    try:
        sounds.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        with wave.open(str(tmp), "wb") as writer:
            writer.setparams(params)
            writer.writeframes(samples.tobytes())
        tmp.replace(target)
    except OSError as ex:
        Warn.once("cache", f"could not write the sound cache at {sounds.CACHE_DIR}: {ex!r}")
        return None
    return str(target), seconds


class Resolved:
    """(path, seconds) per (event, clip, level), worked out once."""

    cache: ClassVar[dict[tuple[str, str, int], tuple[str, float] | None]] = {}
    lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def get(cls, key: str, name: str, level: int) -> tuple[str, float] | None:
        ident = (key, name, level)
        with cls.lock:
            if ident not in cls.cache:
                cls.cache[ident] = _render(key, name, level)
            return cls.cache[ident]


class Voice:
    """Whatever is currently coming out of the speakers, and who owns it."""

    lock: ClassVar[threading.Lock] = threading.Lock()
    owner: ClassVar[str | None] = None
    priority: ClassVar[int] = 0
    started: ClassVar[float] = 0.0
    ends: ClassVar[float] = 0.0

    @classmethod
    def clear(cls) -> None:
        cls.owner = None
        cls.priority = 0
        cls.started = 0.0
        cls.ends = 0.0

    @classmethod
    def claim(cls, event: sounds.Event, now: float, seconds: float) -> bool:
        with cls.lock:
            busy = cls.owner is not None and now < cls.ends
            inside_guard = now - cls.started < GUARD_SECONDS
            if busy and inside_guard and event.priority <= cls.priority:
                return False
            cls.owner = event.key
            cls.priority = event.priority
            cls.started = now
            cls.ends = now + seconds
            return True

    @classmethod
    def release(cls, key: str | None) -> bool:
        """Give up the voice - only if `key` still holds it, or always for None."""
        with cls.lock:
            if key is not None and cls.owner != key:
                return False
            cls.clear()
            return True


def _spawn(fn: Any) -> None:
    """Run a winsound call off the game thread, always."""
    threading.Thread(target=fn, daemon=True, name="movement_overhaul_audio").start()


def play(key: str) -> None:
    """Play the clip configured for one event. Never raises."""
    try:
        _play(key)
    except Exception as ex:  # noqa: BLE001 - a sound must never break a move
        Warn.once("play", f"sound failed: {ex!r}")


def _play(key: str) -> None:
    if winsound is None or not options.sound_enabled.value:
        return
    event = sounds.BY_KEY[key]
    name = str(options.clip_pickers[key].value)
    if name == sounds.NO_SOUND:
        return
    level = level_for(key)
    if level <= 0:
        return

    resolved = Resolved.get(key, name, level)
    if resolved is None:
        return
    path, seconds = resolved

    if not Voice.claim(event, time.monotonic(), seconds):
        return
    if options.diagnostics.value:
        logging.info(f"{TAG} sound {key} -> {name} @{level}%")

    def run() -> None:
        try:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception as ex:  # noqa: BLE001
            Warn.once("winsound", f"sound playback failed: {ex!r}")

    _spawn(run)


def stop(key: str | None = None) -> None:
    """Silence the voice if `key` still owns it. None stops whatever is on."""
    if winsound is None or not Voice.release(key):
        return

    def run() -> None:
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:  # noqa: BLE001, S110
            pass

    _spawn(run)


def forget_rendered() -> None:
    """Drop resolved paths so a volume or clip change is picked up."""
    with Resolved.lock:
        Resolved.cache.clear()


def prune_cache(keep: int = 200) -> None:
    """Keep the cache folder from growing forever as volumes are tried out."""
    try:
        files = sorted(sounds.CACHE_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for stale in files[:-keep]:
        try:
            Path(stale).unlink()
        except OSError:
            pass


class SlideSound:
    """Starts the slide sound when a slide begins, and cuts it when it ends."""

    was_sliding: ClassVar[bool] = False

    @classmethod
    def tick(cls, sliding_now: bool) -> None:
        if sliding_now and not cls.was_sliding:
            play("slide")
        elif cls.was_sliding and not sliding_now:
            stop("slide")
        cls.was_sliding = sliding_now

    @classmethod
    def reset(cls) -> None:
        cls.was_sliding = False
