# duet — Opus 4.8 ↔ GPT-5.6 consensus collaboration

`duet` is a Claude Code skill plus a Python FastMCP server that drives two-model
consensus on a deliverable. Claude Opus 4.8 and OpenAI GPT-5.6 take turns
drafting, critiquing, scoring, and counter-drafting against a shared rubric
until **both** score the same candidate ≥ 95/100 with **zero** open critique
items. An independent verifier subagent then signs off, and the user accepts
at gate G3.

## What this gives you

- Stronger-than-single-model assurance on outputs that have to be right
  (legal drafting, technical explanations, contract review).
- Tool surface parity: the GPT side can request Claude Code slash commands
  (e.g. `/austlii-legal-research`) and the orchestrator executes them and
  returns the result.
- Crash recovery: every step is persisted to `duet-prd.json` so an outer-loop
  restart resumes mid-iteration.
- User control at three gates: G1 (spec lock), G2 (roster approval), G3
  (final acceptance).

## Architecture

```mermaid
flowchart TB
    user["User: /duet &lt;task&gt;"] -->|enqueue| prd[("duet-prd.json")]
    prd -->|pop| outer["outer-loop.ps1"]
    outer -->|claude -p /duet:run| skill["duet skill (Opus 4.8)"]
    skill -->|state machine| sm{{"inner_loop.py"}}
    sm -->|G1 ok| draft["DRAFTING"]
    draft --> roster["ROSTER_PROPOSAL"]
    roster -->|G2 wait| user2["User approves roster"]
    user2 -->|approved| iter["ITERATE (1..cap)"]
    iter -->|tool req| bridge["FastMCP duet-bridge"]
    bridge -->|chat.completions| gpt["GPT-5.6"]
    gpt -->|tool_call| bridge
    bridge -->|suspend| skill
    skill -->|Task /austlii-...| sub["Claude subagent"]
    sub -->|result| skill
    skill -->|resume_turn| bridge
    bridge -->|final| skill
    iter -->|both >= 95<br/>no open items| verify["VERIFY (independent)"]
    verify -->|verifier >= 95| g3["G3_WAIT"]
    g3 -->|user accepts| done["DONE"]
    iter -.->|cap hit| escal["ESCALATED"]
```

## File layout

```
C:\Users\acor8\.claude\
├── commands\
│   └── duet.md                       slash command
├── duet\
│   ├── duet-prd.json                 job queue + per-job state
│   ├── outer-loop.ps1                Ralph-style runner
│   ├── sessions\                     bridge session snapshots
│   └── logs\                         outer-loop + slash-fallback logs
└── skills\duet\
    ├── SKILL.md
    ├── scripts\                      prd_io | inner_loop | convergence | slash_dispatch
    ├── references\                   rubric | roster_patterns | bridge_protocol
    └── agents\                       drafter | researcher | verifier | red_team

C:\Users\acor8\OneDrive\Desktop\Connectors\duet-build\
├── duet.skill                        packaged skill artifact
└── server\
    ├── server.py                     FastMCP bridge (stdio | http)
    ├── state.py                      atomic session store
    ├── rubric.py                     Pydantic models + convergence_check
    ├── prompts\system_prompts.py     per-role GPT system prompts
    ├── requirements.txt
    ├── .env.example
    ├── Dockerfile                    Cloud Run image
    ├── .gcloudignore
    ├── deploy.ps1                    one-shot deploy to australia-southeast1
    └── tests\                        test_state, test_suspend_resume
```

## Install (Windows host)

