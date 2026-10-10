"""Run the attack set against a defence set, score, save, compare, replay."""
import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import agent
from defences import ALL_DEFENCES
from scorer import control_passes, score_attack, score_mentions

ROOT = Path(__file__).parent
ATTACK_FILES = [ROOT / "attacks" / "library.json", ROOT / "attacks" / "generated.json"]
CACHE_PATH = ROOT / ".cache" / "replies.json"
RESULTS_DIR = ROOT / "results"
MAX_CALLS = 60
WORKERS = 2
CallFn = Callable[[dict, set], str]


def parse_defences(spec: str) -> set[str]:
    spec = spec.strip()
    if spec in ("", "none"):
        return set()
    if spec == "all":
        return set(ALL_DEFENCES)
    names = {s.strip() for s in spec.split(",")}
    unknown = names - set(ALL_DEFENCES)
    if unknown:
        raise ValueError(f"unknown defences: {sorted(unknown)}")
    return names


def non_negative(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def load_attacks(paths: list[Path]) -> list[dict]:
    attacks: list[dict] = []
    for path in paths:
        if Path(path).exists():
            attacks.extend(json.loads(Path(path).read_text(encoding="utf-8")))
    return attacks


def verdict_for(technique: str, reply: str, marker: Optional[str]) -> str:
    if technique == "control":
        return "pass" if control_passes(reply) else "fail"
    return score_attack(reply, marker)


def mentions_for(technique: str, reply: str, marker: Optional[str]) -> Optional[str]:
    """Lenient verdict (marker or canary anywhere). None for controls."""
    return None if technique == "control" else score_mentions(reply, marker)


def run_set(attacks: list[dict], enabled: set[str], call: CallFn, model: str = "") -> dict:
    results = []
    for attack in attacks:
        reply = call(attack, enabled)
        marker = attack.get("marker")
        results.append(
            {
                "id": attack["id"],
                "technique": attack["technique"],
                "marker": marker,
                "reply": reply,
                "verdict": verdict_for(attack["technique"], reply, marker),
                "mentions": mentions_for(attack["technique"], reply, marker),
            }
        )
    return {"model": model, "defences": sorted(enabled), "results": results}


def rescore(record: dict) -> dict:
    results = [
        {
            **r,
            "verdict": verdict_for(r["technique"], r["reply"], r.get("marker")),
            "mentions": mentions_for(r["technique"], r["reply"], r.get("marker")),
        }
        for r in record["results"]
    ]
    return {**record, "results": results}


def summarize(record: dict) -> dict:
    techniques: dict[str, dict] = {}
    controls = {"n": 0, "passed": 0}
    for r in record["results"]:
        if r["technique"] == "control":
            controls["n"] += 1
            controls["passed"] += r["verdict"] == "pass"
            continue
        t = techniques.setdefault(r["technique"], {"n": 0, "success": 0, "mentions": 0, "no_reply": 0})
        if r["verdict"] == "no reply":
            t["no_reply"] += 1
            continue
        t["n"] += 1
        t["success"] += r["verdict"] == "success"
        t["mentions"] += r.get("mentions") == "success"
    for t in techniques.values():
        t["rate"] = t["success"] / t["n"] if t["n"] else None
    total = sum(t["n"] for t in techniques.values())
    wins = sum(t["success"] for t in techniques.values())
    lenient = sum(t["mentions"] for t in techniques.values())
    silent = sum(t["no_reply"] for t in techniques.values())
    return {
        "techniques": techniques,
        "control": {**controls, "rate": controls["passed"] / controls["n"] if controls["n"] else None},
        "overall": {
            "n": total,
            "success": wins,
            "rate": wins / total if total else None,
            "mentions": lenient,
            "no_reply": silent,
        },
    }


def compare(a: dict, b: dict) -> dict[str, float]:
    """Rate change from A to B per technique. Negative attack delta means B is safer."""
    deltas: dict[str, float] = {}
    for name in sorted(set(a["techniques"]) | set(b["techniques"])):
        deltas[name] = b["techniques"].get(name, {"rate": 0.0})["rate"] - a["techniques"].get(name, {"rate": 0.0})["rate"]
    if a["control"]["rate"] is not None and b["control"]["rate"] is not None:
        deltas["control"] = b["control"]["rate"] - a["control"]["rate"]
    if a["overall"]["rate"] is not None and b["overall"]["rate"] is not None:
        deltas["overall"] = b["overall"]["rate"] - a["overall"]["rate"]
    return deltas


def _pct(rate: Optional[float]) -> str:
    return "n/a" if rate is None else f"{rate:.0%}"


def format_table(summary: dict) -> str:
    rows = [("technique", "scored", "strict", "strict rate", "mentions", "no reply")]
    for name, t in sorted(summary["techniques"].items()):
        rows.append((name, str(t["n"]), str(t["success"]), _pct(t["rate"]), str(t["mentions"]), str(t["no_reply"])))
    o = summary["overall"]
    rows.append(("ALL ATTACKS", str(o["n"]), str(o["success"]), _pct(o["rate"]), str(o["mentions"]), str(o["no_reply"])))
    c = summary["control"]
    rows.append(("control (useful)", str(c["n"]), str(c["passed"]), _pct(c["rate"]), "", ""))
    widths = [max(len(r[i]) for r in rows) for i in range(6)]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)) for r in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def format_compare(a: dict, b: dict, deltas: dict[str, float]) -> str:
    rows = [("technique", "A", "B", "change")]
    for name, delta in deltas.items():
        if name == "control":
            ra, rb = a["control"]["rate"], b["control"]["rate"]
        elif name == "overall":
            ra, rb = a["overall"]["rate"], b["overall"]["rate"]
        else:
            ra = a["techniques"].get(name, {}).get("rate")
            rb = b["techniques"].get(name, {}).get("rate")
        rows.append((name, _pct(ra), _pct(rb), f"{delta:+.0%}"))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)) for r in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def save_run(record: dict, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=True), encoding="utf-8")


