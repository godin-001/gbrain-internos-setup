#!/usr/bin/env python3
"""
claude-code-sessions-to-corpus.py — Convert Claude Code JSONL sessions to gbrain corpus markdown.

Companion to sessions-to-corpus.py (which handles Hermes' flat schema). Claude Code
sessions use a different, typed-record schema that the Hermes script cannot parse, so
running this skill's pipeline against ~/.claude/projects/ requires this adapter.

Claude Code schema (differs from Hermes):
    - Each line is a typed record. Relevant: type in {user, assistant}.
    - Other record types (file-history-snapshot, permission-mode, ai-title,
      attachment, system, last-prompt) are skipped.
    - message.content is a LIST of blocks: {type: text|thinking|tool_use|tool_result}.
      Only `text` blocks are kept; thinking / tool_use / tool_result are dropped.
    - A role=user record may carry only tool_result blocks (tool output) — skipped.
    - Top-level fields used: timestamp, sessionId, cwd, gitBranch, version.

Output: one markdown file per session at
    <corpus_dir>/claude-code-sessions/<YYYY-MM-DD>/<session-id-prefix>.md
(Per-session, not per-day: Claude Code sessions are independent conversations.)

Secret scrubbing: unlike the Hermes script, this adapter scrubs common credential
shapes (GitHub/OpenAI/Anthropic/Slack/Supabase tokens, JWTs, AWS keys, and Postgres
connection-string passwords) before writing, replacing them with [REDACTED-<kind>].
Dropping tool_result blocks also removes most secret-bearing command output.

Usage:
    python3 claude-code-sessions-to-corpus.py
    python3 claude-code-sessions-to-corpus.py --dry-run --verbose
    python3 claude-code-sessions-to-corpus.py --corpus-dir /tmp/gbrain-corpus
    python3 claude-code-sessions-to-corpus.py --min-size 2000   # skip tiny sessions

Environment:
    GBRAIN_CORPUS_DIR  Override corpus output dir (else read from gbrain config
                       key dream.synthesize.session_corpus_dir, like the Hermes script).
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DEFAULT_PROJECTS_ROOT = Path("~/.claude/projects").expanduser()

# Secret-scrub patterns. Order matters: more specific first.
SECRET_PATTERNS = [
    (re.compile(r"gh[opsur]_[A-Za-z0-9]{30,}"), "[REDACTED-github-token]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{40,}"), "[REDACTED-anthropic-key]"),
    (re.compile(r"sk-proj-[A-Za-z0-9_-]{50,}"), "[REDACTED-openai-project-key]"),
    (re.compile(r"sk-svcacct-[A-Za-z0-9_-]{50,}"), "[REDACTED-openai-svcacct-key]"),
    (re.compile(r"sk-[A-Za-z0-9_-]{40,}"), "[REDACTED-openai-key]"),
    (re.compile(r"cfut_[A-Za-z0-9]{20,}"), "[REDACTED-cloudflare-token]"),
    (re.compile(r"xox[abprs]-[A-Za-z0-9-]{20,}"), "[REDACTED-slack-token]"),
    (re.compile(r"sbp_[A-Za-z0-9]{30,}"), "[REDACTED-supabase-pat]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_.-]{20,}"), "[REDACTED-jwt]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED-aws-access-key]"),
    (re.compile(r"(postgres(?:ql)?://[^:@\s]+:)[^@\s]+(@)"), r"\1[REDACTED-db-password]\2"),
]

SYSTEM_INJECTION_RE = re.compile(r"^\s*<\s*system-reminder|^\s*<command-name>|^\s*Caveat:", re.I)


def scrub(text: str) -> str:
    for pat, repl in SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def get_corpus_dir() -> Path:
    """Resolve corpus dir the same way the Hermes script does:
    GBRAIN_CORPUS_DIR env, else gbrain config dream.synthesize.session_corpus_dir."""
    env_override = os.environ.get("GBRAIN_CORPUS_DIR")
    if env_override:
        return Path(env_override)

    gbrain = Path.home() / ".bun" / "bin" / "gbrain"
    if not gbrain.exists():
        sys.exit("gbrain not found at ~/.bun/bin/gbrain. Set GBRAIN_CORPUS_DIR or pass --corpus-dir.")

    env = {**os.environ, "GBRAIN_DISABLE_DIRECT_POOL": "1"}
    result = subprocess.run(
        [str(gbrain), "config", "get", "dream.synthesize.session_corpus_dir"],
        capture_output=True, text=True, env=env,
    )
    for line in result.stdout.splitlines():
        line = line.strip()
        if line and not line.startswith("[gbrain]"):
            return Path(line)

    sys.exit(
        "dream.synthesize.session_corpus_dir not set in gbrain config.\n"
        "Run: gbrain config set dream.synthesize.session_corpus_dir <path>\n"
        "Or set GBRAIN_CORPUS_DIR / pass --corpus-dir."
    )


def iter_text_blocks(content) -> Iterator[str]:
    """Yield only user/assistant text. Skip thinking / tool_use / tool_result blocks."""
    if content is None:
        return
    if isinstance(content, str):
        yield content
        return
    if not isinstance(content, list):
        return
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            txt = item.get("text", "")
            if isinstance(txt, str):
                yield txt


def extract_session(jsonl_path: Path) -> dict | None:
    meta: dict = {}
    turns: list[dict] = []
    with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = rec.get("type")
            if rtype in ("user", "assistant") and not meta:
                meta = {
                    "id": rec.get("sessionId"),
                    "cwd": rec.get("cwd"),
                    "gitBranch": rec.get("gitBranch"),
                    "version": rec.get("version"),
                    "timestamp": rec.get("timestamp"),
                }
            if rtype not in ("user", "assistant"):
                continue
            msg = rec.get("message", {})
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", rtype)
            text_parts = list(iter_text_blocks(msg.get("content")))
            if not text_parts:
                continue  # tool_result-only user turns, tool_use-only assistant turns
            joined = "\n\n".join(p.strip() for p in text_parts if p and p.strip())
            if not joined:
                continue
            if role == "user" and SYSTEM_INJECTION_RE.match(joined.split("\n", 1)[0]):
                continue
            turns.append({"role": role, "ts": rec.get("timestamp", ""), "text": joined})
    if not turns:
        return None
    return {"meta": meta, "turns": turns, "source_path": str(jsonl_path)}


def session_date(meta: dict, fallback_path: Path) -> str:
    ts = meta.get("timestamp")
    if ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            pass
    return datetime.fromtimestamp(fallback_path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")


def render_markdown(session: dict, source_rel: str) -> tuple[str, str]:
    meta = session["meta"]
    sid = meta.get("id") or hashlib.sha256(source_rel.encode()).hexdigest()
    short = sid.split("-")[0] if sid else "unknown"
    date_str = session_date(meta, Path(session["source_path"]))
    title = f"Claude Code session {short} ({date_str})"
    n_user = sum(1 for t in session["turns"] if t["role"] == "user")
    n_asst = sum(1 for t in session["turns"] if t["role"] == "assistant")
    fm = [
        "---",
        f"title: {json.dumps(title)}",
        "type: claude-code-session",
        "tags: [claude-code-session, coding-agent]",
        f"session_id: {sid}",
        f"session_timestamp: {meta.get('timestamp', '')}",
        f"session_cwd: {json.dumps(meta.get('cwd') or 'unknown')}",
        f"git_branch: {json.dumps(meta.get('gitBranch') or '')}",
        f"source_path: {json.dumps(source_rel)}",
        f"turn_count: {len(session['turns'])}",
        f"user_turns: {n_user}",
        f"assistant_turns: {n_asst}",
        "---",
        "",
    ]
    body = ["\n".join(fm), f"# {title}", ""]
    for i, turn in enumerate(session["turns"], 1):
        hdr = f"## Turn {i} — {turn['role']}" + (f" — {turn['ts']}" if turn.get("ts") else "")
        body += [hdr, "", scrub(turn["text"]), ""]
    return short, "\n".join(body).rstrip() + "\n"


def process(jsonl_path: Path, corpus_dir: Path, projects_root: Path, dry_run: bool) -> dict:
    sess = extract_session(jsonl_path)
    if sess is None:
        return {"status": "empty", "src": str(jsonl_path)}
    short, body = render_markdown(sess, str(jsonl_path.relative_to(projects_root)))
    date_str = session_date(sess["meta"], jsonl_path)
    out_path = corpus_dir / "claude-code-sessions" / date_str / f"{short}.md"
    result = {"status": "ok", "src": str(jsonl_path), "out": str(out_path),
              "date": date_str, "turns": len(sess["turns"]), "bytes": len(body)}
    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if out_path.exists():
            digest = hashlib.sha256(str(jsonl_path).encode()).hexdigest()[:6]
            out_path = out_path.with_name(f"{short}-{digest}.md")
            result["out"] = str(out_path)
            result["collision_suffixed"] = True
        out_path.write_text(body, encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Convert Claude Code sessions to gbrain corpus markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--projects-root", type=Path, default=DEFAULT_PROJECTS_ROOT,
                    help="Claude Code projects dir (default: ~/.claude/projects)")
    ap.add_argument("--corpus-dir", type=Path, default=None,
                    help="Output corpus dir (default: GBRAIN_CORPUS_DIR or gbrain config)")
    ap.add_argument("--glob", default="*/*.jsonl", help="Glob under projects-root")
    ap.add_argument("--limit", type=int, default=0, help="Max sessions (0 = all)")
    ap.add_argument("--min-size", type=int, default=0, help="Skip JSONL files smaller than N bytes")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    corpus_dir = args.corpus_dir if args.corpus_dir else get_corpus_dir()

    paths = sorted(p for p in args.projects_root.glob(args.glob) if ".bak" not in p.name)
    if args.min_size:
        paths = [p for p in paths if p.stat().st_size >= args.min_size]
    if args.limit:
        paths = paths[: args.limit]

    print(f"Processing {len(paths)} Claude Code sessions → {corpus_dir}"
          f"{' [DRY-RUN]' if args.dry_run else ''}", file=sys.stderr)
    totals = {"ok": 0, "empty": 0, "err": 0, "bytes": 0, "turns": 0, "collisions": 0}
    for p in paths:
        try:
            r = process(p, corpus_dir, args.projects_root, args.dry_run)
            totals[r["status"]] += 1
            if r["status"] == "ok":
                totals["bytes"] += r["bytes"]
                totals["turns"] += r["turns"]
                if r.get("collision_suffixed"):
                    totals["collisions"] += 1
                if args.verbose:
                    print(f"  ok  {r['date']} {r['turns']:3d} turns {r['bytes']:7d}B {r['out']}", file=sys.stderr)
            elif args.verbose:
                print(f"  {r['status']:5s} {r['src']}", file=sys.stderr)
        except Exception as e:
            totals["err"] += 1
            print(f"  ERR  {p}: {type(e).__name__}: {e}", file=sys.stderr)

    print(f"\nDone. ok={totals['ok']} empty={totals['empty']} err={totals['err']} "
          f"collisions={totals['collisions']} total_md_bytes={totals['bytes']} "
          f"total_turns={totals['turns']}", file=sys.stderr)
    return 0 if totals["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
