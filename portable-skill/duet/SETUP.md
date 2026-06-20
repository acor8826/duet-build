# duet — cross-surface setup

Two things make `/duet`-style consensus work beyond Claude Code: (1) the
**duet-bridge connector** attached to your Anthropic account, and (2) for chat
surfaces, the **`duet_run`** tool enabled with an Anthropic key. This guide covers
both, per surface.

## 0. Get your credentials

- **Connector URL:** `https://duet-bridge-c6mk7saqmq-ts.a.run.app/mcp`
- **Bearer token** (used once at the connector's authorization consent screen):
  ```
  gcloud secrets versions access latest --secret=duet-mcp-bearer --project=asc-router
  ```

The bridge implements OAuth 2.1 with dynamic client registration, so claude.ai's
"Add custom connector" flow works: it registers, then shows a consent page that
asks you to paste the bearer above to authorize.

## 1. Attach the connector

### Desktop app & Web (claude.ai)
1. Settings → **Connectors** → **Add custom connector**.
2. Name: `duet-bridge`. URL: the connector URL above.
3. Click **Connect** → on the consent page, paste the **bearer token** → Authorize.
4. The `duet_*` tools (`duet_run`, `duet_health`, …) are now available in chat.

### Cowork
Cowork uses the same account connectors as web/desktop — once attached above, it
is available there too. Cowork also has a local orchestrator, so the portable
**skill** (below) gives the fullest experience here.

### Mobile (claude.ai app)
Custom connectors on mobile are available on plans that support them (Max / Team /
Enterprise) and follow the same Settings → Connectors flow. Where supported, the
`duet_run` tool works. Note: a literal typed `/duet` **slash command** is not a
mobile-chat feature — invoke it by asking ("run duet on …"), which triggers the
skill/tool.

## 2. (OPTIONAL) Enable the `duet_run` server-side fallback

You do **not** need this for normal use. On every Claude surface (chat, cowork,
mobile, Claude Code) the skill has *you* play the Opus side and only routes the
GPT critique through the bridge — so the loop runs on your own session with **no
Anthropic API credits required**. The funded GPT key on the bridge covers the
OpenAI side.

`duet_run` is only the fallback for **headless / non-Claude callers** that have no
model in the loop. If you want that fallback available, give the bridge an
Anthropic key (note: this consumes Anthropic API credits when used):

```
echo -n "<your-anthropic-api-key>" | gcloud secrets create duet-anthropic-key \
    --data-file=- --replication-policy=automatic --project=asc-router
pwsh ./server/deploy.ps1        # redeploy so Cloud Run mounts the secret
```

`duet_health` then reports `"duet_run_available": true`. (Already done in this
project; activating it just needs credits on the Anthropic account.)

## 2b. (OPTIONAL) Give GPT live Google Drive for case folders

For legal-matter work you can have the GPT side read the case files **from Google
Drive itself**, so you pass only a folder *catalogue* (file names/ids) and GPT opens
the documents to understand the matter before reviewing your candidate. This needs
the bridge deployed on the **Responses-API path** with an OpenAI Google Drive
connector:

```
# 1. Store the connector's OAuth access token as a secret.
echo -n "<google-drive-oauth-access-token>" | gcloud secrets create duet-drive-auth \
    --data-file=- --replication-policy=automatic --project=asc-router

# 2. Redeploy with the connector id + the two case folder ids.
pwsh ./server/deploy.ps1 -DriveConnectorId connector_googledrive `
    -DriveFolderIds "<federal-court-appeal-folder-id>,<supreme-court-case-folder-id>"
```

`duet_health` then reports `"responses_api": true` and `"drive_connector": true`.

**Scoping (important).** The Drive connector uses the `drive.readonly` OAuth scope,
which is **all-or-nothing** — it can read everything the authorized Google account can
see, and there is **no connector setting that limits it to specific folders**. To keep
GPT to just the two case folders, authorize a **dedicated Google account or restricted
shared drive** that only has those folders shared to it. `DUET_DRIVE_FOLDER_IDS` is only
a prompt hint, **not** a security boundary. When the connector is not configured, GPT
falls back to `request_document` (you resolve those as today). The OpenAI connector
field names are env-overridable (`DUET_DRIVE_*`) for forward-compatibility with API
changes.

## 3. Upload the portable skill (recommended) — gives duet in chat + cowork

claude.ai supports custom skills in **chat and Cowork**. Upload this skill:
1. Use the prebuilt `duet-portable.skill` in the repo root (a ZIP of the `duet/`
   folder; rename to `.zip` if the upload dialog filters by extension).
2. claude.ai → **Customize → Skills** → **"+"** → **"+ Create skill"** →
   **"Upload a skill"** → select the ZIP.
3. On Claude Code, instead drop the `duet/` folder into `~/.claude/skills/`.

**How it's invoked once uploaded:** on claude.ai chat, Claude invokes the skill
**automatically when your request matches its description** — i.e. ask "run duet
on …" or type "/duet …" and Claude recognizes it (per Anthropic: *"You don't need
to explicitly invoke them—Claude determines when each skill is needed based on
your request."*). A `/`-type-to-pick menu exists in sidebar contexts (e.g. the
M365 add-in). Claude Code and Cowork support the literal typed `/duet`.

## Surface capability summary

| Surface | What to set up | Needs Anthropic credits? | Invocation |
|---|---|---|---|
| Claude Code | already installed (`~/.claude/skills/`) | no | literal `/duet` |
| Cowork | attach connector + upload skill | no (you are Opus) | `/duet` / intent |
| Web / Desktop chat | attach connector + upload skill | no (you are Opus) | intent-based ("run duet on …") |
| Mobile | attach connector + upload skill (where supported) | no (you are Opus) | intent-based |

So the only per-surface activation is **attach the connector (step 1) + upload the
skill (step 3)** — both account actions, no billing. Anthropic credits are needed
ONLY if you also want the optional headless `duet_run` fallback (step 2).

The one capability that does not cross to non-Claude-Code surfaces is GPT's
ability to call **your** local slash commands mid-loop (e.g.
`/austlii-legal-research`). Do those lookups yourself and fold them into the spec.
