"""Synthesise the mod's bundled movement sounds from scratch.

Everything here is generated from noise and oscillators with fixed random
seeds, so the output is original, reproducible, and free to distribute. Run:

    python tools/make_sounds.py

and the .wav files land in src/movement_overhaul/sounds/<event>/. Every clip
is 44.1 kHz 16-bit mono PCM (all winsound can play) and peak-normalised, so
the in-mod volume sliders are the only level control.

To tweak a sound, edit its recipe below and re-run. Each recipe is a function
returning a list of float samples in -1..1.
"""

from __future__ import annotations

import array
import math
import random
import wave
from pathlib import Path

RATE = 44100
OUT = Path(__file__).resolve().parent.parent / "src" / "movement_overhaul" / "sounds"


# --- building blocks ------------------------------------------------------------


def seconds(n: float) -> int:
    return int(n * RATE)


def white(n: int, rng: random.Random) -> list[float]:
    return [rng.uniform(-1.0, 1.0) for _ in range(n)]


def pink(n: int, rng: random.Random) -> list[float]:
    """Paul Kellet's economy pink filter - softer, more 'air' than white noise."""
    b0 = b1 = b2 = 0.0
    out = []
    for _ in range(n):
        w = rng.uniform(-1.0, 1.0)
        b0 = 0.99765 * b0 + w * 0.0990460
        b1 = 0.96300 * b1 + w * 0.2965164
        b2 = 0.57000 * b2 + w * 1.0526913
        out.append((b0 + b1 + b2 + w * 0.1848) * 0.2)
    return out


def svf(signal: list[float], cutoff, q: float, mode: str = "band") -> list[float]:
    """Chamberlin state-variable filter. `cutoff` is Hz, or a function of time t (s)."""
    low = band = 0.0
    damp = 1.0 / max(q, 0.5)
    out = []
    for i, x in enumerate(signal):
        fc = cutoff(i / RATE) if callable(cutoff) else cutoff
        f = 2.0 * math.sin(math.pi * min(fc, RATE / 6.0) / RATE)
        low += f * band
        high = x - low - damp * band
        band += f * high
        out.append(low if mode == "low" else high if mode == "high" else band)
    return out


def envelope(n: int, attack: float, decay: float, curve: float = 2.0) -> list[float]:
    """Linear attack to 1, then a power-curve decay to 0 over `decay` seconds."""
    a = max(1, seconds(attack))
    out = []
    for i in range(n):
        if i < a:
            out.append(i / a)
        else:
            t = (i - a) / max(1, seconds(decay))
            out.append(max(0.0, 1.0 - t) ** curve)
    return out


def mul(a: list[float], b: list[float]) -> list[float]:
    return [x * y for x, y in zip(a, b)]


def mix(*layers: tuple[list[float], float]) -> list[float]:
    n = max(len(layer) for layer, _ in layers)
    out = [0.0] * n
    for layer, gain in layers:
        for i, x in enumerate(layer):
            out[i] += x * gain
    return out


def thump(n: int, freq: float, drop: float, length: float) -> list[float]:
    """A soft low body hit: a sine that falls in pitch and dies quickly."""
    out = []
    phase = 0.0
    for i in range(n):
        t = i / RATE
        f = freq * (1.0 - drop * min(t / length, 1.0))
        phase += 2 * math.pi * f / RATE
        out.append(math.sin(phase) * max(0.0, 1.0 - t / length) ** 3)
    return out


def crackle(n: int, rng: random.Random, density: float, width: int = 40) -> list[float]:
    """Sparse little impulses - grit under a slide."""
    out = [0.0] * n
    i = 0
    while i < n:
        i += int(rng.expovariate(density) * RATE) + 1
        amp = rng.uniform(0.3, 1.0) * rng.choice((-1, 1))
        for k in range(width):
            if i + k < n:
                out[i + k] += amp * math.exp(-k / (width / 5)) * rng.uniform(0.5, 1.0)
    return out


