---
name: gbrain-internos-setup
description: "Run gbrain's dream import pipeline without an Anthropic API key, using Claude Code as the synthesis engine. Covers two permanent workarounds: Supabase IPv6 fix and Claude Code haiku subagent replacement for gbrain's Anthropic-dependent synthesize phase."
version: 1.0.0
author: Aibus Dumbleclaw / Mel (dumbleclaw)
license: MIT
platforms: [linux, macos]
required_environment_variables:
  - name: OPENAI_API_KEY
    prompt: OpenAI API key
    help: Used for embeddings (text-embedding-3-large). Get at https://platform.openai.com/api-keys
    required_for: gbrain import (embedding generation)
  - name: GBRAIN_DISABLE_DIRECT_POOL
    prompt: "Set to 1"
    help: Disables gbrain's auto-derived direct Postgres connection. Required on Supabase free tier (IPv6-only direct host). Without it, every gbrain command fails with ECONNREFUSED.
    required_for: Supabase connectivity on free tier
prerequisites:
  commands: [bun, python3]
  skills: [gbrain-supabase]
metadata:
  hermes:
    tags: [gbrain, brain, memory, knowledge, internos, claude-code, synthesis, import, supabase, openai]
    related_skills: [gbrain-setup, gbrain-supabase]
  gbrain_version_tested: "0.41.26.1"
  opensrc_registry: OSL-0003
---

# gbrain internOS Setup

**Problem this skill solves:** gbrain's `dream` cycle (the pipeline that synthesizes conversation transcripts into a searchable knowledge brain) requires an Anthropic API key for two steps:

1. **Significance judge** — Haiku decides if a transcript is worth synthesizing
2. **Synthesis subagent** — Sonnet reads the transcript and calls `put_page` to write brain pages

If you have only an OpenAI key (no Anthropic key), neither step works in gbrain v0.41. This skill documents two workarounds that together enable the full import pipeline using only OpenAI embeddings + Claude Code's model access.

Additionally, gbrain fails on Supabase free tier due to an IPv6 routing issue — that fix is also documented here.

---

## Workaround 1 — Supabase Free Tier IPv6

### Problem

gbrain's `ConnectionManager` auto-derives a direct Postgres URL from the pooler URL by extracting the project ref and constructing `db.<ref>.supabase.co:5432`. On Supabase free tier, that host resolves to IPv6 only. Machines without IPv6 routing get `ECONNREFUSED` on every gbrain startup.

### Fix

```bash
export GBRAIN_DISABLE_DIRECT_POOL=1
```

Set permanently in your shell env or agent env file (e.g. `~/.hermes/.env`):

```bash
echo "GBRAIN_DISABLE_DIRECT_POOL=1" >> ~/.hermes/.env
```

This activates `readKillSwitchEnv()` in gbrain's connection manager, skipping the direct pool entirely. All operations route through the session pooler (port 6543). No functional penalty.

### What does NOT work

- `NODE_OPTIONS=--dns-result-order=ipv4first` — gbrain runs on Bun, not Node.js. Silently ignored.

### Paid alternative

Supabase Dashboard → Project Settings → Add-ons → IPv4 address (~$4/mo). Makes the env var unnecessary.

---

## Workaround 2 — Claude Code as Synthesis Engine

### Problem

`gbrain dream --phase synthesize` uses two Anthropic-dependent components:

- **Significance judge** (`makeJudgeClient`) — checks `ANTHROPIC_API_KEY` before returning a usable Haiku judge. No key → every transcript skipped.
- **Synthesis subagent** (MinionQueue worker, `subagent.ts`) — even with `agent.use_gateway_loop true` and an OpenAI model configured, gbrain's `put_page` tool schema fails with `schema is not a function` when routed through the OpenAI gateway. Anthropic-specific format, partially implemented for other providers in v0.41.

### Fix — Three-step replacement pipeline

Replace gbrain's subagent with Claude Code's own model access:

```
sessions JSONL  →  corpus markdown  →  Claude haiku synthesis  →  gbrain import
```

**Step 1 — Convert sessions to corpus markdown**