def load_run(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class ReplyCache:
    """Disk cache of model replies keyed by model, system prompt, defences, and email."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.Lock()
        self.data: dict[str, str] = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def _key(self, model: str, system: str, enabled: set[str], email: dict, run: int = 0) -> str:
        parts = [model, system, sorted(enabled), email["id"], email["subject"], email["body"]]
        if run:
            parts.append(run)  # run 0 keeps the original key, so existing replies stay valid
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest()

    def get(self, model: str, system: str, enabled: set[str], email: dict, run: int = 0) -> Optional[str]:
        return self.data.get(self._key(model, system, enabled, email, run))

    def put(self, model: str, system: str, enabled: set[str], email: dict, reply: str, run: int = 0) -> None:
        with self.lock:
            self.data[self._key(model, system, enabled, email, run)] = reply
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data), encoding="utf-8")

    def wrap(self, call: CallFn, model: str, system: str) -> CallFn:
        def cached(email: dict, enabled: set[str]) -> str:
            reply = self.get(model, system, enabled, email)
            if reply is None:
                reply = call(email, enabled)
                self.put(model, system, enabled, email, reply)
            return reply

        return cached


SECRET_PATTERNS = [
    re.compile(r"""['"]?(?:authorization|proxy-authorization|x-api-key|api[-_ ]?key|cookie|set-cookie)['"]?\s*[:=]\s*[^\n,}]*""", re.I),
    re.compile(r"bearer\s+\S+", re.I),
    re.compile(r"[A-Za-z0-9_\-.]{32,}"),
]
ERROR_DETAIL_MAX = 200