def fade_edges(signal: list[float], ms: float = 4.0) -> list[float]:
    """Kill clicks at the very start and end."""
    k = max(1, int(RATE * ms / 1000))
    out = list(signal)
    for i in range(min(k, len(out))):
        out[i] *= i / k
        out[-1 - i] *= i / k
    return out


def normalise(signal: list[float], peak: float = 0.89) -> list[float]:
    top = max((abs(x) for x in signal), default=0.0) or 1.0
    return [x * peak / top for x in signal]


def brown(n: int, rng: random.Random) -> list[float]:
    """Brownian noise - a deep rumble, most of its energy low."""
    out = []
    last = 0.0
    for _ in range(n):
        last = (last + 0.02 * rng.uniform(-1.0, 1.0)) / 1.02
        out.append(last * 3.5)
    return out


def tone(n: int, freq, shape: str = "sine", harmonics: tuple[float, ...] = ()) -> list[float]:
    """An oscillator. `freq` is Hz or a function of time t (s), so pitch can move.

    `harmonics` adds overtones at 2x, 3x... with the given amplitudes.
    """
    out = []
    phase = 0.0
    for i in range(n):
        f = freq(i / RATE) if callable(freq) else freq
        phase += 2 * math.pi * f / RATE
        if shape == "saw":
            x = ((phase / math.pi) % 2.0) - 1.0
        elif shape == "tri":
            x = 2.0 * abs(((phase / math.pi) % 2.0) - 1.0) - 1.0
        else:
            x = math.sin(phase)
        for k, amp in enumerate(harmonics, start=2):
            x += amp * math.sin(phase * k)
        out.append(x)
    return out


def curve(n: int, points: list[tuple[float, float]]) -> list[float]:
    """A piecewise-linear envelope through (seconds, level) points."""
    out = []
    j = 0
    for i in range(n):
        t = i / RATE
        while j < len(points) - 2 and t > points[j + 1][0]:
            j += 1
        (t0, v0), (t1, v1) = points[j], points[min(j + 1, len(points) - 1)]
        if t <= t0:
            out.append(v0)
        elif t >= t1:
            out.append(v1)
        else:
            out.append(v0 + (v1 - v0) * (t - t0) / (t1 - t0))
    return out


def resonate(signal: list[float], modes: list[tuple[float, float]], q: float) -> list[float]:
    """Ring a signal through a bank of narrow resonances - metal, plates, pipes."""
    return mix(*((svf(signal, freq, q=q), gain) for freq, gain in modes))


def soft_clip(signal: list[float], drive: float) -> list[float]:
    """Gentle saturation, for grit and warmth."""
    return [math.tanh(x * drive) for x in signal]


# --- recipes ------------------------------------------------------------------------


def slide_scrape() -> list[float]:
    """Boots skidding over grit: band-limited noise with crackle on top."""
    rng = random.Random(11)
    n = seconds(2.6)
    bed = svf(pink(n, rng), lambda t: 1800 + 500 * math.sin(t * 7.0), q=1.4)
    grit = svf(crackle(n, rng, density=90), 3500, q=1.0, mode="high")
    body = svf(white(n, rng), 450, q=0.8, mode="low")
    sig = mix((bed, 1.0), (grit, 0.35), (body, 0.5))
    return fade_edges(normalise(mul(sig, envelope(n, 0.03, 2.55, curve=1.3))))


def slide_soft() -> list[float]:
    """A softer, duller skid - fabric on smooth floor."""
    rng = random.Random(12)
    n = seconds(2.2)
    bed = svf(pink(n, rng), lambda t: 900 + 250 * math.sin(t * 5.0), q=1.1)
    sig = mix((bed, 1.0), (svf(white(n, rng), 300, q=0.7, mode="low"), 0.4))
    return fade_edges(normalise(mul(sig, envelope(n, 0.05, 2.15, curve=1.6))))


def slide_gravel() -> list[float]:
    """Loose gravel: mostly crackle, thin noise bed."""
    rng = random.Random(13)
    n = seconds(2.4)
    grit = svf(crackle(n, rng, density=220, width=60), 2200, q=1.2)
    bed = svf(white(n, rng), 2600, q=2.0)
    sig = mix((grit, 1.0), (bed, 0.25))
    return fade_edges(normalise(mul(sig, envelope(n, 0.02, 2.35, curve=1.5))))


