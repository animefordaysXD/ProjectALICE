"""Generate synthetic logs for a stress session.

The generator writes editable text files, rotates and truncates them, emits
JSONL and TXT variants, occasionally floods a file, and includes Korean text and
fake PII so redaction runs. It stops after the duration or when a STOP file
appears.
"""

from __future__ import annotations

import argparse
import json
import random
import string
import time
from datetime import datetime
from pathlib import Path

ALPHABET = string.ascii_lowercase


def token(length: int = 6) -> str:
    return "".join(random.choice(ALPHABET) for _ in range(length))


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def make_line() -> str:
    stamp = now_text()
    roll = random.random()
    if roll < 0.28:
        return f"{stamp} ERROR worker {token()} failed to flush queue after timeout"
    if roll < 0.38:
        return f"{stamp} WARNING retry {token()} scheduled"
    if roll < 0.43:
        return f"{stamp} 오류 작업자 {token()} 실패"
    if roll < 0.48:
        return (
            f"{stamp} INFO user jane{token()}@example.com "
            f"phone 010-1234-5678 rrn 900101-1234567"
        )
    if roll < 0.485:
        return f"{stamp} INFO " + "x" * 12000
    return f"{stamp} INFO request {token()} completed in {random.randint(1, 900)}ms"


def make_json_line() -> str:
    return json.dumps(
        {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "level": random.choice(["INFO", "WARN", "ERROR"]),
            "message": f"request {token()} status {random.randint(100, 599)}",
            "id": token(8),
        },
        ensure_ascii=False,
    )


def rotate(files: list[Path]) -> None:
    for path in files:
        try:
            for index in (3, 2, 1):
                source = path.with_name(f"{path.name}.{index}")
                target = path.with_name(f"{path.name}.{index + 1}")
                if source.exists():
                    source.replace(target)
            if path.exists():
                path.replace(path.with_name(f"{path.name}.1"))
            path.write_text("", encoding="utf-8")
        except OSError:
            continue


def delete_old(files: list[Path]) -> None:
    for path in files:
        old = path.with_name(f"{path.name}.3")
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--messages", required=True)
    parser.add_argument("--duration", type=int, default=0)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--rotate-seconds", type=float, default=60.0)
    parser.add_argument("--truncate-seconds", type=float, default=180.0)
    parser.add_argument("--delete-seconds", type=float, default=300.0)
    parser.add_argument("--flood-seconds", type=float, default=120.0)
    args = parser.parse_args()

    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    files = [root / f"app-{index}.log" for index in range(4)]
    for path in files:
        path.touch()
    noise = root / "noise.txt"
    structured = root / "events.jsonl"
    binary = root / "binary.log"
    messages = Path(args.messages)
    messages.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"\x00" * 2048)
    noise.touch()
    structured.touch()
    stop = root / "STOP"

    started = time.monotonic()
    next_rotate = started + args.rotate_seconds
    next_truncate = started + args.truncate_seconds
    next_delete = started + args.delete_seconds
    next_flood = started + args.flood_seconds
    next_report = started + 60
    loops = 0

    while True:
        if args.duration and time.monotonic() - started >= args.duration:
            break
        if stop.exists():
            break

        loops += 1
        path = files[loops % len(files)]
        try:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(make_line() + "\n")
            if loops % 4 == 0:
                with noise.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(make_line() + "\n")
            if loops % 5 == 0:
                with structured.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(make_json_line() + "\n")
            if loops % 10 == 0:
                with messages.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(f"stress message {token()} status update\n")
            if loops % 50 == 0:
                binary.write_bytes(b"\x00" * 2048 + f"binary {token()}".encode())
        except OSError:
            pass

        now = time.monotonic()
        if now >= next_rotate:
            rotate(files)
            next_rotate = now + args.rotate_seconds
        if now >= next_truncate:
            try:
                (files[random.randrange(len(files))]).write_text("", encoding="utf-8")
            except OSError:
                pass
            next_truncate = now + args.truncate_seconds
        if now >= next_delete:
            delete_old(files)
            next_delete = now + args.delete_seconds
        if now >= next_flood:
            target = files[random.randrange(len(files))]
            try:
                with target.open("a", encoding="utf-8", newline="\n") as handle:
                    for _ in range(500):
                        handle.write(make_line() + "\n")
            except OSError:
                pass
            next_flood = now + args.flood_seconds
        if now >= next_report:
            print(
                f"{now_text()} stress lines={loops} files={len(files)} "
                f"messages={messages.name}",
                flush=True,
            )
            next_report = now + 60
        time.sleep(max(0.0, args.interval))

    print(f"{now_text()} stress generator stopped after {loops} iterations", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