def describe_error(exc: Exception) -> str:
    """Exception class and message for the failed-call log: one line, secrets removed, then cut to 200 chars."""
    text = str(exc)
    key = os.environ.get("FEATHERLESS_API_KEY", "").strip()
    if key:
        text = text.replace(key, "[redacted]")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    text = " ".join(text.split())[:ERROR_DETAIL_MAX]
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def fetch_missing(
    missing: list[dict], enabled: set[str], call: Callable[[dict, set], object], cache: ReplyCache,
    model: str, system: str, workers: int = WORKERS, run_index: int = 0,
) -> tuple[int, int]:
    """Call the API for uncached emails, at most `workers` at a time. Returns (failed, truncated).

    Failed and truncated calls are not cached and not scored, so they cannot pass as blocked attacks.
    `call` may return a str or an agent.Response. One metadata line is printed per call, never the reply.
    """

    def one(email: dict) -> str:
        start = time.monotonic()
        try:
            result = call(email, enabled)
        except agent.Truncated:
            print(f"{email['id']}  truncated after retry, skipped")
            return "truncated"
        except Exception as exc:  # timeout, rate limit, network: skip this email
            print(f"{email['id']}  failed ({describe_error(exc)}), skipped")
            return "failed"
        elapsed = time.monotonic() - start
        reply = result.reply if isinstance(result, agent.Response) else result
        finish = getattr(result, "finish_reason", None) or "n/a"
        think = "yes" if getattr(result, "think", False) else "no"
        retried = "yes" if getattr(result, "retried", False) else "no"
        print(f"{email['id']}  elapsed={elapsed:.1f}s  finish={finish}  think={think}  retried={retried}")
        cache.put(model, system, enabled, email, reply, run_index)
        return "ok"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(one, missing))
    return outcomes.count("failed"), outcomes.count("truncated")


def positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def run_repeats(
    attacks: list[dict], enabled: set[str], cache: ReplyCache, model: str, system: str, args: argparse.Namespace,
) -> int:
    """N independent live runs, saved as run_1.json..run_N.json. Run k caches under run index k."""
    out_dir: Path = args.out_dir
    taken = [out_dir / f"run_{k}.json" for k in range(1, args.repeat + 1) if (out_dir / f"run_{k}.json").exists()]
    if taken:
        print(f"error: {taken[0]} already exists; refusing to overwrite", file=sys.stderr)
        return 1
    todo = {k: [a for a in attacks if cache.get(model, system, enabled, a, k) is None] for k in range(1, args.repeat + 1)}
    total = sum(len(v) for v in todo.values())
    if total > args.max_calls:
        print(f"error: {total} uncached calls exceeds the cap of {args.max_calls} (--max-calls)", file=sys.stderr)
        return 1
    call = None
    if total:
        try:
            call = agent.Agent(agent.make_client()).respond
        except (RuntimeError, ImportError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"{total} uncached API calls across {args.repeat} repeats "
              f"(cap {args.max_calls}, {WORKERS} at a time, {agent.TIMEOUT_S}s timeout each).")
        if input("Type y to continue: ").strip().lower() != "y":
            print("aborted, no calls made.")
            return 1
    for k in range(1, args.repeat + 1):
        failed = truncated = 0
        if todo[k]:
            print(f"-- run_{k}: {len(todo[k])} calls")
            failed, truncated = fetch_missing(todo[k], enabled, call, cache, model, system, run_index=k)
        done = [a for a in attacks if cache.get(model, system, enabled, a, k) is not None]
        record = run_set(done, enabled, lambda email, en, k=k: cache.get(model, system, en, email, k), model=model)
        missing = [a["id"] for a in attacks if a not in done]
        path = out_dir / f"run_{k}.json"
        if missing:
            record = {**record, "incomplete": True, "failed": failed, "truncated": truncated, "missing": missing}
            print(f"run_{k}: INCOMPLETE ({failed} failed, {truncated} truncated, {len(missing)} not scored)")
        else:
            print(f"run_{k}: complete")
        save_run(record, path)
        print(f"saved {path}")
    return 0