def dash_whoosh() -> list[float]:
    """Air rushing past: a resonant sweep up, then back down as it fades."""
    rng = random.Random(21)
    n = seconds(0.55)

    def cutoff(t: float) -> float:
        rise = min(t / 0.18, 1.0)
        fall = max(0.0, (t - 0.18) / 0.37)
        return 400 + 2600 * rise - 1800 * fall

    sig = svf(pink(n, rng), cutoff, q=3.0)
    return fade_edges(normalise(mul(sig, envelope(n, 0.12, 0.43, curve=2.2))))


def dash_low() -> list[float]:
    """A heavier, lower rush."""
    rng = random.Random(22)
    n = seconds(0.6)
    sig = svf(pink(n, rng), lambda t: 250 + 1300 * math.sin(math.pi * min(t / 0.6, 1.0)), q=2.5)
    return fade_edges(normalise(mul(sig, envelope(n, 0.1, 0.5, curve=2.0))))


def dash_burst() -> list[float]:
    """A short sharp puff."""
    rng = random.Random(23)
    n = seconds(0.3)
    sig = svf(white(n, rng), lambda t: 3500 - 2500 * min(t / 0.3, 1.0), q=1.5)
    return fade_edges(normalise(mul(sig, envelope(n, 0.01, 0.29, curve=3.0))))


def air_jump_lift() -> list[float]:
    """A soft push-off thump under a quick upward whoosh."""
    rng = random.Random(31)
    n = seconds(0.5)
    air = svf(pink(n, rng), lambda t: 600 + 3000 * min(t / 0.25, 1.0), q=2.2)
    air = mul(air, envelope(n, 0.05, 0.45, curve=2.4))
    body = thump(n, 110, 0.5, 0.18)
    return fade_edges(normalise(mix((air, 1.0), (body, 0.9))))


def air_jump_hop() -> list[float]:
    """Lighter and shorter - a springy hop."""
    rng = random.Random(32)
    n = seconds(0.32)
    air = svf(white(n, rng), lambda t: 1200 + 2400 * min(t / 0.15, 1.0), q=2.8)
    air = mul(air, envelope(n, 0.02, 0.3, curve=2.8))
    body = thump(n, 160, 0.4, 0.1)
    return fade_edges(normalise(mix((air, 0.8), (body, 1.0))))


def mantle_grab() -> list[float]:
    """Hands meeting a ledge: a dull hit and a short cloth rustle."""
    rng = random.Random(41)
    n = seconds(0.45)
    hit = thump(n, 90, 0.3, 0.12)
    rustle = svf(pink(n, rng), 1400, q=1.2)
    rustle = mul(rustle, envelope(n, 0.04, 0.35, curve=2.0))
    return fade_edges(normalise(mix((hit, 1.0), (rustle, 0.6))))


def mantle_vault() -> list[float]:
    """A grab, then the body swinging over."""
    rng = random.Random(42)
    n = seconds(0.6)
    hit = thump(n, 85, 0.3, 0.1)
    swing = svf(pink(n, rng), lambda t: 500 + 1800 * math.sin(math.pi * min(t / 0.6, 1.0)), q=2.0)
    swing = mul(swing, [0.0] * seconds(0.08) + envelope(n - seconds(0.08), 0.12, 0.4, curve=2.0))
    return fade_edges(normalise(mix((hit, 1.0), (swing, 0.7))))


# --- round two ------------------------------------------------------------------


def stick_slip(n: int, rng: random.Random, rate: float, low: float) -> list[float]:
    """Irregular grab-and-release of a sole on rough ground, as a gain curve.

    A new random level every 1/rate seconds, smoothed so it breathes rather
    than steps. This is what makes a scrape sound *rough* instead of hissy.
    """
    step = max(1, int(RATE / rate))
    target = rng.uniform(low, 1.0)
    level = target
    out = []
    for i in range(n):
        if i % step == 0:
            target = rng.uniform(low, 1.0)
        level += (target - level) * 0.004
        out.append(level)
    return out


