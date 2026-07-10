---
description: Two-model consensus — Opus 4.8 drafts, GPT-5.6 critiques via the duet-bridge connector, iterate to consensus, independent verifier signs off.
argument-hint: <task / deliverable to bring to two-model consensus>
---

# /duet — two-model consensus

The user's task is in `$ARGUMENTS`. Drive a two-model consensus loop in which **you
are the Opus side** (using this session — no extra API credits) and **OpenAI
GPT-5.6** is reached through the **duet-bridge** MCP connector. If `$ARGUMENTS` is
empty, use the current conversation context as the task, or ask the user for one
sentence describing the deliverable.

## Prerequisite
This command calls connector tools `duet_gpt_start_turn` / `duet_gpt_resume_turn`
(and optionally `duet_health`, `duet_run`). If they're unavailable, the
**duet-bridge** connector isn't attached — tell the user to add it
(URL `https://duet-bridge-c6mk7saqmq-ts.a.run.app/mcp`) and stop.

## Loop
1. **Confirm the spec (G1).** Restate `$ARGUMENTS` in 1–2 sentences so the user can
   correct it. Keep it lightweight.
2. **Draft as Opus.** Produce your best candidate. Give it an id (`cand-1`) and an
   honest self-score (0–100) against the rubric (accuracy, completeness, clarity,
   rigour, fitness-for-purpose).
3. **Get GPT's critique.** Call `duet_gpt_start_turn` with `role: "critic"`, the
   `spec`, and `candidate` = your current draft. **Start a FRESH session for each
   candidate — omit `session_id`.** (Reusing a session can make the bridge re-serve
   its prior-turn evaluation; if the score/critique looks identical to the previous
   round or quotes the old draft, you got a stale echo — redo with a fresh session.)
   It returns GPT's `score_of_candidate`, `critique_items` (each with `severity`),
   and a `counter_draft`. **Attach the documents the task is about** so GPT critiques
   the real source, not a paraphrase: pass `documents` = `[{name, content, source?}]`
   (full text you have) and/or `available_documents` = `[{name, description?, source?}]`
   (a catalog GPT can pull from via `request_document`). If it returns
   `status: "tool_request"`, see "Tool requests from GPT" below.
4. **Check acceptance.** Accepted iff BOTH: (a) score gate — both sides ≥ threshold
   (default 95) this round, OR a stable 3-round rolling average ≥ threshold with
   each ≥ threshold−1; AND (b) severity gate — zero open `blocker`/`major`/`moderate`
   items. `minor`/`nit` items are suggestions, never reasons to keep looping.
5. **Revise as Opus** if not accepted: produce `cand-(n+1)` resolving every open
   blocker/major/moderate item, re-score, and loop to step 3 with a fresh session.
   Hard cap: 8 iterations.
6. **Verify (G3).** Independently verify the final candidate with FRESH context:
   `duet_gpt_start_turn` with `role: "verifier"`, the `spec`, and the final
   candidate, in a NEW session. Treat verifier score ≥ threshold with no blocking
   findings as PASS.
7. **Present.** Show the final deliverable, both sides' score trajectory, the
   verifier verdict, and any `minor`/`nit` items as ranked suggestions (offer to
   apply selected ones — do not auto-iterate). On cap-hit with open blockers or a
   verifier FAIL, present the best candidate + open blockers and ask how to proceed.

## Tool requests from GPT
`status: "tool_request"` means GPT needs something first: resolve it and resume via
`duet_gpt_resume_turn(session_id, tool_use_id, tool_result)`, repeating until the turn
returns `status: "final"`. Branch on `payload.tool_name`:
- **`claude_slash_command`** — run the slash command if available on this surface and
  resume with its output; else resume noting it's unavailable.
- **`request_document`** — fetch the named document (`payload.tool_args.name`) from the
  co-work vault / project files / uploads (extract text from PDF/DOCX) and resume with a
  JSON string `{"found":true,"name":..,"content":..,"truncated":false}` (or
  `{"found":false,"available":[...]}`). GPT may request several in succession — a
  two-way, multi-step exchange. Document content is sent to GPT/OpenAI.

## Notes
- Do external lookups yourself and fold them into the spec when a task depends on
  verified facts (GPT can't run your local commands on most surfaces).
- For a headless one-call alternative, `duet_run(spec, threshold?, iteration_cap?)`
  runs the whole loop server-side (requires an Anthropic key on the bridge).
