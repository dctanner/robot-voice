import argparse
import asyncio
import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

from robot_voice.application import RuntimeLock
from robot_voice.config import load_config
from robot_voice.memory.daily_context import day_bounds
from robot_voice.memory.store import Store, encode


def parser():
    root = argparse.ArgumentParser(description="A sleeping voice interface; SIMULATED robot only.")
    root.add_argument("--config", help="TOML settings file")
    root.add_argument("--database", type=Path)
    sub = root.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Microphone/speaker loop (requires PortAudio and Silero)")
    run.add_argument("--wake-mode", choices=["vad-transcribe", "keyboard", "keyword"])
    run.add_argument("--wake-phrase")
    run.add_argument("--audio-mode", choices=["half-duplex", "headphones", "external-aec"])
    run.add_argument("--input-device")
    run.add_argument("--output-device")
    run.add_argument("--idle-timeout", type=float)
    sub.add_parser(
        "doctor", help="Offline configuration and hardware diagnostics; no paid API calls"
    )
    sub.add_parser("devices")
    history = sub.add_parser("history")
    history.add_argument("--today", action="store_true")
    export = sub.add_parser("export")
    export.add_argument("--session", required=True)
    export.add_argument("--format", choices=["json", "jsonl", "text"], default="json")
    delete = sub.add_parser("delete")
    delete.add_argument("--session")
    delete.add_argument(
        "--before", help="Delete sessions ending before this ISO-8601 UTC timestamp"
    )
    delete.add_argument("--yes", action="store_true", required=True)
    sub.add_parser("demo", help="Deterministic zero-cloud task/memory demo")
    live = sub.add_parser(
        "live-test", help="PAID synthetic test; no microphone or speaker required"
    )
    live.add_argument("--allow-paid", action="store_true", required=True)
    live.add_argument("--seconds", type=float, default=35)
    live.add_argument("--cycles", type=int, default=1)
    live.add_argument("--startup-delay", type=float, choices=[0, 0.5, 1, 2, 3], default=0.5)
    live.add_argument("--output", type=Path, default=Path(".robot-voice/live-test"))
    return root


def doctor(config):
    import os

    import openai
    from dotenv import load_dotenv

    from robot_voice.audio.frontend import sounddevice

    load_dotenv(override=False)
    report = {
        "python": sys.version.split()[0],
        "openai": openai.__version__,
        "key_configured": bool(os.environ.get("OPENAI_API_KEY")),
        "timezone": config.timezone,
        "silero_installed": importlib.util.find_spec("silero_vad") is not None,
        "audio_mode": config.audio_mode,
        "aec_hardware_verified": False,
        "warning": "PortAudio has no automatic AEC. Test your selected external AEC device.",
    }
    try:
        sd = sounddevice()
        report["devices"] = [dict(d) for d in sd.query_devices()]
        sd.check_input_settings(
            device=config.input_device, channels=1, dtype="int16", samplerate=config.hardware_rate
        )
        sd.check_output_settings(
            device=config.output_device, channels=1, dtype="int16", samplerate=16000
        )
        report["audio_format_supported"] = True
    except Exception as exc:
        report["audio_error"] = str(exc)
    print(encode(report))
    return 1 if "audio_error" in report or not report["silero_installed"] else 0


def main(argv=None):
    args = parser().parse_args(argv)
    overrides = {
        k: getattr(args, k, None)
        for k in (
            "database",
            "wake_mode",
            "wake_phrase",
            "audio_mode",
            "input_device",
            "output_device",
            "idle_timeout",
        )
    }
    for k in ("input_device", "output_device"):
        value = overrides[k]
        if isinstance(value, str) and value.isdigit():
            overrides[k] = int(value)
    try:
        config = load_config(args.config, **overrides)
        if args.command == "doctor":
            return doctor(config)
        if args.command == "devices":
            from robot_voice.audio.frontend import sounddevice

            print(sounddevice().query_devices())
            return 0
        if args.command == "live-test":
            from robot_voice.testing import live_test

            with RuntimeLock(args.output / "test.sqlite3"):
                asyncio.run(
                    live_test(config, args.output, args.seconds, args.cycles, args.startup_delay)
                )
            return 0
        if args.command in ("run", "demo"):
            from robot_voice.application import run
            from robot_voice.testing import offline_demo

            with RuntimeLock(config.database):
                asyncio.run(run(config) if args.command == "run" else offline_demo(config))
            return 0
        with RuntimeLock(config.database):
            store = Store(config.database)
            try:
                if args.command == "history":
                    params: tuple = ()
                    sql = "SELECT * FROM sessions ORDER BY start"
                    if args.today:
                        start, end = day_bounds(datetime.now(UTC), config.timezone)
                        sql, params = (
                            "SELECT * FROM sessions WHERE start<? AND COALESCE(end,start)>=? ORDER BY start",
                            (end.isoformat(), start.isoformat()),
                        )
                    print(encode(store.rows(sql, params)))
                elif args.command == "export":
                    data = store.export(args.session)
                    if args.format == "json":
                        print(encode(data))
                    elif args.format == "jsonl":
                        for table, rows in data.items():
                            for row in rows:
                                print(encode({"table": table, **row}))
                    else:
                        print(
                            "Approximate transcript fragments; overlapping timestamps are not complete turns."
                        )
                        for row in data["transcript_fragments"]:
                            print(
                                f"[{row['start_ms']}..{row['end_ms']}] {row['speaker']}: {row['text']}"
                            )
                elif args.command == "delete":
                    if bool(args.session) == bool(args.before):
                        raise ValueError("Choose exactly one of --session or --before")
                    identities = (
                        [args.session]
                        if args.session
                        else [
                            r["id"]
                            for r in store.rows(
                                "SELECT id FROM sessions WHERE end<?",
                                (datetime.fromisoformat(args.before).astimezone(UTC).isoformat(),),
                            )
                        ]
                    )
                    for identity in identities:
                        store.delete(identity)
                    print(f"Deleted {len(identities)} sessions; filesystem backups are not erased.")
            finally:
                store.close()
    except KeyboardInterrupt:
        print("Stopped; in-process mock jobs cancelled and recorded.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Do not print cloud exception bodies/headers (can include operator data).
        print(
            f"{type(exc).__name__}: operation failed. Run doctor; inspect private local events.",
            file=sys.stderr,
        )
        if isinstance(exc, (ValueError, RuntimeError)) and not type(exc).__module__.startswith(
            "openai"
        ):
            from robot_voice.privacy import sanitize

            print(sanitize(str(exc)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