def slide_dirt() -> list[float]:
    """Dirt Skid - a long gritty skid through Pandora dust and loose stones.

    Three layers: a friction bed that grabs and releases, dense small gravel
    ticking underneath, and a thin dust hiss on top, over a low rumble of body
    weight. Saturated slightly so it sits rough rather than clean.
    """
    rng = random.Random(51)
    n = seconds(2.5)
    friction = mul(svf(pink(n, rng), lambda t: 1100 + 250 * math.sin(t * 3.1), q=0.9), stick_slip(n, rng, 28, 0.35))
    gravel = svf(crackle(n, rng, density=380, width=30), 2400, q=0.9)
    pebbles = svf(crackle(n, rng, density=18, width=160), 900, q=1.5)
    dust = svf(white(n, rng), 5500, q=0.7, mode="high")
    rumble = svf(brown(n, rng), 180, q=0.7, mode="low")
    sig = mix((friction, 1.0), (gravel, 0.45), (pebbles, 0.35), (dust, 0.12), (rumble, 0.6))
    sig = soft_clip(normalise(sig, 1.0), 1.6)
    env = curve(n, [(0, 0), (0.02, 1.0), (1.8, 0.75), (2.5, 0)])
    return fade_edges(normalise(mul(sig, env)))


def slide_metal() -> list[float]:
    """Metal Skid - boots grinding across Hyperion deck plating.

    Friction noise rung through a handful of inharmonic plate resonances (a
    metal sheet's overtones don't line up like a string's), with a faint
    squeal riding the brightest one.
    """
    rng = random.Random(52)
    n = seconds(2.3)
    drive = mul(svf(white(n, rng), 3000, q=0.6), stick_slip(n, rng, 40, 0.3))
    plate = resonate(drive, [(870, 0.8), (1460, 1.0), (2310, 0.8), (3520, 0.5), (5100, 0.3)], q=30)
    squeal = mul(tone(n, lambda t: 2310 + 18 * math.sin(2 * math.pi * 6 * t)), curve(n, [(0, 0), (0.3, 0.12), (1.6, 0.08), (2.3, 0)]))
    grit = svf(crackle(n, rng, density=120, width=25), 3000, q=0.8)
    sig = mix((plate, 1.0), (squeal, 0.35), (grit, 0.25), (svf(brown(n, rng), 150, q=0.7, mode="low"), 0.4))
    env = curve(n, [(0, 0), (0.015, 1.0), (1.6, 0.7), (2.3, 0)])
    return fade_edges(normalise(mul(sig, env)))


def slide_energy() -> list[float]:
    """Energy Skid - a shield humming and spitting sparks as it grinds the floor.

    A low buzzing hum that sags in pitch as the slide slows, electric crackle
    in bursts, and a band of noise sliding down with it.
    """
    rng = random.Random(53)
    n = seconds(2.3)
    pitch = lambda t: 62 - 14 * min(t / 2.3, 1.0)  # noqa: E731
    hum = svf(tone(n, pitch, "saw"), 520, q=1.2, mode="low")
    hum = mul(hum, [0.8 + 0.2 * math.sin(2 * math.pi * 7 * i / RATE) for i in range(n)])
    gate = [1.0 if math.sin(2 * math.pi * 3.3 * i / RATE + 1.3 * math.sin(i / RATE * 11)) > 0.2 else 0.25 for i in range(n)]
    sparks = mul(svf(crackle(n, rng, density=140, width=12), 4200, q=0.8, mode="high"), gate)
    band = svf(pink(n, rng), lambda t: 1500 - 700 * min(t / 2.3, 1.0), q=2.5)
    sig = mix((hum, 1.0), (sparks, 0.55), (band, 0.5))
    env = curve(n, [(0, 0), (0.03, 1.0), (1.7, 0.8), (2.3, 0)])
    return fade_edges(normalise(mul(sig, env)))