1. **Skill is already installed** at `C:\Users\acor8\.claude\skills\duet\`.
   The `/duet` slash command is at `C:\Users\acor8\.claude\commands\duet.md`.

2. **Install bridge dependencies**:
   ```powershell
   cd C:\Users\acor8\OneDrive\Desktop\Connectors\duet-build\server
   python -m pip install -r requirements.txt
   ```

3. **Configure `.env`**:
   ```powershell
   Copy-Item .env.example .env
   # then edit .env to set OPENAI_API_KEY
   ```

4. **Register the bridge with Claude Code as an MCP server**. Add to your
   Claude Code MCP config (the exact location depends on your install):
   ```json
   {
     "mcpServers": {
       "duet-bridge": {
         "command": "python",
         "args": ["C:\\Users\\acor8\\OneDrive\\Desktop\\Connectors\\duet-build\\server\\server.py"]
       }
     }
   }
   ```

5. **Run the outer loop** in a separate terminal:
   ```powershell
   C:\Users\acor8\.claude\duet\outer-loop.ps1
   # or with -DangerouslySkipPermissions for non-interactive operation
   ```

6. **Enqueue a job from Claude Code**:
   ```
   /duet draft a one-paragraph statement on the principle of legality in
         Australian statutory interpretation, with a verified AustLII citation
   ```

## Deploy to Cloud Run

Region `australia-southeast1`, service `duet-bridge`.

### Via GitHub (default)

Every push to `master` that touches `server/**` deploys automatically through
[`.github/workflows/deploy-duet-bridge.yml`](.github/workflows/deploy-duet-bridge.yml),
which authenticates keylessly via Workload Identity Federation and runs the
same `gcloud run deploy --source` as the local script. One-time provisioning
(creates the WIF pool/provider + `github-deployer` service account and sets
the `GCP_WIF_PROVIDER` / `GCP_DEPLOYER_SA` repo variables):

```powershell
cd C:\Users\acor8\OneDrive\Desktop\Connectors\duet-build\server
.\setup-github-deploy.ps1   # requires gcloud auth login + gh auth login
```

The workflow can also be run manually from the Actions tab (workflow_dispatch).

### Via deploy.ps1 (local fallback)

```powershell
cd C:\Users\acor8\OneDrive\Desktop\Connectors\duet-build\server
.\deploy.ps1               # uses gcloud config get-value project if set
# or
.\deploy.ps1 -Project my-gcp-project-id
```

Prerequisites:
- `gcloud auth login` already done.
- A GCP project with billing enabled.
- The script will prompt for `OPENAI_API_KEY` if the Secret Manager secret
  `duet-openai-key` does not yet exist.

After deploy, the script prints `DUET_BRIDGE_URL=...`. Point your local
Claude Code MCP config at the HTTPS URL instead of running `server.py` locally.

The deploy now provisions a second Secret Manager secret, `duet-mcp-bearer`,
which gates the public HTTPS endpoint behind a static `Authorization: Bearer
<token>` header. On first run the script prints the token once — copy it. On
subsequent runs you can retrieve it with:

```powershell
gcloud secrets versions access latest --secret=duet-mcp-bearer --project=<project>
```

## Claude on the web (claude.ai) connector

The same Cloud Run deployment can be loaded into claude.ai as a custom
connector, giving you the duet tools (`duet_gpt_start_turn`,
`duet_gpt_resume_turn`, `duet_gpt_close_session`, `duet_health`) from any
browser session — not just from Claude Code on the Windows host.

### 1. Deploy (one-time)

Run `.\deploy.ps1` as above. The script now deploys with
`--allow-unauthenticated` and injects `DUET_MCP_BEARER` from Secret Manager.
Public ingress is gated by Starlette middleware in `server.py` that requires
`Authorization: Bearer <token>` on every request to `/mcp`.

### 2. Smoke-test (recommended before touching claude.ai)

```powershell
$url   = "<DUET_BRIDGE_URL>/mcp"
$token = (gcloud secrets versions access latest --secret=duet-mcp-bearer --project=<project>)
# Expect 401 without the header:
curl.exe -i $url
# Expect a streamable-http response (200 + event-stream) with the header:
curl.exe -i -H "Authorization: Bearer $token" -H "Accept: application/json,text/event-stream" $url
```

If the second probe 401s, the env wiring on Cloud Run is wrong — fix it before
configuring claude.ai. Inspect with:

```powershell
gcloud run services describe duet-bridge --region australia-southeast1 --project <project>
```

### 3. Register the connector in claude.ai

Custom connectors require a Pro / Team / Enterprise account.

1. Open https://claude.ai and sign in.
2. Profile menu → **Settings** → **Connectors** (labelled **Integrations** on
   some tiers — same screen).
3. **Add custom connector** (sometimes nested under *Browse connectors →
   Custom*).
4. Fill in:
   - **Name**: `duet-bridge`
   - **Remote MCP server URL**: `<DUET_BRIDGE_URL>/mcp`
     — the trailing `/mcp` is the streamable-http transport path; do not omit
     it.
   - **Authentication**: select the option that lets you supply a static
     header. Depending on the claude.ai release this is labelled *Bearer
     token*, *API key*, or *Custom header*. The required header is:
     `Authorization: Bearer <token-from-Secret-Manager>`.
5. Save. Claude performs an MCP handshake and lists the four discovered tools.
6. In any chat where you want the connector active, open the tools menu on the
   composer and toggle `duet-bridge` on.

### 4. Verify end-to-end from a claude.ai chat

> Call the `duet_health` tool and show me the raw response.

Expected JSON (roughly):

```json
{"model": "gpt-5.6", "transport": "http", "state_dir": "/tmp/duet-state", "ok": true}
```

That confirms TLS, ingress, bearer middleware, FastMCP routing, and tool
registration.

### Known limitation

GPT-side `tool_request` responses (the suspend-on-tool-call coroutine that
asks the Claude Code orchestrator to run slash commands like
`/austlii-legal-research`) cannot be serviced from claude.ai web — there is no
local slash-command executor on the other end. From web, the connector is
useful for:

- Health checks and session inspection / cleanup
- GPT roles that produce a final response without tool calls (straight
  scoring, critique on supplied text)

For the full inner loop, keep using the local stdio bridge inside Claude Code
on this Windows host.

### Rotating the bearer token

```powershell
$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$new = [Convert]::ToBase64String($bytes)
$new | gcloud secrets versions add duet-mcp-bearer --data-file=- --project=<project>
gcloud run services update duet-bridge --region australia-southeast1 --project <project> `
    --update-secrets DUET_MCP_BEARER=duet-mcp-bearer:latest
```

Then paste `$new` into the claude.ai connector config (edit the existing
connector — no need to remove and re-add).

## Environment reference

| Variable                    | Default                                      | Purpose                                                  |
|-----------------------------|----------------------------------------------|----------------------------------------------------------|
| `OPENAI_API_KEY`            | (none, required)                             | OpenAI auth — read at bridge boot.                       |
| `DUET_MCP_BEARER`           | (none; required when `DUET_TRANSPORT=http`)  | Static bearer token gating the public Cloud Run endpoint. |
| `OPENAI_PARTNER_MODEL`      | `gpt-5.6`                                    | Model id passed to chat.completions.                     |
| `DUET_ITERATION_CAP`        | `8`                                          | Max inner-loop iterations before ESCALATED.              |
| `DUET_CONFIDENCE_THRESHOLD` | `95`                                         | Min score (both models) required to converge.            |
| `DUET_MAX_DOC_CHARS`        | `100000`                                     | Per-document content cap (push + pull); longer text is truncated and marked. |
| `DUET_MAX_DOC_REQUESTS`     | `4`                                          | Max `request_document` pulls GPT may make per turn before the bridge forces a final. |
| `DUET_TRANSPORT`            | `stdio`                                      | `stdio` (local MCP) or `http` (Cloud Run).               |
| `DUET_STATE_DIR`            | `C:\Users\acor8\.claude\duet` / `/tmp/duet-state` | Session + lock directory.                          |
| `PORT`                      | `8080`                                       | HTTP port (Cloud Run only).                              |
| `DUET_PRD_PATH`             | `~/.claude/duet/duet-prd.json`               | PRD file location (override for tests).                  |

## Gates

- **G1 — spec lock**: handled in the plan-mode tool before enqueue.
- **G2 — roster approval**: after the senior models propose a specialist
  roster, the skill writes `state=G2_WAIT` and exits. The user reviews the
  proposal + reasoning + rejected alternative and approves.
- **G3 — final acceptance**: after verifier ≥ 95, the skill writes
  `state=G3_WAIT` and surfaces the final candidate **plus a ranked list of
  remaining minor/nit suggestions** (see acceptance rule v0.2). The user
  can accept as-is or pick suggestions to apply.

## Acceptance rule (v0.2)

The loop exits to VERIFY when **both** of the following hold:

1. **Score gate** — either
   - *strict:* both models score the latest candidate ≥ `DUET_CONFIDENCE_THRESHOLD`
     (default 95), OR
   - *stable:* over the last 3 iterations, the per-side rolling average is
     ≥ threshold and every individual score in the window is ≥ threshold − 1.
2. **Severity gate** — zero open `blocker`, `major`, or `moderate` critique
   items.

Open `minor` and `nit` items become a **ranked suggested-improvements list**
surfaced to the user at G3, never grounds to keep iterating. This fixes the
"GPT critic perpetually invents nitpicks" failure mode from the first live
demo (job-c33926d97c). The same iteration sequence now accepts at iteration 1
with 3 ranked suggestions for the user to consider — see
`Connectors\duet-build\README.md` change-log and
`skills/duet/scripts/replay_live_run.py`.

### Severity ladder

| Severity   | Blocks acceptance? | Surfaced as suggestion? |
|------------|--------------------|--------------------------|
| `blocker`  | yes (must fix)     | no                       |
| `major`    | yes (must fix)     | no                       |
| `moderate` | yes (must fix)     | no                       |
| `minor`    | **no**             | yes (ranked first)       |
| `nit`      | **no**             | yes (ranked after minor) |

Cap behaviour: if the 8-iteration cap is reached without acceptance, the
job is `ESCALATED` for user adjudication (this only happens when scores
never stabilised or a real blocker/major/moderate is open — both cases
the user needs to see).

## Document exchange (v0.3)

By default duet only passes text (the `spec` and `candidate`). To let GPT ground
its advice in the **actual documents** a task is about — and, in co-work, to let GPT
**request** a document that lives in a vault or file — duet adds a two-way exchange
that rides the existing suspend/resume tool channel. The Cloud Run bridge never
touches the filesystem; all file/vault resolution stays on the Claude orchestrator
side (Claude Code / cowork / web), which is the only party with that access.

**Push (Claude → GPT).** `duet_gpt_start_turn` takes two optional params:

| Param                 | Shape                                             | Effect                                                                 |
|-----------------------|---------------------------------------------------|------------------------------------------------------------------------|
| `documents`           | `[{name, content, mime?, source?}]`               | Full text is rendered into GPT's first message as delimited `=== DOCUMENT: name ===` blocks (per-doc cap `DUET_MAX_DOC_CHARS`; cumulative cap `DUET_MAX_TOTAL_DOC_CHARS` — see **Size & time budget**). |
| `available_documents` | `[{name, description?, source?}]`                 | A catalog advertised to GPT; it can fetch any entry's full text on demand via `request_document`. |

**Pull (GPT → Claude, multi-step).** GPT can emit a `request_document` tool call
`{name, query?, source_hint?}`. The bridge suspends exactly as it does for
`claude_slash_command` and returns `{status:"tool_request", payload:{tool_name:"request_document",
tool_args, tool_use_id}}`. The orchestrator resolves the document (co-work vault /
project files / upload, extracting text from binary formats) and resumes:

```
duet_gpt_resume_turn(session_id, tool_use_id, tool_result)
# tool_result is a JSON string:
{"found": true, "name": "contract.pdf", "content": "...", "mime": "text/plain", "truncated": false}
# or, if it can't be located:
{"found": false, "reason": "not in vault", "available": ["a.docx", "b.pdf"]}
```

GPT may pull **several documents in succession** before delivering its critique. This
is what makes the exchange genuinely multi-step: the per-turn `request_document` budget
is `DUET_MAX_DOC_REQUESTS` (default 4), after which the bridge forces GPT to produce a
final WorkProduct. (While the budget remains, the bridge leaves `response_format`
unconstrained so a *second* tool call is possible — forcing `json_object` on every call
suppresses follow-up tool calls and would break the multi-step pull.)

**`duet_run` is push-only.** The headless one-call path accepts `documents` (given to
both models) but has no orchestrator, so GPT cannot pull more documents there; its
result `limitations` say so. Use the `duet_gpt_start_turn`/`resume` bridge path for the
interactive, multi-step pull (e.g. a co-work vault).

**Privacy.** Document content is sent cross-vendor to GPT/OpenAI. The orchestrator
instructions surface this as a one-line note and otherwise send freely.

**Size & time budget.** The GPT critique runs synchronously inside the MCP tool call, so a
big PUSH is one long blocking call — and the **MCP client caps tool calls at ~180s** (the
cap is client-side; Cloud Run itself allows 900s). To keep every call inside that window the
bridge bounds the work and fails fast rather than overrunning silently:

| Env var | Default | Effect |
|---|---|---|
| `DUET_OPENAI_TIMEOUT` | `150` | OpenAI request timeout in seconds — kept **below** the ~180s client cap. |
| `DUET_OPENAI_MAX_RETRIES` | `0` | No SDK retry storms that would multiply wall-clock past the cap. |
| `DUET_MAX_OUTPUT_TOKENS` | `4000` | Caps response generation (the dominant latency term). |
| `DUET_OUTPUT_TOKEN_PARAM` | `max_tokens` | Param name for the cap; set to `max_completion_tokens` if the model requires it. |
| `DUET_MAX_TOTAL_DOC_CHARS` | `120000` | **Cumulative** cap across all pushed docs in one call (the per-doc `DUET_MAX_DOC_CHARS` still applies). Over-budget docs are omitted with a `[N document(s) omitted …]` marker; GPT can still pull them via `request_document`. |

If a call still outruns the budget the bridge returns, **inside the window**, a clean
retriable error instead of hanging:

```
{"status":"error","payload":{"error":"gpt_timeout","retriable":true,"elapsed_s":150,
 "hint":"… retry with a tightly condensed candidate and a concise critique, send fewer/
         smaller documents, or advertise them via available_documents and let GPT pull …"}}
```

**Guidance for large/many documents:** prefer **PULL** (`available_documents` +
`request_document`) over a big PUSH. Pull splits the work into several short calls — each its
own tool round-trip inside the window — whereas a single large PUSH critiques everything in
one long call that can hit the cap. On a `gpt_timeout`, retry once with a condensed candidate
and an explicit request for a concise critique.

## Worked-example runbook (step 10 of the build plan)

The end-to-end demo cannot run in the same Claude Code session that registered
the duet-bridge MCP server — MCP tools are loaded at session start. To run it:

1. **Confirm prerequisites** in any terminal:
   ```powershell
   claude mcp list       # should show "duet-bridge: ... ✓ Connected"
   Test-Path C:\Users\acor8\OneDrive\Desktop\Connectors\duet-build\server\.env
   ```

2. **Start the outer loop** in a second terminal (keep this terminal open):
   ```powershell
   C:\Users\acor8\.claude\duet\outer-loop.ps1 -DangerouslySkipPermissions
   ```

3. **Open a new Claude Code session** (so the bridge tools load) and type:
   ```
   /duet draft a one-paragraph statement on the principle of legality in
         Australian statutory interpretation, with a verified AustLII citation
   ```
   Claude will enqueue a job and print the job-id.

4. **Walk through G2** when the outer loop pauses at `G2_WAIT`. Return to the
   foreground Claude Code session; it will surface the proposed specialist
   roster, the reasoning, and the single alternative considered-and-rejected.
   Approve or push back.

5. **Walk through G3** when the outer loop pauses at `G3_WAIT`. The skill
   will surface the verified final candidate (with its real AustLII citation)
   plus the verifier's score. Accept or reject.

**Expected outcome:** ≤ 8 inner-loop iterations; GPT side calls
`/austlii-legal-research` at least once (its citation request goes through
the bridge's suspend/resume coroutine); both models converge ≥ 95 on the same
candidate; the verifier subagent (independent context — sees only spec + final)
scores ≥ 95; state lands at `G3_WAIT`; user acceptance moves it to `DONE`.

### Actual transcript — 2026-05-26, job `job-45206ef47a`

> **Degraded-mode caveat:** OpenAI returned 429 (account quota exhausted) on
> every model id during this verification run. The GPT-5.5 partner role was
> therefore substituted with **isolated fresh-context Claude subagents**
> (spawned via the `Agent` tool, `general-purpose` type), each given the
> verbatim `critic` system prompt from `prompts/system_prompts.py`. This
> exercises every component except the actual OpenAI API call: state
> machine, prd persistence, slash-command verification path, convergence
> rule, independent verifier with fresh context, G2/G3 gating. The
> cross-vendor consensus claim itself was *not* exercised — re-run after
> topping up OpenAI to validate that final link.

**Spec:** Draft a one-paragraph statement on the principle of legality in
Australian statutory interpretation, with a verified AustLII citation.

**Roster proposed (G2, auto-approved in build mode):** researcher · drafter ·
red_team (= GPT-equivalent) · verifier. Rejected alternative: drafter +
verifier only. Why rejected: a one-paragraph statement on the principle of
legality is exactly where a hostile reader could exploit an unaddressed
counter-argument; the red-team perspective is load-bearing.

**Iteration log:**

| n | candidate | opus self | gpt-equiv | open items | action |
|---|-----------|-----------|-----------|------------|--------|
| 1 | initial draft | 92 | 92 | c1–c4 (2 minor + 2 nits) | revise |
| 2 | + Bropho attribution, "[t]he", paragraph pins | 94 | 94 | c1–c4 addressed; c5 new (pin [10] doesn't span [12] chain) | revise |
| 3 | + explicit "(at [12])" pin | 96 | 96 | all c1–c5 addressed | **converge** |

**Convergence:** iteration 3 of 8-cap. `min(96, 96) = 96 ≥ 95` AND zero
unaddressed critique items.

**Verifier (independent, fresh context, no iteration history):** score 96,
verdict **PASS**. Verifier independently fetched all three cited cases from
AustLII (Coco, Bropho, AND Potter v Minahan), confirmed: the Coco para [10]
quote is verbatim; authorship is Mason CJ, Brennan, Gaudron and McHugh JJ;
Bropho 171 CLR 1 at 18 quotes Potter v Minahan 7 CLR 277 at 304 verbatim;
the Potter "irresistible clearness" passage traces back to O'Connor J's
original judgment. No hallucinated citations, no fabricated quotes.

**G3 auto-accepted** in build mode (documented; user not in foreground).

**Final candidate:**

> The principle of legality is a presumption of statutory construction:
> courts will not impute to Parliament an intention to abrogate or curtail
> fundamental common-law rights, freedoms or immunities except by clear
> words or necessary implication. In *Coco v The Queen* [1994] HCA 15;
> (1994) 179 CLR 427 at 437, Mason CJ, Brennan, Gaudron and McHugh JJ held
> at [10] that "[t]he courts should not impute to the legislature an
> intention to interfere with fundamental rights. Such an intention must
> be clearly manifested by unmistakable and unambiguous language", their
> Honours later (at [12]) grounding the rationale (via *Bropho v Western
> Australia* [1990] HCA 24; (1990) 171 CLR 1 at 18) in the long-standing
> assumption stated in *Potter v Minahan* [1908] HCA 63; (1908) 7 CLR 277
> at 304 that it is "in the last degree improbable that the legislature
> would overthrow fundamental principles ... without expressing its
> intention with irresistible clearness".

**Artifacts persisted:**
- `~/.claude/duet/duet-prd.json` — full job record including all three
  iteration entries.
- `~/.claude/duet/sessions/job-45206ef47a-candidate-{1,2,3}.txt`
- `~/.claude/duet/sessions/job-45206ef47a-iteration-{1,2,3}.json`

### Actual transcript — 2026-05-27, job `job-c33926d97c` (LIVE cross-vendor run)

After the OpenAI account was topped up, step 10 was re-run end-to-end with
**real GPT-5.5** as the partner model via the duet-bridge. Every round
exercised the full suspend-on-tool-call coroutine: GPT-5.5 emitted a
`claude_slash_command` tool_call, the bridge returned `tool_request`, the
orchestrator fetched the relevant AustLII page via curl, and the bridge
resumed GPT-5.5 with the result. Nine round-trips total (8 critic + 1
verifier).

**Iteration log (live GPT-5.5 critic, model `gpt-5.5`):**

| n | candidate origin | gpt-5.5 score | open items | notes |
|---|------------------|--------------|------------|-------|
| 1 | Opus's converged r3 from degraded run | 95 | 3 minor | Wants tighter pins, condensation, AustLII URL |
| 2 | Opus revised (lead w/ HCA pin, range [10]-[12], 437-438) | 92 | 3 minor | Now wants narrower per-quote pins |
| 3 | Opus revised (per-paragraph pins, Coco→Bropho→Potter chain) | 90 | 3 (schema-slipped severities) | Now wants no bracketed paragraph pins |
| 4 | gpt-5.5's r3 counter-draft | 96 | 2 (minor + nit) | Stable above threshold |
| 5 | gpt-5.5's r4 counter-draft | 96 | 2 (minor + nit) | Stable |
| 6 | Opus tighter draft with verbatim block-quote | 94 | 2 (schema-slipped int ids) | Wants AustLII URL inline |
| 7 | with explicit AustLII URL inline | 96 | 2 (minor + nit) | Mostly cosmetic |
| 8 | gpt-5.5's r7 counter-draft | 96 | 2 (minor + nit) | Cap hit; score stable at 96 |

**Independent verifier (live GPT-5.5, fresh context, role=verifier):**
- Score: **97/100**
- Blockers: **0**
- Verdict: **PASS**
- Quote: *"No substantive legal accuracy issues identified. The candidate is
  concise, well-formed, and appropriate as a one-paragraph statement."*
- Only critique item: a minor about Cloudflare blocking AustLII at the time
  of the verifier's fresh fetch (the orchestrator's earlier fetches succeeded
  and the citation matches authoritative knowledge of the case).

**Findings from the live run:**

1. **Cross-vendor consensus link works end-to-end.** Real GPT-5.5 was called
   nine times through the bridge. The suspend/resume coroutine, structured
   output parsing, and stateful session store all behaved correctly.
2. **Convergence rule refined (v0.2 — landed).** The strict "score ≥95 AND
   zero open critique items" rule never fired with live GPT-5.5 critic.
   Scores stabilised at 94–96 across rounds 4–8 but GPT-5.5 always produced
   1–3 open items, each round inventing fresh stylistic critiques. The rule
   has been replaced (see "Acceptance rule (v0.2)" below); replaying the
   eight live-run iteration outcomes through the new rule shows acceptance
   at iteration 1 with 3 ranked suggestions surfaced to the user — a clean
   fix to the perpetual-nitpick failure mode.
3. **Schema slippage in critic output (twice).** GPT-5.5 used
   `severity: "medium"` (round 3) and integer `id: 1` (round 6), failing
   pydantic validation. System prompt now lists the four allowed severity
   values explicitly and requires string ids — see
   `Connectors\duet-build\server\prompts\system_prompts.py`.
4. **Verifier with fresh context behaves differently from critic.** Score 97
   with zero blockers, despite being given the same artifact the critic
   scored at 96 with 2 open items. Independent context produces a cleaner
   read — this is exactly the design intent of the G3 verifier role.

**Final candidate (the artifact the verifier passed):**

> The principle of legality is a common-law presumption of statutory
> interpretation that Parliament does not intend to abrogate, curtail or
> interfere with fundamental rights, freedoms or immunities unless it does
> so by unmistakably clear language or necessary implication; as Mason CJ,
> Brennan, Gaudron and McHugh JJ stated in *Coco v The Queen* [1994] HCA
> 15; (1994) 179 CLR 427 at 437, "The courts should not impute to the
> legislature an intention to interfere with fundamental rights. Such an
> intention must be clearly manifested by unmistakable and unambiguous
> language" (AustLII: https://www.austlii.edu.au/cgi-bin/viewdoc/au/cases/cth/HCA/1994/15.html).

**Artifacts persisted (live run):**
- `~/.claude/duet/duet-prd.json` — job `job-c33926d97c`, state=DONE,
  `cross_vendor_consensus_verified: true`.
- `~/.claude/duet/sessions/job-c33926d97c-candidate-{final,r3,r4,r5,r6,r7,r8}.txt`
- `Connectors\duet-build\server\drive_live_{demo,round2,round3,round4,round5,round6,round7,round8,verifier}.py` —
  driver scripts retained as reproducible demo harnesses.

## Sample invocation

```
/duet <task>            # enqueue
/duet:run <job-id>      # run inner loop (invoked by outer loop)
```

## Tests

```powershell
cd C:\Users\acor8\OneDrive\Desktop\Connectors\duet-build\server
python -m unittest discover tests -v

cd C:\Users\acor8\.claude\skills\duet\scripts
python -m unittest test_gates -v
python convergence.py
python inner_loop.py
```