```bash
python3 ~/.hermes/scripts/sessions-to-corpus.py --date YYYY-MM-DD
# or range:
python3 ~/.hermes/scripts/sessions-to-corpus.py --from 2026-05-14 --to 2026-05-27
```

Reads `~/.hermes/sessions/YYYYMMDD_*.jsonl`, extracts user+assistant turns (strips tool calls, compaction injections, session_meta), writes `~/.hermes/gbrain-notes/corpus/YYYY-MM-DD.md`.

Flags: `--dry-run` (preview without writing), `--force` (overwrite existing), `--corpus-dir <path>` (override output dir).

**Step 1 (Claude Code variant) — `claude-code-sessions-to-corpus.py`**

The Hermes `sessions-to-corpus.py` only parses Hermes' flat `{role, content, timestamp}`
schema. **Claude Code sessions use a different schema** and will silently yield zero turns
if fed to the Hermes script. Use the dedicated adapter instead:

```bash
python3 claude-code-sessions-to-corpus.py            # all sessions under ~/.claude/projects/
python3 claude-code-sessions-to-corpus.py --dry-run --verbose
python3 claude-code-sessions-to-corpus.py --min-size 2000   # skip tiny/heartbeat sessions
```

Differences from the Hermes script:
- **Schema:** Claude Code records are typed (`type: user|assistant`, plus `file-history-snapshot`,
  `permission-mode`, `ai-title`, `attachment`, `system`, `last-prompt`). `message.content` is a
  **block array** (`text` / `thinking` / `tool_use` / `tool_result`); only `text` blocks are kept.
  A `role: user` record may carry only `tool_result` blocks — those are skipped.
- **Output layout:** one file *per session* at `<corpus_dir>/claude-code-sessions/<YYYY-MM-DD>/<short-id>.md`
  (Claude Code sessions are independent conversations, not a single daily log).
- **Secret scrubbing:** scrubs GitHub/OpenAI/Anthropic/Slack/Supabase tokens, JWTs, AWS keys, and
  Postgres connection-string passwords before writing (the Hermes script does not). Dropping
  `tool_result` blocks also removes most secret-bearing command output.

Corpus dir resolves the same way as the Hermes script: `GBRAIN_CORPUS_DIR` env, else gbrain
config `dream.synthesize.session_corpus_dir`, else `--corpus-dir`.

**Step 2 — Synthesize via Claude Code haiku subagent**

In a Claude Code session, spawn a haiku agent with this prompt template:

```
You are synthesizing a conversation transcript into a personal knowledge brain.

TRANSCRIPT DATE: YYYY-MM-DD
TRANSCRIPT HASH SUFFIX (use in slugs): <first 6 chars of SHA-256 of corpus file>

TRANSCRIPT:
[paste corpus file contents here]

TASK: Decide if this transcript contains anything worth writing to the knowledge brain.

WORTH WRITING:
- Mel articulates a decision, direction, or explicit instruction
- A system upgrade or config change happened with verifiable outcome
- A meaningful operational pattern or constraint is established

NOT WORTH WRITING:
- Pure routine ops with no decision or learning

SLUG RULES:
- Allowed prefixes ONLY: wiki/personal/reflections/, wiki/originals/ideas/, dream-cycle-summaries/
- Format: lowercase alphanumeric and hyphens, slash-separated, no underscores, no extensions
- Include the hash suffix in reflection/original slugs
- Example: wiki/personal/reflections/YYYY-MM-DD-<topic>-<hash6>

OUTPUT FORMAT for each page:
SLUG: wiki/personal/reflections/...
---
[markdown content — may use wikilinks like [[wiki/skills/intern-os]]]
---

If nothing is worth writing, output: NOTHING_TO_WRITE
```

Parse the agent output and write each page to `/tmp/gbrain-synth/<slug>.md` (creating subdirectories as needed).

**Step 2b — Parallel synthesis (multiple days at once)**

When synthesizing several days in a single session, spawn all haiku agents simultaneously rather than sequentially. Claude Code runs them in parallel — total wall time equals the slowest agent, not the sum.