def dash_big() -> list[float]:
    """Big Whoosh - the long, deep WHOOOSH of something big going past fast.

    Two resonant bands of noise swell open and closed together, like a
    doppler pass, over a heavy low body that moves the air.
    """
    rng = random.Random(61)
    n = seconds(1.0)

    def band(t: float) -> float:
        return 180 + 1300 * math.sin(math.pi * min(t / 1.0, 1.0)) ** 1.5

    air = mix(
        (svf(pink(n, rng), band, q=1.3), 1.0),
        (svf(pink(n, rng), lambda t: band(t) * 2.3, q=2.0), 0.45),
    )
    body = svf(brown(n, rng), lambda t: 120 + 280 * math.sin(math.pi * min(t, 1.0)), q=0.8, mode="low")
    sig = mix((air, 1.0), (body, 0.9))
    env = curve(n, [(0, 0), (0.33, 1.0), (0.5, 0.9), (1.0, 0)])
    return fade_edges(normalise(mul(sig, env)))


def dash_thruster() -> list[float]:
    """Thruster - an ignition pop, then a short fluttering jet roar that dies away."""
    rng = random.Random(62)
    n = seconds(0.65)
    pop = mix(
        (thump(n, 75, 0.5, 0.09), 1.0),
        (mul(svf(white(n, rng), 2500, q=0.7, mode="high"), envelope(n, 0.001, 0.03, curve=3)), 0.6),
    )
    roar = svf(mix((brown(n, rng), 1.0), (pink(n, rng), 0.7)), lambda t: 2600 - 2000 * min(t / 0.65, 1.0), q=0.9, mode="low")
    flutter = [0.75 + 0.25 * math.sin(2 * math.pi * 32 * i / RATE) for i in range(n)]
    roar = soft_clip(normalise(mul(roar, flutter), 1.0), 2.0)
    roar = mul(roar, curve(n, [(0, 0), (0.02, 1.0), (0.2, 0.8), (0.65, 0)]))
    return fade_edges(normalise(mix((pop, 0.9), (roar, 1.0))))


def dash_phase() -> list[float]:
    """Phase Zip - a digital zip that plunges in pitch, with a shimmer and one echo.

    Like being digistructed a few metres to the side.
    """
    rng = random.Random(63)
    n = seconds(0.5)
    zip_ = tone(n, lambda t: 2400 * math.exp(-t * 9) + 180, harmonics=(0.4, 0.2))
    ring = [x * (0.6 + 0.4 * math.sin(2 * math.pi * 83 * i / RATE)) for i, x in enumerate(zip_)]
    ring = mul(ring, envelope(n, 0.004, 0.3, curve=2.0))
    air = mul(svf(white(n, rng), lambda t: 6000 - 4000 * min(t / 0.3, 1.0), q=1.5), envelope(n, 0.01, 0.35, curve=2.5))
    dry = mix((ring, 1.0), (air, 0.35))
    delay = seconds(0.06)
    echo = [0.0] * delay + [x * 0.35 for x in dry[:-delay]]
    return fade_edges(normalise(mix((dry, 1.0), (echo, 1.0))))


def air_jump_boing() -> list[float]:
    """Boing - a spring: a twangy tone whose pitch bounces and wobbles as it settles.

    The pitch starts high, snaps down, and wobbles about 11 times a second
    with the wobble dying away; a sweeping filter gives it the "oi-oi-oing".
    """
    n = seconds(0.8)

    def pitch(t: float) -> float:
        snap = 1 + 1.0 * math.exp(-t / 0.035)
        wobble = 1 + 0.14 * math.sin(2 * math.pi * 11 * t) * math.exp(-t / 0.35)
        return 165 * snap * wobble

    twang = tone(n, pitch, "tri", harmonics=(0.35, 0.15))
    twang = svf(twang, lambda t: 700 + 1500 * (0.5 + 0.5 * math.sin(2 * math.pi * 11 * t)) * math.exp(-t / 0.3), q=2.5, mode="low")
    twang = mul(twang, envelope(n, 0.004, 0.78, curve=2.2))
    return fade_edges(normalise(mix((twang, 1.0), (thump(n, 120, 0.4, 0.07), 0.5))))


