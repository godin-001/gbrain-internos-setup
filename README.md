# gbrain-internos-setup

Run gbrain's dream import pipeline **without an Anthropic API key**, using Claude Code as the synthesis engine.

Covers two permanent workarounds for [gbrain](https://github.com/garrytan/gbrain) v0.41+:

1. **Supabase free-tier IPv6 fix** — `GBRAIN_DISABLE_DIRECT_POOL=1`
2. **Claude Code synthesis pipeline** — replaces gbrain's Anthropic-dependent synthesize phase with a haiku Agent + `gbrain import`

## Files

| File | Purpose |
|------|---------|
| `SKILL.md` | Full skill documentation — setup, pipeline, batch protocol, parallelization |
| `sessions-to-corpus.py` | Converts **Hermes** JSONL session files to dated corpus markdown for gbrain |
| `claude-code-sessions-to-corpus.py` | Converts **Claude Code** JSONL sessions (`~/.claude/projects/`) — different schema; also scrubs secrets |
| `gbrain-prejudge.ts` | Pre-populates `dream_verdicts` table in Supabase to bypass significance judge (Bun) |

## Quick start

```bash
# 1. Convert sessions to corpus markdown
python3 sessions-to-corpus.py --date YYYY-MM-DD

# 2. Synthesize (Claude Code session — spawn haiku Agent with corpus content)
#    See SKILL.md Step 2 for the prompt template

# 3. Import into gbrain
OPENAI_API_KEY=$(grep '^OPENAI_API_KEY=' ~/.hermes/.env | cut -d= -f2-) \
GBRAIN_DISABLE_DIRECT_POOL=1 \
~/.bun/bin/gbrain import /tmp/gbrain-synth/
```

See `SKILL.md` for the full pipeline, parallel synthesis pattern, large corpus handling, and one-time setup.

## Tested on

- gbrain v0.41.26.1
- Supabase free tier
- Ubuntu / AWS EC2