Pattern:
1. Convert all target days first (sequential, fast): `sessions-to-corpus.py --from ... --to ...`
2. Get hashes for all corpus files: `for d in ...; do sha256sum corpus/$d.md | cut -c1-6; done`
3. Pre-read any corpus files >100KB (see large corpus note below)
4. Spawn one haiku Agent per day in a single message — Claude Code runs them concurrently
5. Collect all outputs, write all pages to `/tmp/gbrain-synth/`
6. Run one final `gbrain import` — it skips already-indexed pages automatically

**Large corpus handling (>100KB):** Corpora exceeding ~100KB will hit haiku token limits if passed directly. Pre-summarize first: scan `grep -n "^## Session"` on the corpus file, read 20–30 lines per session header, extract decision-worth events into a ~2KB summary, and pass that to haiku instead of the raw file. Output quality is equivalent.

**Haiku retraction behaviour:** Haiku sometimes produces a page then self-corrects to `NOTHING_TO_WRITE`. Trust the first output if the content is operationally real — haiku over-filters on marginal cases. Verify with `gbrain search` after import.

**Step 3 — Import into gbrain**

```bash
OPENAI_API_KEY=$(grep '^OPENAI_API_KEY=' ~/.hermes/.env | cut -d= -f2-) \
GBRAIN_DISABLE_DIRECT_POOL=1 \
~/.bun/bin/gbrain import /tmp/gbrain-synth/
```

gbrain handles chunking, embedding (via OpenAI), and Supabase upsert. No Anthropic key required. Already-indexed pages are skipped automatically (content-hash check), so it is safe to re-run import after adding new pages to `/tmp/gbrain-synth/`.

### Gotchas (parallel synthesis + import)

Two failure modes that bite when running the pipeline at scale:

**1. Parallel haiku agents flatten slash-paths in filenames.** When you ask each agent to
write `wiki/personal/reflections/<slug>.md`, some agents write a *flat* file named
`wiki-personal-reflections-<slug>.md` (dashes) instead of creating real subdirectories.
Because gbrain derives the page slug from the **directory tree**, a flat filename produces the
wrong slug (`wiki-personal-reflections-...` instead of `wiki/personal/reflections/...`).
Mitigation: have each agent **return the intended slug** (e.g. as JSON), then normalize on disk
before import — move each file to `<corpus>/<slug>.md` with real parent dirs. Don't trust the
filename the agent chose.

**2. `gbrain import <subdir>` strips the namespace.** gbrain roots slugs at the directory you
pass to `import`. So `gbrain import .../corpus/claude-code-sessions/` produces bare slugs like
`2026-05-28/<id>` (no `claude-code-sessions/` prefix), losing the namespace and risking
collisions with other date-partitioned corpora. **Always import from the corpus parent**
(`gbrain import .../corpus/`) so slugs keep their top-level namespace
(`claude-code-sessions/2026-05-28/<id>`). Already-imported pages elsewhere in the tree are
skipped as unchanged, so importing the parent is cheap. If you already imported a subdir, delete
the bare-slug pages (`gbrain delete <slug>`) and re-import from the parent.

### Pre-seeding verdicts (optional)

To skip gbrain's significance judge for corpus files you've already evaluated, use `gbrain-prejudge.ts`:

```bash
GBRAIN_DISABLE_DIRECT_POOL=1 \
bun run ~/.hermes/scripts/gbrain-prejudge.ts --date YYYY-MM-DD [--worth false] [--dry-run]
```

This writes a row to the `dream_verdicts` table in Supabase. gbrain reads the cached verdict and skips the judge — useful if you want to run `gbrain dream` for other phases while bypassing the significance filter.

---

## Env Pattern (all gbrain commands)

**Always extract the API key inline** — do NOT use `source ~/.env` (fails silently in bash subshells when PATH is set after):

```bash
OPENAI_API_KEY=$(grep '^OPENAI_API_KEY=' ~/.hermes/.env | cut -d= -f2-) \
GBRAIN_DISABLE_DIRECT_POOL=1 \
~/.bun/bin/gbrain <command>
```

---

## One-time Setup (new install)