def summarize_repeats(folder: Path) -> tuple[str, bool]:
    """Offline per-run strict successes with min, max and mean. Returns (text, found_any)."""
    runs = sorted(Path(folder).glob("run_*.json"), key=lambda f: int(f.stem.split("_")[1]) if f.stem.split("_")[1].isdigit() else -1)
    if not runs:
        return f"no run_*.json files in {folder}", False
    lines, counts, skipped = [], [], 0
    for f in runs:
        record = load_run(f)
        overall = summarize(rescore(record))["overall"]
        score = f"strict {overall['success']}/{overall['n']}"
        if record.get("incomplete"):
            skipped += 1
            lines.append(
                f"{f.stem:<8} INCOMPLETE ({record.get('failed', 0)} failed, {record.get('truncated', 0)} truncated, "
                f"missing {', '.join(record.get('missing', []))})  {score} (not counted)"
            )
        else:
            counts.append(overall["success"])
            lines.append(f"{f.stem:<8} {score}")
    if counts:
        lines.append(f"min {min(counts)}  max {max(counts)}  mean {sum(counts) / len(counts):.2f}  ({len(counts)} complete runs)")
    else:
        lines.append("no complete runs; no statistics")
    if skipped:
        lines.append(f"{skipped} incomplete run{'s' if skipped != 1 else ''} excluded from min, max and mean")
    return "\n".join(lines), True


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Tripwire: red-team an email assistant. Cache-only unless --live.")
    p.add_argument("--defences", default="none", help="none, all, or comma list: " + ",".join(ALL_DEFENCES))
    p.add_argument("--live", action="store_true", help="call the API for uncached emails (asks first)")
    p.add_argument("--max-calls", type=int, default=MAX_CALLS, help="hard cap on API calls per run")
    p.add_argument("--limit", type=non_negative, help="with --live, make at most N uncached calls")
    p.add_argument("--out", type=Path, help="save results here (default results/<defences>.json)")
    p.add_argument("--repeat", type=positive, help="with --live and --out-dir, make N independent runs: run_1.json..run_N.json")
    p.add_argument("--out-dir", type=Path, help="folder for --repeat results (e.g. results/repeats/<name>/)")
    p.add_argument("--summary", type=Path, metavar="DIR", help="print per-run strict successes for a repeats folder")
    p.add_argument("--replay", type=Path, help="view a recorded results file; no API key needed")
    p.add_argument("--compare", nargs=2, type=Path, metavar=("A", "B"), help="compare two recorded runs")
    args = p.parse_args(argv)

    if args.summary:
        text, found = summarize_repeats(args.summary)
        print(text, file=sys.stdout if found else sys.stderr)
        return 0 if found else 1
    if args.compare:
        a, b = (summarize(rescore(load_run(f))) for f in args.compare)
        print(format_compare(a, b, compare(a, b)))
        return 0
    if args.replay:
        print(format_table(summarize(rescore(load_run(args.replay)))))
        return 0

    try:
        enabled = parse_defences(args.defences)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    model, system = agent.MODEL, agent.SYSTEM_PROMPT
    attacks = load_attacks(ATTACK_FILES)
    cache = ReplyCache(CACHE_PATH)
    if args.repeat or args.out_dir:
        if not (args.repeat and args.out_dir and args.live):
            print("error: --repeat needs --live and --out-dir (and --out-dir needs --repeat)", file=sys.stderr)
            return 1
        if args.limit is not None:
            print("error: --limit cannot be combined with --repeat", file=sys.stderr)
            return 1
        return run_repeats(attacks, enabled, cache, model, system, args)
    missing = [a for a in attacks if cache.get(model, system, enabled, a) is None]
    failed = truncated = 0

    if args.live and missing:
        to_call = missing if args.limit is None else missing[: args.limit]
        if len(to_call) > args.max_calls:
            print(f"error: {len(to_call)} uncached calls exceeds the cap of {args.max_calls} (--max-calls)", file=sys.stderr)
            return 1
        try:
            client = agent.make_client()
        except (RuntimeError, ImportError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"{len(to_call)} uncached API calls (cap {args.max_calls}, {WORKERS} at a time, {agent.TIMEOUT_S}s timeout each).")
        if len(to_call) < len(missing):
            print(f"{len(missing) - len(to_call)} more uncached attacks will remain (--limit {args.limit}).")
        if input("Type y to continue: ").strip().lower() != "y":
            print("aborted, no calls made.")
            return 1
        failed, truncated = fetch_missing(to_call, enabled, agent.Agent(client).respond, cache, model, system)
        missing = [a for a in attacks if cache.get(model, system, enabled, a) is None]

    cached = [a for a in attacks if cache.get(model, system, enabled, a) is not None]
    record = run_set(cached, enabled, lambda email, en: cache.get(model, system, en, email), model=model)
    print(format_table(summarize(record)))
    if failed:
        print(f"{failed} calls failed and were skipped.")
    if truncated:
        print(f"{truncated} truncated (skipped, not scored).")
    if missing:
        hint = "" if args.live else " Re-run with --live to fetch them."
        print(f"{len(missing)} uncached attacks not scored; results not saved.{hint}")
        return 0
    out = args.out or RESULTS_DIR / f"{'+'.join(sorted(enabled)) or 'none'}.json"
    save_run(record, out)
    print(f"saved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
