# Concoct architecture

This document records the design analysis done before implementation: what
Fabricate (the conceptual inspiration) does, where its approach falls short, and
how Concoct is structured to address that.

## 1. What Fabricate does

[Fabricate](https://github.com/dabit3/fabricate) is a ~1,000-line Python CLI:

| Module | Responsibility |
| --- | --- |
| `cli.py` | Click commands `generate`, `status`, `list-repos`, `delete` |
| `config.py` | Pydantic settings, language table, complexity profiles |
| `generator.py` | All Claude calls: concept → initial commit → N follow-up commits |
| `git_ops.py` | Write files, commit with back-dated timestamps |
| `github_client.py` | Create repo, force-push, delete |
| `persona.py` | Orchestrates everything and prints a summary |

Observed limitations that Concoct deliberately avoids:

* **No development plan.** Each follow-up commit picks a random commit "type"
  from a progress bucket. There is no narrative, so history drifts.
* **Commits cannot see code.** Follow-up prompts receive only the *list of file
  paths*, never their contents, so later commits routinely contradict or
  overwrite earlier ones.
* **No validation.** Nothing checks that files parse, dependencies are valid or
  tests pass. Parse failures silently fall back to placeholder README commits.
* **GitHub is mandatory.** Even `--no-push` requires a token because the
  orchestrator queries the GitHub user up front.
* **Unsafe remote behaviour.** If a repo name already exists it is *reused* and
  then **force-pushed**, which can destroy unrelated repositories. `list-repos`
  and `delete` operate on every repo the token can see.
* **Secret handling.** The token is embedded in the push URL, and the process
  environment is mutated for commit dates.
* **Non-determinism.** The global `random` module is used without a seed.
* **Monolithic generator** coupled directly to the Anthropic SDK.

## 2. Design goals for Concoct

1. Coherent evolution over volume: plan first, then commits that each operate on
   the *actual* current repository state.
2. Local-first and safe: no network mutation without explicit flags; GitHub
   operations only ever touch repositories Concoct itself created.
3. Verifiable output: a repository is only "successful" once validation passes
   (with bounded, logged repair attempts).
4. Replaceable parts: provider, validators, reporter and GitHub client are all
   injected behind small interfaces so tests never hit external services.

## 3. Module map

```
src/concoct/
├── cli.py                 Click command group (thin: parse → build → call)
├── ui.py                  Rich reporter + tables/panels (implements Reporter)
├── config.py              Settings (pydantic-settings, CONCOCT_ prefix, .env)
│                          and GenerationOptions (validated per-run options)
├── models.py              Pydantic domain models: ProjectSpec, DevelopmentPlan,
│                          PlannedCommit, FileChange, CommitRecord,
│                          ValidationReport, RepositoryResult, Manifest …
├── events.py              Reporter protocol + event types (decouples UI)
├── logging.py             Logging setup with a secret-redacting filter
├── secrets.py             Secret registry, redaction and secret scanning
├── rng.py                 Deterministic per-repository RNG derivation
├── budget.py              Token / cost accounting and hard budget limits
├── languages.py           Language profiles (required files, validators, cmds)
├── manifest.py            Read/write the per-repo Concoct manifest
├── providers/
│   ├── base.py            LLMProvider protocol, LLMRequest/LLMResponse, Usage
│   ├── anthropic.py       Claude provider (the only module importing anthropic)
│   ├── offline.py         Deterministic, key-less provider for demos/CI
│   ├── pricing.py         Per-model price table for cost estimates
│   └── registry.py        Name → provider factory
├── planning/
│   ├── prompts.py         Concept + plan prompts and JSON schemas
│   ├── planner.py         Concept → DevelopmentPlan, normalised to commit bounds
│   └── schedule.py        Deterministic, realistic commit timestamps
├── generation/
│   ├── parsing.py         Robust JSON extraction + schema validation
│   ├── workspace.py       Snapshot of current repo state for prompts; path safety
│   ├── prompts.py         Commit / repair prompts
│   ├── commits.py         CommitGenerator: PlannedCommit + state → FileChanges
│   └── repair.py          Bounded repair loop driven by validation diagnostics
├── gitops/
│   └── repository.py      GitPython wrapper: init, apply changes, dated commits
├── github/
│   └── client.py          PyGithub wrapper: create, push, list, delete (guarded)
├── validation/
│   ├── checks.py          Individual checks (files, syntax, deps, secrets, cmds)
│   └── runner.py          Runs checks on a clean export of HEAD
└── orchestrator.py        RepositoryOrchestrator + BatchRunner (the pipeline)
```

## 4. Pipeline

For each repository (`orchestrator.RepositoryOrchestrator`):

1. **Concept & plan** (`planning.planner`): one LLM call produces a
   `ProjectSpec` (name, description, architecture, stack, features) and a
   `DevelopmentPlan` with exactly *N* `PlannedCommit`s, where *N* is chosen by
   the seeded RNG within `[min_commits, max_commits − repair_reserve]`. The plan
   is validated: first commit must be a scaffold, kinds must be known, count
   must match (one corrective retry, then deterministic normalisation).
2. **Schedule** (`planning.schedule`): *N* monotonically increasing timestamps
   inside the history window, biased toward working hours and weekdays, with
   bursts and gaps; fully determined by the seed.
3. **Commit loop**: for each planned commit, the `CommitGenerator` sends the
   plan, the step, and a snapshot of the *current working tree contents* (the
   result of all previous commits) and receives a list of `FileChange`s
   (`write` full content / `delete`). Changes are path-checked, secret-scanned,
   syntax-checked (one bounded in-step fix attempt), applied and committed with
   the scheduled author/committer date.
4. **Validation** (`validation.runner`): the committed tree is exported with
   `git archive` to a temp dir and checked: required files, syntax, dependency
   metadata, secret scan, then tests/lint in an isolated environment when the
   toolchain exists.
5. **Repair** (`generation.repair`): on failure, up to `max_repair_attempts`
   repair commits are generated from the diagnostics and committed as ordinary
   `fix:` commits after the last timestamp; validation re-runs each time. Never
   unbounded.
6. **Manifest**: stored at `.git/concoct/manifest.json` (inside the git dir, so
   it never enters history) for `inspect`, `list-repos`, `delete`.
7. **Publish** (only with `--push`): create the GitHub repo (fails if the name
   exists), tag it with the `concoct-generated` topic, push `main` without force,
   authenticating through a transient HTTP header so the token never touches
   the remote URL or `.git/config`.

## 5. Safety model

* Default is local generation. `--push` (or `CONCOCT_PUSH=true`) is required for
  GitHub mutation, and a confirmation prompt is shown unless `--yes`.
* `--dry-run` resolves configuration and prints the plan of action without any
  LLM calls, filesystem writes or network mutation.
* GitHub `list-repos`/`delete` only consider repos carrying the
  `concoct-generated` topic *and* owned by the authenticated user; `delete`
  previews the targets and requires `--yes` or interactive confirmation.
* Secrets are `SecretStr`, registered with a redacting log filter, and generated
  files are scanned for secret-like strings (and for the configured secrets
  themselves) before any commit.

## 6. Implementation sequence

1. Project skeleton (uv, ruff, pytest), config, models, logging/secrets.
2. Provider abstraction + Claude provider + offline provider.
3. Planning (planner + schedule) with tests.
4. Git operations with tests.
5. Generation (workspace, parsing, commit generator, repair) with tests.
6. Validation checks + runner with tests.
7. Orchestrator + Rich reporter; vertical slice end to end.
8. GitHub client + `--push` flow, list/delete/inspect/config commands, tests.
9. README, responsible-use documentation, CI workflow.
