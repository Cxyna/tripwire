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
from scorer import control_passes, score_attack

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
            }
        )
    return {"model": model, "defences": sorted(enabled), "results": results}


def rescore(record: dict) -> dict:
    results = [{**r, "verdict": verdict_for(r["technique"], r["reply"], r.get("marker"))} for r in record["results"]]
    return {**record, "results": results}


def summarize(record: dict) -> dict:
    techniques: dict[str, dict] = {}
    controls = {"n": 0, "passed": 0}
    for r in record["results"]:
        if r["technique"] == "control":
            controls["n"] += 1
            controls["passed"] += r["verdict"] == "pass"
            continue
        t = techniques.setdefault(r["technique"], {"n": 0, "success": 0})
        t["n"] += 1
        t["success"] += r["verdict"] == "success"
    for t in techniques.values():
        t["rate"] = t["success"] / t["n"]
    total = sum(t["n"] for t in techniques.values())
    wins = sum(t["success"] for t in techniques.values())
    return {
        "techniques": techniques,
        "control": {**controls, "rate": controls["passed"] / controls["n"] if controls["n"] else None},
        "overall": {"n": total, "success": wins, "rate": wins / total if total else None},
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
    rows = [("technique", "attacks", "succeeded", "rate")]
    for name, t in sorted(summary["techniques"].items()):
        rows.append((name, str(t["n"]), str(t["success"]), _pct(t["rate"])))
    o = summary["overall"]
    rows.append(("ALL ATTACKS", str(o["n"]), str(o["success"]), _pct(o["rate"])))
    c = summary["control"]
    rows.append(("control (useful)", str(c["n"]), str(c["passed"]), _pct(c["rate"])))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
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

    def _key(self, model: str, system: str, enabled: set[str], email: dict) -> str:
        blob = json.dumps([model, system, sorted(enabled), email["id"], email["subject"], email["body"]])
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, model: str, system: str, enabled: set[str], email: dict) -> Optional[str]:
        return self.data.get(self._key(model, system, enabled, email))

    def put(self, model: str, system: str, enabled: set[str], email: dict, reply: str) -> None:
        with self.lock:
            self.data[self._key(model, system, enabled, email)] = reply
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
    model: str, system: str, workers: int = WORKERS,
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
        cache.put(model, system, enabled, email, reply)
        return "ok"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(one, missing))
    return outcomes.count("failed"), outcomes.count("truncated")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Tripwire: red-team an email assistant. Cache-only unless --live.")
    p.add_argument("--defences", default="none", help="none, all, or comma list: " + ",".join(ALL_DEFENCES))
    p.add_argument("--live", action="store_true", help="call the API for uncached emails (asks first)")
    p.add_argument("--max-calls", type=int, default=MAX_CALLS, help="hard cap on API calls per run")
    p.add_argument("--limit", type=non_negative, help="with --live, make at most N uncached calls")
    p.add_argument("--out", type=Path, help="save results here (default results/<defences>.json)")
    p.add_argument("--replay", type=Path, help="view a recorded results file; no API key needed")
    p.add_argument("--compare", nargs=2, type=Path, metavar=("A", "B"), help="compare two recorded runs")
    args = p.parse_args(argv)

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
