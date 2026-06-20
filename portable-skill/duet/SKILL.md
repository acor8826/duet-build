---
name: duet
description: Two-model consensus collaboration between Claude Opus 4.8 and OpenAI GPT-5.5. Use when the user wants stronger-than-single-model assurance on a deliverable — phrases like "/duet", "run duet", "consensus loop", "iterate with GPT", "have GPT critique this", "second-opinion this", "two-model review", or "get GPT to score this against the rubric". Works on every Claude surface (web, desktop, mobile chat, cowork) via the duet-bridge connector: YOU (the assistant) play the Opus side and call the GPT bridge for the cross-vendor critique, so no extra API credits are needed beyond your own session.
metadata:
  version: 2.0.0
  portable: true
---

# duet — portable two-model consensus

This skill gets a deliverable to two-model consensus quality: **Claude Opus 4.8**
(you, the assistant running this skill) and **OpenAI GPT-5.5** (reached through the
duet-bridge connector) draft, critique, score, and counter-draft against a shared
rubric until both accept the same candidate, after which an independent verifier
signs off.

## Prerequisite: the duet-bridge connector

This skill calls tools exposed by the **duet-bridge** remote MCP connector
(`duet_gpt_start_turn`, `duet_gpt_resume_turn`, `duet_run`, `duet_health`). If
those tools are not available, the connector is not attached — tell the user to
add it (see `SETUP.md`) and stop.

## Primary flow — you are the Opus side (works on ALL Claude surfaces)

This is the default and needs **no Anthropic API credits**: you act as Opus using
the current session; only the GPT side goes through the bridge (the bridge's GPT
key is what funds it). This works in claude.ai chat, Cowork, Claude Code, and
mobile chat — anywhere you can call the connector tools.

1. **Confirm the spec** (gate **G1**). Restate the task in 1–2 sentences so the
   user can correct it. Keep it lightweight.
2. **Draft as Opus.** Produce your best candidate for the spec. Give it a stable
   id (`cand-1`) and assign yourself an honest self-score (0–100) against the
   rubric (accuracy, completeness, clarity, rigour, fitness-for-purpose).
3. **Get GPT's critique.** Call `duet_gpt_start_turn` with
   `role: "critic"`, `spec`, and `candidate` = your current draft. **Always start a
   FRESH session for each candidate — omit `session_id`.** Reusing a session can
   make the bridge re-serve its prior-turn evaluation instead of reading the new
   candidate; if a score/critique looks identical to the previous round or quotes
   the old draft, you got a stale echo — redo with a fresh session. It returns a
   WorkProduct: GPT's `score_of_candidate`, `critique_items` (each with a
   `severity`), and a `counter_draft`. (If it returns `status: "tool_request"`
   instead of `final`, GPT asked for a tool — see "Tool requests" below.)
   - **Attach the documents the task is about.** If the work depends on specific
     files (repo files, attachments, or co-work vault items), don't make GPT critique
     a paraphrase — pass the real source. Use `documents` (a list of
     `{name, content, source?}`) for text you already have, and/or
     `available_documents` (a list of `{name, description?, source?}`) to advertise a
     catalog GPT can pull from on demand via `request_document`. Extract text from
     binary formats (PDF/DOCX) before sending. Document content is sent to GPT/OpenAI.
   - **Inject the matter + case-folder catalogue.** For a legal matter, pass
     `project_name` (the matter name) and `folder_catalogue` — a manifest of the case
     Drive folders, e.g. `[{folder_name: "Federal Court Appeal", folder_id?, files:
     [{name, file_id?, mime?}]}, {folder_name: "Supreme Court Case", ...}]` — on
     **every** turn (critic AND verifier), so GPT knows which matter the request relates
     to and what the record contains. Only the *listing* is sent, not the file bytes.
     When the bridge runs with live Drive (`DUET_USE_RESPONSES_API` + a configured Drive
     connector), GPT opens the listed files from Drive itself and reviews the case to
     understand the matter *before* scoring your candidate — you do **not** push their
     full text. If live Drive is unavailable, GPT falls back to `request_document`.
4. **Check acceptance** (see rule below). If accepted → go to verify.
5. **Revise as Opus.** Otherwise, produce `cand-(n+1)` that resolves every open
   blocker/major/moderate item; re-score yourself. Loop back to step 3 with the
   new candidate. Hard cap: 8 iterations.