def air_jump_pad() -> list[float]:
    """Jump Pad - a rising synth whoop with air rushing up past you."""
    rng = random.Random(72)
    n = seconds(0.6)
    pitch = lambda t: 220 * 4 ** min(t / 0.3, 1.0)  # noqa: E731
    whoop = mix((tone(n, pitch, "saw"), 1.0), (tone(n, lambda t: pitch(t) * 1.012, "saw"), 0.8))
    whoop = svf(whoop, lambda t: 500 + 3500 * min(t / 0.3, 1.0), q=1.5, mode="low")
    whoop = mul(whoop, curve(n, [(0, 0), (0.02, 1.0), (0.3, 0.8), (0.6, 0)]))
    air = mul(svf(pink(n, rng), lambda t: 800 + 3000 * min(t / 0.35, 1.0), q=2.0), envelope(n, 0.05, 0.5, curve=2.0))
    return fade_edges(normalise(mix((whoop, 0.7), (air, 1.0), (thump(n, 100, 0.3, 0.08), 0.5))))


def air_jump_pop() -> list[float]:
    """Air Pop - a soft bubble pop and a little puff of air under your feet."""
    rng = random.Random(73)
    n = seconds(0.35)

    def pitch(t: float) -> float:
        return 300 + 800 * min(t / 0.025, 1.0) - 200 * max(0.0, min((t - 0.025) / 0.1, 1.0))

    pop = mul(tone(n, pitch, harmonics=(0.25,)), envelope(n, 0.002, 0.08, curve=2.0))
    click = mul(svf(white(n, rng), 4000, q=0.7, mode="high"), envelope(n, 0.0005, 0.005, curve=2))
    puff = mul(svf(pink(n, rng), 2000, q=1.2), envelope(n, 0.01, 0.3, curve=2.5))
    return fade_edges(normalise(mix((pop, 1.0), (click, 0.3), (puff, 0.5))))


RECIPES = {
    "slide": {
        "Scrape": slide_scrape,
        "Scrape Soft": slide_soft,
        "Gravel": slide_gravel,
        "Dirt Skid": slide_dirt,
        "Metal Skid": slide_metal,
        "Energy Skid": slide_energy,
    },
    "dash": {
        "Whoosh": dash_whoosh,
        "Whoosh Low": dash_low,
        "Burst": dash_burst,
        "Big Whoosh": dash_big,
        "Thruster": dash_thruster,
        "Phase Zip": dash_phase,
    },
    "air_jump": {
        "Lift": air_jump_lift,
        "Hop": air_jump_hop,
        "Boing": air_jump_boing,
        "Jump Pad": air_jump_pad,
        "Air Pop": air_jump_pop,
    },
    "mantle": {"Grab": mantle_grab, "Vault": mantle_vault},
}


# Every clip is brought to about the same loudness (RMS), never past a safe
# peak, so switching clips in the menu doesn't jump in volume.
TARGET_RMS = 0.12
PEAK_LIMIT = 0.89


def level(samples: list[float]) -> list[float]:
    rms = math.sqrt(sum(x * x for x in samples) / max(len(samples), 1)) or 1.0
    peak = max((abs(x) for x in samples), default=0.0) or 1.0
    gain = min(TARGET_RMS / rms, PEAK_LIMIT / peak)
    return [x * gain for x in samples]


def write(path: Path, samples: list[float]) -> None:
    samples = level(samples)
    pcm = array.array("h", (max(-32768, min(32767, int(x * 32767))) for x in samples))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm.tobytes())


def main() -> None:
    for event, recipes in RECIPES.items():
        for name, recipe in recipes.items():
            path = OUT / event / f"{name}.wav"
            samples = recipe()
            write(path, samples)
            print(f"{path.relative_to(OUT.parent.parent)}  {len(samples) / RATE:.2f}s")


if __name__ == "__main__":
    main()
