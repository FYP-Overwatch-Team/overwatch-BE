"""Seeded Slack traffic for the load benchmark: only its shape matters, never what it says."""

import random
from dataclasses import dataclass

DAY_START = 1_794_211_200.0  # 2026-11-09 08:00 UTC

#: how much of the traffic is people talking to each other; the real mix is unknown, so it's swept
MIXES = {"low": 0.15, "medium": 0.35, "high": 0.6}
#: which discussion starts, by weight
SHAPES = {"thread_2": 40, "thread_solo": 10, "thread_mid": 25, "thread_long": 5, "run": 20}
WORDS = "deploy billing api token mongo review ticket branch cache staging error retry webhook sprint".split()
USERS = [f"U{n:04d}" for n in range(12)]


@dataclass(frozen=True)
class Posted:
    channel: str
    ts: str
    user: str
    text: str
    thread_ts: str | None


def _text(rng: random.Random) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(max(2, int(rng.lognormvariate(2.4, 0.7)))))


def _people(rng: random.Random, count: int) -> list[str]:
    return rng.sample(USERS, min(count, len(USERS)))


def _stamp(taken: set[str], value: float) -> str:
    while (ts := f"{value:.6f}") in taken:
        value += 0.000001
    taken.add(ts)
    return ts


def _thread(rng: random.Random, taken: set[str], channel: str, start: float, users: list[str], size: int) -> list[Posted]:
    root = _stamp(taken, start)
    posted = [Posted(channel, root, users[0], _text(rng), None)]
    at = start
    for i in range(1, size):
        at += rng.uniform(20, 600)
        posted.append(Posted(channel, _stamp(taken, at), users[i % len(users)], _text(rng), root))
    return posted


def _run(rng: random.Random, taken: set[str], channel: str, start: float, users: list[str], size: int) -> list[Posted]:
    at, posted = start, []
    for i in range(size):
        posted.append(Posted(channel, _stamp(taken, at), users[i % len(users)], _text(rng), None))
        at += rng.uniform(30, 300)
    return posted


def generate(messages: int, channels: int, mix: str, *, hours: float = 8, burst: float = 0.4, seed: int = 7) -> list[Posted]:
    """About `messages` messages over a working day, with `burst` of the conversations in its busiest hour."""
    rng = random.Random(f"{seed}:{messages}:{channels}:{mix}")
    names = [f"C{n:03d}" for n in range(channels)]
    span, peak = hours * 3600, rng.uniform(0, (hours - 1) * 3600)
    taken: set[str] = set()
    posted: list[Posted] = []
    while len(posted) < messages:
        channel = rng.choice(names)
        start = DAY_START + (peak + rng.uniform(0, 3600) if rng.random() < burst else rng.uniform(0, span))
        if rng.random() >= MIXES[mix]:
            posted.append(Posted(channel, _stamp(taken, start), rng.choice(USERS), _text(rng), None))
            continue
        shape = rng.choices(list(SHAPES), weights=list(SHAPES.values()))[0]
        if shape == "thread_2":
            posted += _thread(rng, taken, channel, start, _people(rng, 2), 2)
        elif shape == "thread_solo":
            posted += _thread(rng, taken, channel, start, _people(rng, 1), rng.randint(2, 3))
        elif shape == "thread_mid":
            posted += _thread(rng, taken, channel, start, _people(rng, rng.randint(2, 4)), rng.randint(3, 8))
        elif shape == "thread_long":
            posted += _thread(rng, taken, channel, start, _people(rng, rng.randint(3, 6)), rng.randint(15, 40))
        else:
            posted += _run(rng, taken, channel, start, _people(rng, rng.randint(2, 4)), rng.randint(3, 10))
    return sorted(posted[:messages], key=lambda m: float(m.ts))