```bash
# 1. Install
bun install -g github:garrytan/gbrain
# NOTE: Do NOT use `bun install -g gbrain` — that installs an unrelated RL library

# 2. Configure connection
gbrain config set database_url "postgresql://postgres.<ref>:<password>@aws-1-us-east-1.pooler.supabase.com:6543/postgres"
gbrain config set openai_api_key sk-...
gbrain config set embedding_model openai:text-embedding-3-large
gbrain config set embedding_dimensions 1536

# 3. Set corpus and brain dirs
gbrain config set dream.synthesize.session_corpus_dir ~/.hermes/gbrain-notes/corpus/
gbrain config set sync.repo_path ~/.hermes/gbrain-notes/brain/

# 4. Tune synthesize settings
gbrain config set dream.synthesize.min_chars 200
gbrain config set models.dream.synthesize_verdict openai:gpt-4o-mini
gbrain config set models.dream.synthesize openai:gpt-4o-mini
gbrain config set agent.use_gateway_loop true --force

# 5. Create dirs
mkdir -p ~/.hermes/gbrain-notes/corpus ~/.hermes/gbrain-notes/brain

# 6. Set permanent env vars
echo "GBRAIN_DISABLE_DIRECT_POOL=1" >> ~/.hermes/.env

# 7. Verify
OPENAI_API_KEY=$(grep '^OPENAI_API_KEY=' ~/.hermes/.env | cut -d= -f2-) \
GBRAIN_DISABLE_DIRECT_POOL=1 \
~/.bun/bin/gbrain health
```

---

## Batch Import Protocol

Run batches smallest → largest. Gate each before advancing: verify pages are searchable, check quality, then proceed.

| Batch | Scope | Sessions | Raw size |
|-------|-------|----------|----------|
| B1 | single sparse day | 1–4 | <300KB |
| B2 | single content day | 1 | ~200KB |
| B3 | small multi-session | 2 | ~400KB |
| B4 | mid-size | 5 | ~1.2MB |
| B5 | heavy | 8 | ~3MB |
| B6 | largest | 17 | ~5MB |

Per-batch steps:
```bash
# Convert
python3 ~/.hermes/scripts/sessions-to-corpus.py --date YYYY-MM-DD --dry-run
python3 ~/.hermes/scripts/sessions-to-corpus.py --date YYYY-MM-DD

# Synthesize (in Claude Code session — spawn haiku Agent with corpus content)

# Import
OPENAI_API_KEY=... GBRAIN_DISABLE_DIRECT_POOL=1 ~/.bun/bin/gbrain import /tmp/gbrain-synth/

# Verify
OPENAI_API_KEY=... GBRAIN_DISABLE_DIRECT_POOL=1 ~/.bun/bin/gbrain stats
OPENAI_API_KEY=... GBRAIN_DISABLE_DIRECT_POOL=1 ~/.bun/bin/gbrain search "<topic from that day>"
```

---

## gbrain v0.41 Worker Pattern (reference)

In v0.41, `gbrain dream` submits jobs to a MinionQueue in Supabase but does NOT execute them. A separate worker must be running:

```bash
# Terminal 1
OPENAI_API_KEY=... GBRAIN_DISABLE_DIRECT_POOL=1 ~/.bun/bin/gbrain jobs work

# Terminal 2
OPENAI_API_KEY=... GBRAIN_DISABLE_DIRECT_POOL=1 ~/.bun/bin/gbrain dream --phase synthesize --date YYYY-MM-DD
```

**This is only needed if using gbrain's native dream synthesize** (which requires Anthropic key anyway). The Claude Code synthesis pipeline (Workaround 2) bypasses this entirely via `gbrain import`.

---

## Helper Scripts

| Script | Purpose |
|--------|---------|
| `~/.hermes/scripts/sessions-to-corpus.py` | Convert Hermes JSONL sessions → dated corpus markdown |
| `~/.hermes/scripts/gbrain-prejudge.ts` | Pre-populate `dream_verdicts` table to bypass significance judge |

---

## Related Skills

- `skills/operations/gbrain-setup/` — initial install procedure
- `skills/operations/gbrain-supabase/` — Supabase migration detail (Hermes Agent format)

## Source

- gbrain source: `~/.opensrc/repos/github.com/garrytan/gbrain/master`
- opensrc registry: OSL-0003
- Validated on: gbrain v0.41.26.1, Supabase free tier, Ubuntu/AWS EC2
