"""Command line: ``serve`` (default), ``probe`` and ``dump``.

Every option can also be set via an environment variable (shown in --help), which is how
the systemd unit configures the exporter (EnvironmentFile=/etc/default/datakom-exporter).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sys
import threading
import time

from . import __version__
from .registers import (
    DEFAULT_GROUP_INTERVALS,
    GROUP_HELP,
    GROUPS,
    HW_VERSION_ADDR,
    IDENTITY_ADDR,
    READ_PLAN,
    RENAMED_GROUPS,
    SW_VERSION_ADDR,
    WORD_ORDER_HINTS,
    WORD_ORDERS,
    guess_word_order,
    model_name,
)

log = logging.getLogger("datakom_exporter")

DEFAULT_LISTEN = ":9725"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _non_negative(conv):
    """argparse type: conv() of a finite value >= 0 (env defaults are validated the same way)."""
    def parse(s: str):
        try:
            v = conv(s)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {s!r}") from None
        if not math.isfinite(v) or v < 0:
            raise argparse.ArgumentTypeError(f"must be a finite number >= 0: {s!r}")
        return v
    parse.__name__ = conv.__name__
    return parse


_uint = _non_negative(int)
_ufloat = _non_negative(float)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="datakom-exporter", description=__doc__,
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("command", nargs="?", default="serve", choices=("serve", "probe", "dump"),
                   help="serve: HTTP /metrics; probe: identity + word order check; "
                        "dump: register snapshot as a JSON test fixture")
    c = p.add_argument_group("controller")
    c.add_argument("--host", default=os.environ.get("DATAKOM_HOST"),
                   help="controller address, required ($DATAKOM_HOST)")
    c.add_argument("--port", type=_uint, default=_env("DATAKOM_PORT", "502"), help="$DATAKOM_PORT")
    c.add_argument("--unit-id", type=_uint, default=_env("DATAKOM_UNIT_ID", "1"),
                   help="$DATAKOM_UNIT_ID")
    c.add_argument("--word-order", choices=WORD_ORDERS,
                   default=_env("DATAKOM_WORD_ORDER", "low_first"),
                   help="32-bit word order ($DATAKOM_WORD_ORDER); D-300 MK3 is low_first")
    c.add_argument("--timeout", type=_ufloat, default=_env("DATAKOM_TIMEOUT", "2"),
                   help="per-request timeout, s ($DATAKOM_TIMEOUT)")
    c.add_argument("--retries", type=_uint, default=_env("DATAKOM_RETRIES", "1"),
                   help="pymodbus retries on the same connection ($DATAKOM_RETRIES); a lost "
                        "response is always retried once after a reconnect anyway")
    c.add_argument("--request-delay", type=_ufloat, default=_env("DATAKOM_REQUEST_DELAY", "0"),
                   help="pause between block requests, s ($DATAKOM_REQUEST_DELAY)")
    s = p.add_argument_group("serve")
    s.add_argument("--listen", default=_env("DATAKOM_LISTEN", DEFAULT_LISTEN),
                   help="[addr]:port for /metrics ($DATAKOM_LISTEN)")
    s.add_argument("--cache-ttl", type=_ufloat, default=_env("DATAKOM_CACHE_TTL", "10"),
                   help="scrapes reuse a poll result for this many seconds, counted from the end "
                        "of the poll ($DATAKOM_CACHE_TTL). Below the scrape interval it only merges "
                        "concurrent scrapes (HA pair, manual curl, scrapes queued behind a slow "
                        "poll); above it the controller is polled less often than Prometheus scrapes")
    s.add_argument("--groups", type=parse_groups,
                   default=_env("DATAKOM_GROUPS", format_groups(DEFAULT_GROUP_INTERVALS)),
                   help="polling groups and their intervals in seconds, 0 = every scrape; "
                        "groups not listed are never read ($DATAKOM_GROUPS). Groups: "
                        + "; ".join(f"{g}: {GROUP_HELP[g]}" for g in GROUPS))
    s.add_argument("--log-level", default=_env("DATAKOM_LOG_LEVEL", "WARNING"),
                   help="$DATAKOM_LOG_LEVEL")
    d = p.add_argument_group("dump")
    d.add_argument("--out", help="write the dump to this file instead of stdout")
    d.add_argument("--name", default="", help="fixture name stored in the dump")
    return p


def format_groups(groups: dict) -> str:
    return ",".join(f"{g}={v:g}" for g, v in groups.items())


def parse_groups(s: str) -> dict:
    """'genset=0,alarms=60,counters=300' -> {'genset': 0.0, ...}; 'name=off' disables a group."""
    out = {}
    for item in filter(None, (x.strip() for x in s.split(","))):
        name, _, val = item.partition("=")
        name = name.strip()
        if name not in GROUPS:
            hint = f" ({RENAMED_GROUPS[name]})" if name in RENAMED_GROUPS else ""
            raise argparse.ArgumentTypeError(
                f"unknown group {name!r}{hint}, known: {', '.join(GROUPS)}")
        val = val.strip() or "0"
        if val.lower() == "off":
            continue
        try:
            out[name] = _ufloat(val)
        except argparse.ArgumentTypeError:
            raise argparse.ArgumentTypeError(f"bad interval for {name}: {val!r}") from None
    if not out:
        raise argparse.ArgumentTypeError("no polling group enabled")
    return out


def parse_listen(s: str):
    addr, _, port = s.rpartition(":")
    return addr.strip("[]") or "0.0.0.0", int(port)


def make_reader(args):
    from .modbus import ControllerReader

    return ControllerReader(args.host, port=args.port, unit_id=args.unit_id, timeout=args.timeout,
                            retries=args.retries, request_delay=args.request_delay)


# --- serve --------------------------------------------------------------------------------------
def serve(args, stop: threading.Event | None = None) -> int:
    from prometheus_client import REGISTRY, start_http_server

    from .collector import DatakomCollector

    reader = make_reader(args)
    collector = DatakomCollector(reader, word_order=args.word_order,
                                 cache_ttl=args.cache_ttl, groups=args.groups)
    REGISTRY.register(collector)
    addr, port = parse_listen(args.listen)
    start_http_server(port, addr=addr)
    log.warning("datakom-exporter %s listening on %s:%d, controller %s:%d unit %d, word order %s, "
                "groups %s", __version__, addr, port, args.host, args.port, args.unit_id,
                args.word_order, format_groups(args.groups))

    if stop is None:
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    reader.close()
    return 0


# --- probe --------------------------------------------------------------------------------------
def probe(args) -> int:
    from .collector import decode_snapshot
    from .sanity import check

    reader = make_reader(args)
    try:
        snap = reader.poll()
    finally:
        reader.close()
    if not snap.connected and not snap.words:
        print(f"cannot read {args.host}:{args.port} unit {args.unit_id}: "
              f"{snap.failed_blocks[0][2] if snap.failed_blocks else 'no data'}")
        return 2

    w = snap.words
    ident = w.get(IDENTITY_ADDR)
    print(f"device identity : {ident if ident is None else hex(ident)} -> {model_name(ident)}"
          "  (0xd300 D-300, 0xd500 D-500, 0xd700 D-700; D-509 MK3 undocumented - record it)")
    print(f"hw / sw version : {w.get(HW_VERSION_ADDR)} / {w.get(SW_VERSION_ADDR)}")
    print(f"status / mode   : {w.get(10604)} / {w.get(10605)}")
    print(f"poll            : {snap.requests} requests, {len(snap.failed_blocks)} failed, "
          f"{snap.duration:.3f}s")
    for start, count, why in snap.failed_blocks:
        print(f"  failed block {start}+{count}: {why}")
    print("\n32-bit pairs (word@N, word@N+1):")
    for name, addr in WORD_ORDER_HINTS:
        print(f"  {name:15s} {addr}: {w.get(addr)!s:>6} {w.get(addr + 1)!s:>6}")
    guess, votes = guess_word_order(w)
    print()
    if guess:
        mark = "OK" if guess == args.word_order else "MISMATCH, set --word-order " + guess
        print(f"word order looks like {guess} (votes {votes}); configured {args.word_order}: {mark}")
    else:
        print(f"cannot decide word order (votes {votes}) - start the engine or check mains voltage")

    problems = check(decode_snapshot(w, args.word_order))
    print("\nplausibility with configured word order:", "OK" if not problems else "")
    for p in problems:
        print("  " + p)
    return 0 if not problems and not snap.failed_blocks else 1


# --- dump ---------------------------------------------------------------------------------------
def dump(args) -> int:
    reader = make_reader(args)
    try:
        snap = reader.poll()
    finally:
        reader.close()
    guess, _ = guess_word_order(snap.words)
    blocks = {}
    for start, count in READ_PLAN:
        regs = [snap.words.get(a) for a in range(start, start + count)]
        if all(v is not None for v in regs):
            blocks[str(start)] = regs
    ident = snap.words.get(IDENTITY_ADDR)
    doc = {
        "name": args.name or f"{model_name(ident)} recorded {time.strftime('%Y-%m-%d')}",
        "source": "recorded",
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "exporter_version": __version__,
        "unit_id": args.unit_id,
        "device_identity": None if ident is None else f"0x{ident:04x}",
        "word_order": args.word_order,
        "word_order_guess": guess,
        "failed_blocks": [[s, c, why] for s, c, why in snap.failed_blocks],
        "blocks": blocks,
        "expected": {},
    }
    text = json.dumps(doc, indent=1) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {args.out}: {len(blocks)} blocks, {len(snap.failed_blocks)} failed",
              file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0 if blocks else 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.host:
        parser.error("controller address is required: --host or $DATAKOM_HOST")
    logging.basicConfig(level=args.log_level.upper() if args.command == "serve" else "WARNING",
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    # pymodbus logs every failed request; our own log reports up/down transitions only.
    logging.getLogger("pymodbus").setLevel(logging.CRITICAL)
    return {"serve": serve, "probe": probe, "dump": dump}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