6. **Verify** (gate **G3**). Get an independent read of the final candidate with
   **fresh context**: call `duet_gpt_start_turn` with `role: "verifier"`, the
   `spec`, and the final `candidate`, in a NEW session (omit `session_id`). The
   verifier must not be anchored by the loop. Treat a verifier score ≥ threshold
   with no blocking findings as PASS.
7. **Present to the user**: the final candidate; both sides' score trajectory; the
   verifier verdict; and the ranked `minor`/`nit` items as suggested improvements
   (offer to apply selected ones — do **not** auto-iterate on them). On cap-hit
   with open blockers or a verifier FAIL, present the best candidate + open
   blockers and ask how to proceed.

### Acceptance rule
Accepted iff **both**: (a) **score gate** — both sides ≥ `threshold` (default 95)
this round (strict), OR a stable 3-iteration rolling average ≥ threshold with each
score ≥ threshold−1 (stable); AND (b) **severity gate** — zero open
`blocker`/`major`/`moderate` items. Remaining `minor`/`nit` items are
suggestions, never reasons to keep looping.

### Tool requests from GPT
A `status: "tool_request"` from `duet_gpt_start_turn`/`resume` means GPT needs
something before it can finish. Resolve it and continue with
`duet_gpt_resume_turn(session_id, tool_use_id, tool_result)`, and keep resolving
successive requests until the turn returns `status: "final"`. Branch on
`payload.tool_name`:
- **`claude_slash_command`** — GPT asked you to run a slash command (e.g.
  `/austlii-legal-research`). Run it if you can on this surface and resume with its
  output; if you cannot (most chat surfaces), resume with a brief note that the tool
  is unavailable so GPT proceeds with its own analysis. (Do the lookup yourself and
  fold it into the spec when a task truly depends on verified external facts.)
- **`request_document`** — GPT wants the actual text of a document (its name is in
  `payload.tool_args.name`, with optional `query`/`source_hint`). Fetch it from the
  co-work vault / project files / uploads, extract text from binary formats, and
  resume with a JSON string
  `{"found":true,"name":..,"content":..,"mime":"text/plain","truncated":false}`. If
  you can't locate it, resume with `{"found":false,"reason":"...","available":["..."]}`
  so GPT can pick another or proceed. This is the two-way, multi-step exchange: GPT
  may request several documents in succession (e.g. a vault file, then a referenced
  exhibit) before delivering its critique. Document content is sent to GPT/OpenAI.
- **Google Drive (live)** — when the bridge has a Drive connector configured, GPT reads
  the catalogued case files *inside its own runtime*; those reads are resolved by OpenAI
  and do **NOT** come back to you as `tool_request`. Only `claude_slash_command` and
  `request_document` suspend the turn for you to resolve. Case content read this way is
  sent to GPT/OpenAI directly from Drive.

### Large documents & the time budget
The GPT critique runs inside one tool call, and the MCP client caps tool calls at
**~180s**. A big PUSH (large `candidate` + many/large `documents`) critiques everything in
one long call and can hit that cap; the bridge bounds the work and, if a call still outruns
the budget, returns `status:"error"` with `payload.error == "gpt_timeout"` (`retriable:true`)
**inside the window** rather than hanging.
- **Prefer PULL for large/many docs.** Advertise them via `available_documents` and let GPT
  fetch what it needs with `request_document` — that splits the work into several short calls,
  each its own round-trip inside the window — instead of pushing everything in one long call.
- **On a `gpt_timeout` error, retry once**: re-send a tightly condensed `candidate` with an
  explicit request for a *concise* critique, and/or move large `documents` into
  `available_documents` so GPT pulls them. That reliably returns inside the window.
- Pushed docs are bounded by a cumulative size budget; any over-budget ones are dropped with
  a `[N document(s) omitted …]` marker and can still be pulled via `request_document`.

## Fallback flow — one-call `duet_run` (for non-Claude orchestrators)

If for some reason you cannot act as the Opus side (e.g. an automated/headless
caller with no model in the loop), call `duet_run(spec, threshold?, iteration_cap?)`,
which runs the whole loop server-side. This requires the bridge to have an
Anthropic key (`duet_health` → `duet_run_available: true`); it returns
`final_candidate`, scores, `verifier`, and `suggested_improvements`. Prefer the
primary flow on any real Claude surface — it needs no extra credits and gives you
direct control of the Opus role.

## Notes
- Crash-recovery via a local job queue (the Claude-Code version) is not available
  here; if a long run is interrupted, restart it.
- The literal typed `/duet` works in Claude Code and Cowork; in claude.ai chat,
  Claude invokes this skill when your request matches its description.
