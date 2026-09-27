# Concoct

**Generate realistic synthetic software repositories, with coherent, incremental Git histories, for research, testing, demos and AI engineering experiments.**

Concoct asks Claude to design a small but real project, plans its development
as a sequence of commits, and then builds the repository one commit at a time.
Each commit is generated from the repository's *actual current contents*. Every
commit is dated in a plausible schedule, the finished project is validated
(parsing, dependency metadata, install, tests, lint), and failures are
repaired within a bounded budget. Repositories stay on your machine unless you
explicitly ask Concoct to publish them to GitHub.

> [!IMPORTANT]
> **Responsible use.** Concoct produces *synthetic* activity. Do not present
> generated repositories or commit history as genuine professional experience,
> use them in job applications or portfolios, or use them to mislead anyone
> about who wrote what or when. By default, Concoct adds a notice to each README
> saying the repository is synthetic, and it commits under a synthetic identity
> rather than yours. See [Responsible use](#responsible-use).

---

## Contents

- [Features](#features)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Examples](#examples)
- [CLI reference](#cli-reference)
- [How it works](#how-it-works)
- [Validation and repair](#validation-and-repair)
- [GitHub integration and safety](#github-integration-and-safety)
- [Costs and budgets](#costs-and-budgets)
- [Reproducibility](#reproducibility)
- [Architecture](#architecture)
- [Development](#development)
- [Responsible use](#responsible-use)
- [Acknowledgements](#acknowledgements)

## Features

- **Plan first, then build.** Every repository gets a concept (name,
  description, architecture, features) and a commit-by-commit development plan
  covering scaffolding, features, tests, refactors, bug fixes, docs, CI and
  dependency changes.
- **Coherent evolution.** Each commit is generated against the full current
  working tree, so later commits build on the code earlier commits created. A
  planned `fix:` repairs a defect that actually exists in the earlier code.
- **Real Git history** built with GitPython: realistic Conventional Commit
  messages, timezone-aware historical author and committer dates (bursty,
  weekday- and working-hour-biased), a valid author identity and a `main`
  branch. History is preserved exactly when pushed.
- **Validation before success:** required files, syntax (Python, JSON, TOML,
  YAML), dependency metadata, secret scanning, then isolated install, tests and
  lint where the toolchain exists. Failures produce diagnostics and trigger
  **bounded** repair commits.
- **Safe by default.** Local generation is the default. GitHub changes need
  `--push`, show a preview and ask for confirmation. Existing repositories are
  never reused, overwritten or force-pushed, and only Concoct-tagged
  repositories can be listed or deleted.
- **Provider abstraction.** Claude is the default provider. The orchestration
  never imports the Anthropic SDK. A deterministic **offline provider** runs
  the whole pipeline with no API key.
- **Budgets and visibility.** Live token and cost accounting, hard cost and
  token limits, and a Rich UI that shows the plan, the current phase, each
  commit as it lands, validation results and a final summary.
- **Secrets stay secret.** Keys are `SecretStr` values. They are redacted from
  logs and errors, stripped from the environment of any generated code Concoct
  runs, never written to remotes or `.git/config`, and any generated file that
  contains a credential-like string is rejected before commit.

## Installation

Concoct needs Python 3.12+, Git and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/leeclarkuk/concoct.git
cd concoct
uv sync                      # creates .venv with Concoct and dev tools
uv run concoct --help
```

To install it as a standalone tool:

```bash
uv tool install .            # then just: concoct --help
```

## Quick start

**1. Try it without an API key.** The offline provider uses a built-in,
deterministic FastAPI template, so you can see the full pipeline (plan,
incremental commits, validation) at no cost:

```bash
uv run concoct generate --provider offline --min-commits 8 --max-commits 15
uv run concoct inspect hooklens
```

**2. Generate with Claude.**

```bash
uv run concoct config init            # writes a commented .env (mode 600)
# edit .env and set CONCOCT_ANTHROPIC_API_KEY=sk-ant-...
uv run concoct generate --language python --tech fastapi \
    --category "developer tool" --repos 1 --min-commits 8 --max-commits 15 --no-push
```

This writes `concoct-output/<name>/`: a normal Git repository you can `cd` into,
`git log`, test and run.

## Configuration

Every setting can come from a CLI option, an environment variable with the
`CONCOCT_` prefix, or a `.env` file. The order of precedence, highest first, is:

**CLI option → environment variable → `.env` → default**

`concoct config show` prints the effective configuration (secrets are shown
only as "set"/"not set"). `--env-file PATH` reads a different `.env` file.
List values accept comma-separated strings, for example
`CONCOCT_LANGUAGES=python,go`.

| Environment variable | CLI option | Default | Description |
| --- | --- | --- | --- |
| `CONCOCT_ANTHROPIC_API_KEY` (or `ANTHROPIC_API_KEY`) | – | – | Claude API key |
| `CONCOCT_GITHUB_TOKEN` | – | – | GitHub token. Only needed for `--push`, `list-repos --remote` and `delete --remote` |
| `CONCOCT_GITHUB_OWNER` | – | token user | Organisation to create repositories in |
| `CONCOCT_PROVIDER` | `--provider` | `anthropic` | `anthropic` or `offline` |
| `CONCOCT_MODEL` | `-m, --model` | `claude-opus-5` | Model identifier |
| `CONCOCT_EFFORT` | `--effort` | `high` | Claude reasoning effort (`low` … `max`) |
| `CONCOCT_LANGUAGES` | `-l, --language` | `python` | Languages. Repositories cycle through them |
| `CONCOCT_TECHNOLOGIES` | `-t, --tech` | – | Frameworks and technologies every project must use |
| `CONCOCT_CATEGORIES` | `-c, --category` | built-in list | Free-form project categories |
| `CONCOCT_REPOS` | `-r, --repos` | `1` | Number of repositories |
| `CONCOCT_COMPLEXITY` | `--complexity` | `medium` | `low`, `medium`, `high` or `random` |
| `CONCOCT_LICENSE` | `--license` | – | Add a licence, for example `MIT` |
| `CONCOCT_HISTORY_DAYS` | `-d, --history-days` | `180` | How far back history may start |
| `CONCOCT_END_DATE` | `--end-date` | now | Latest possible commit time |
| `CONCOCT_MIN_COMMITS` / `CONCOCT_MAX_COMMITS` | `--min-commits` / `--max-commits` | `8` / `20` | Commits per repository, including repair commits |
| `CONCOCT_TIMEZONE` | `--timezone` | `UTC` | IANA timezone for commit times |
| `CONCOCT_AUTHOR_NAME` / `CONCOCT_AUTHOR_EMAIL` | `--author-name` / `--author-email` | `Concoct Synthetic <synthetic@concoct.invalid>` | Commit identity |
| `CONCOCT_SEED` | `--seed` | random (printed) | Seed for reproducible choices and timestamps |
| `CONCOCT_MAX_COST_USD` | `--max-cost` | `10` | Hard stop on estimated spend |
| `CONCOCT_MAX_TOTAL_TOKENS` | `--max-tokens` | – | Hard stop on tokens used |
| `CONCOCT_MAX_REPAIR_ATTEMPTS` | `--max-repairs` | `2` | Repair commits allowed per repository |
| `CONCOCT_RUN_COMMANDS` | `--run-commands/--no-run-commands` | `true` | Install and run tests and lint during validation |
| `CONCOCT_STRICT_LINT` | `--strict-lint` | `false` | Treat lint findings as failures |
| `CONCOCT_COMMAND_TIMEOUT` | – | `600` | Seconds per validation command |
| `CONCOCT_SYNTHETIC_DISCLOSURE` | `--disclosure/--no-disclosure` | `true` | Synthetic-origin notice in README |
| `CONCOCT_OUTPUT_DIR` | `-o, --output-dir` | `concoct-output` | Where repositories are written |
| `CONCOCT_VISIBILITY` | `--visibility` | `private` | GitHub visibility when pushing |
| `CONCOCT_PUSH` | `--push/--no-push` | `false` | Publish to GitHub |
| `CONCOCT_CLEANUP` | `--cleanup` | `false` | Remove local copies after a successful push |
| `CONCOCT_ANTHROPIC_BASE_URL` | – | SDK default | Custom API endpoint |
| `CONCOCT_REQUEST_TIMEOUT` | – | `600` | Seconds per model request |
| `CONCOCT_LOG_LEVEL` / `CONCOCT_LOG_FILE` | `--log-level` / `--log-file` | `WARNING` / – | Diagnostics (always redacted) |

> Never commit `.env`. It holds credentials. `concoct config init` creates it
> with mode `600`, and Concoct's own `.gitignore` excludes it.

## Examples

```bash
# The canonical vertical slice: one FastAPI developer tool, 8–15 commits, local only
concoct generate --language python --tech fastapi --category "developer tool" \
    --repos 1 --min-commits 8 --max-commits 15 --no-push

# Preview decisions (languages, categories, commit counts, schedule window) at no cost
concoct generate -l python -l go -r 4 --complexity random --seed 7 --dry-run

# A reproducible batch across languages, with a year of history in London time
concoct generate -l python -l typescript -l go -r 3 -d 365 \
    --timezone Europe/London --seed 42 --license MIT

# Cheaper, faster generation with a hard budget
concoct generate -m claude-sonnet-5 --effort medium --max-cost 2 --complexity low

# Publish to GitHub (private by default); preview and confirmation are shown first
export CONCOCT_GITHUB_TOKEN=ghp_...
concoct generate -l rust -c "command-line utility" --push

# Publish into an organisation, skip the prompt, remove local copies afterwards
CONCOCT_GITHUB_OWNER=my-research-org concoct generate --push --yes --cleanup

# Inspect, list and clean up
concoct list-repos                       # local, from manifests
concoct list-repos --remote              # GitHub repos tagged concoct-generated
concoct inspect hooklens                 # plan, history, validation, usage
concoct delete hooklens --dry-run        # preview
concoct delete hooklens --remote         # local + GitHub (asks to confirm)
```

## CLI reference

```
concoct [--env-file PATH] COMMAND [OPTIONS]
```

| Command | Purpose |
| --- | --- |
| `generate` | Plan, build, validate and optionally publish repositories |
| `status` | Show configuration, credentials present, tooling and GitHub connectivity (read-only) |
| `list-repos` | List Concoct repositories: local (default) or `--remote`; `--json` for scripts |
| `inspect TARGET` | Show a repository's spec, architecture, plan, Git history, validation and token usage; `--json` prints the manifest |
| `delete NAMES…` | Delete Concoct repositories locally (default) and/or `--remote`, with preview, `--dry-run` and `--yes` |
| `config show` | Effective configuration with the environment variable for each setting |
| `config init` | Write a commented `.env` template |

`generate` options (see `concoct generate --help`):

| Option | Meaning |
| --- | --- |
| `-l/--language`, `-t/--tech`, `-c/--category` | What to build (repeatable) |
| `-r/--repos`, `--complexity` | How many, how big |
| `-d/--history-days`, `--end-date`, `--timezone` | Where commits fall in time |
| `--min-commits`, `--max-commits` | Commits per repository (repairs count towards the maximum) |
| `--author-name`, `--author-email` | Commit identity |
| `--license` | Add a licence file |
| `--provider`, `-m/--model`, `--effort` | Which model generates the code |
| `--max-cost`, `--max-tokens` | Hard budget |
| `--seed` | Reproducibility |
| `--max-repairs`, `--run-commands/--no-run-commands`, `--strict-lint` | Validation behaviour |
| `--disclosure/--no-disclosure` | README notice that the repository is synthetic |
| `-o/--output-dir` | Local destination |
| `--push/--no-push`, `--visibility`, `--cleanup`, `-y/--yes` | Publishing |
| `--dry-run` | Resolve and display everything, with no model calls, writes or network changes |
| `-v/--verbose`, `--log-level`, `--log-file` | Diagnostics |

Exit codes: `0` when every repository succeeded, `1` when any repository
failed or a confirmation was declined, `2` for usage or configuration errors.

## How it works

```
                ┌──────────────┐   seed → language, category, complexity, commit count
 settings ────► │  assignment  │
                └──────┬───────┘
                       ▼
                ┌──────────────┐   concept + N planned commits (validated, 1 retry
                │   planner    │   with feedback, deterministic normalisation)
                └──────┬───────┘
                       ▼
                ┌──────────────┐   N increasing, timezone-aware timestamps
                │   schedule   │   (bursty, weekday/working-hour biased)
                └──────┬───────┘
                       ▼
          ┌─────────────────────────┐  for each planned commit:
          │ commit generator + git  │   snapshot working tree → model returns full
          │ (repeat N times)        │   file contents → screen (paths, secrets,
          └───────────┬─────────────┘   syntax; 1 retry) → apply → dated commit
                      ▼
          ┌─────────────────────────┐  git archive HEAD → temp dir → files, syntax,
          │       validation        │  dependencies, secrets → venv install →
          └───────────┬─────────────┘  pytest → ruff
                 fail │ pass
                      ▼
          ┌─────────────────────────┐  ≤ max-repairs "fix:" commits from diagnostics,
          │      bounded repair     │  re-validated each time
          └───────────┬─────────────┘
                      ▼
          manifest (.git/concoct/manifest.json) → optional GitHub publish
```

Design points:

- **The repository is the source of truth.** A commit prompt contains the
  project brief, the whole plan (so the model knows what *not* to do yet) and
  the complete current contents of the repository, within a context budget
  that prioritises the files the step touches. Models return **full file
  contents**, never diffs, so applied changes are exact.
- **Prompt caching.** The brief and plan form a stable prefix marked for
  caching, so the second and later calls for a repository reuse it.
- **Commit counts are honoured.** The planner asks for exactly *N* commits,
  where *N* is chosen by the seeded RNG within `[min, max − repair reserve]`.
  If the plan has the wrong length, the planner retries once with feedback and
  then trims low-value commits. Repair commits use the reserve, so totals stay
  within `--max-commits`.
- **Nothing is overwritten.** Output directories get a suffix if the name is
  taken, and names already on GitHub are avoided at planning time.
- **The manifest lives inside `.git`.** It records the options (without
  secrets), spec, plan, commits, validation and usage for `inspect`,
  `list-repos` and `delete`, and never appears in history.

### The offline provider

`--provider offline` swaps Claude for a deterministic template: *hooklens*, a
FastAPI developer tool that captures and inspects webhooks. Its 16-stage history
includes a deliberately introduced eviction bug that a later `fix:` commit
repairs with a regression test. Every file is rendered from the set of stages
applied so far, so any subset of stages (chosen to fit your commit range)
produces a history in which **every commit installs and passes its tests**.
Use it for demos, CI, and trying Concoct without spending tokens. It supports
Python only, up to 16 commits.

## Validation and repair

Validation runs on a clean `git archive` of `HEAD` in a temporary directory, so
it sees exactly what history contains and never pollutes the repository.

| Check | What it verifies | Blocking |
| --- | --- | --- |
| required files | README, `.gitignore`, a dependency manifest, source files, tests, and a LICENSE if requested | yes |
| syntax | Python (AST), JSON, TOML, YAML parse | yes |
| dependency metadata | `pyproject.toml` (PEP 621, requirement syntax, build system), `requirements.txt`, `package.json`, `go.mod`, `Cargo.toml`; warns if a requested technology is not declared | yes |
| secrets | No credential-like strings, and none of your configured secrets | yes |
| install | Python: `uv venv` and `pip install -e .[dev]` | yes |
| tests | Python: `pytest`. Go: `go vet` and `go test`. Rust: `cargo test`. Node: `npm install` and `npm test` (when the toolchain is installed) | yes |
| lint | `ruff check` | only with `--strict-lint` |

If validation fails, the diagnostics (failing test output, parse errors, and
so on) go back to the model, which proposes a fix. The fix is committed as a
normal `fix:` commit shortly after the last one, and validation runs again.
This happens at most `--max-repairs` times, so it never loops indefinitely. A
repository that still fails is reported as `validation_failed`, with its
diagnostics kept in the manifest.

> **Validation runs generated code.** Installing and testing a generated
> project executes code a model wrote, with your user's permissions. Concoct
> strips credentials (`*TOKEN*`, `*KEY*`, `*SECRET*`, `CONCOCT_*`,
> `ANTHROPIC_*`, `GITHUB_*`, …) from its environment, but it is not a sandbox.
> Use `--no-run-commands` to limit validation to static checks, or run Concoct
> inside a container or VM.

## GitHub integration and safety

GitHub is only contacted when you ask for it:

| Action | Requirement |
| --- | --- |
| Create and push repositories | `--push` (or `CONCOCT_PUSH=true`), a token, and interactive confirmation unless `--yes` |
| List remote repositories | `list-repos --remote` |
| Delete remote repositories | `delete NAME --remote`, a preview, and confirmation unless `--yes` |

Guarantees:

- **Never reuses or overwrites a repository.** If the name exists, creation
  fails. Pushes are plain fast-forward pushes of `main` to a new, empty
  repository, never forced.
- **Only touches its own repositories.** Created repositories get the
  `concoct-generated` and `synthetic-data` topics. `list-repos --remote` and
  `delete --remote` ignore anything without the marker topic or outside the
  configured owner.
- **Rolls back half-created repositories.** If tagging or pushing fails, the
  just-created empty repository is deleted and the local copy is kept.
- **Tokens never touch disk.** Pushes authenticate with a transient HTTP
  header passed through Git's environment configuration. The remote URL and
  `.git/config` never contain the token.
- **Visibility defaults to private.**

The token needs the `repo` scope (classic) or *Administration: write* and
*Contents: write* (fine-grained). Deleting remote repositories also needs
`delete_repo`.

## Costs and budgets

Concoct estimates spend from the model's list prices (see
`src/concoct/providers/pricing.py`), counting cached input at the reduced
rate. It stops before starting a new call once `--max-cost` or `--max-tokens`
is reached. The repository in progress is marked `budget_exceeded` (its
partial history and manifest are kept) and remaining repositories are skipped.
Live usage appears in the progress bar and per-repository summaries.

Cost grows with the number of commits times the size of the repository,
because every commit call includes the current repository contents. The brief
and plan prefix is cached. Use `concoct generate --dry-run` to check the plan
of action first, and `--complexity low`, fewer commits, a smaller model
(`-m claude-sonnet-5`) or `--effort medium` to reduce cost.

## Reproducibility

`--seed` (printed on every run) fixes each repository's language, category,
complexity, commit count and complete timestamp schedule. Each repository's
randomness is derived independently from the seed, so adding repositories
doesn't change earlier ones. With the offline provider and a fixed
`--end-date`, runs are **bit-for-bit reproducible**: same files, dates and
author give identical commit SHAs. Model output itself is not deterministic.

## Architecture

```
src/concoct/
├── cli.py              Click commands (thin)          ├── planning/
├── ui.py               Rich reporter and tables       │   ├── prompts.py   plan prompt and schema
├── config.py           pydantic-settings (CONCOCT_)   │   ├── planner.py   concept → validated plan
├── models.py           Pydantic domain models         │   └── schedule.py  commit timestamps
├── events.py           Reporter interface             ├── generation/
├── orchestrator.py     pipeline + bounded repair      │   ├── workspace.py repo state → prompt
├── budget.py           token/cost limits              │   ├── prompts.py   commit/repair prompts
├── secrets.py          redaction and secret scanning  │   ├── parsing.py   robust JSON extraction
├── logging.py          redacting log setup            │   └── commits.py   step → screened changes
├── manifest.py         .git/concoct/manifest.json     ├── gitops/repository.py  GitPython wrapper
├── languages.py        per-language conventions       ├── github/client.py      PyGithub, guarded
├── rng.py              deterministic RNG derivation   ├── validation/           checks + runner
└── providers/          base protocol, anthropic.py (only SDK import), offline.py, pricing, registry
```

To add a provider, implement the `LLMProvider` protocol, a `name`, a `model`
and `complete(LLMRequest) -> LLMResponse`, then register it in
`providers/registry.py`. The request carries the system prompt, a cacheable
`prompt_prefix`, the prompt, an optional JSON schema and structured `context`.
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) covers the design analysis,
including what Concoct does differently from Fabricate.

## Development

```bash
uv sync
uv run pytest                                  # unit tests (external APIs mocked)
CONCOCT_RUN_INTEGRATION=1 uv run pytest        # also runs a real install+pytest validation
uv run ruff check . && uv run ruff format --check .
uv run concoct generate --provider offline     # end-to-end smoke test, no API key
```

The tests cover configuration precedence, planning and normalisation, the
schedule's statistical properties, Git history (dates, authors, incremental
diffs, exports), generation retries and screening, validation checks, the
Claude provider's request shape and error handling (with a fake client), the
budget, GitHub operations (mocked PyGithub plus real pushes to a local bare
repository), orchestration including repair and failure paths, and the CLI.

## Responsible use

Concoct is a tool for **synthetic data**. Legitimate uses include:

- research on code generation, repository mining and AI-generated code detection;
- test fixtures for tools that analyse repositories, Git history or CI;
- demos, workshops and product screenshots that need realistic but fake projects;
- evaluation datasets for coding agents.

Do **not** use Concoct to:

- present generated repositories or activity as your own work or experience,
  for example in CVs, job applications, portfolios or contribution graphs;
- impersonate real people. Don't set `--author-name` or `--author-email` to
  someone else's identity;
- inflate reputation metrics, game platforms, or deceive anyone about the
  provenance of code.

Defaults that support this: a README notice on every generated repository
(`--no-disclosure` removes it for fixtures where the notice would interfere,
so use it knowingly), a synthetic commit identity, private visibility, and
`concoct-generated` and `synthetic-data` topics on anything published.

## Acknowledgements

Concoct is an independent implementation inspired by
[dabit3/fabricate](https://github.com/dabit3/fabricate), which explored
generating repositories with Claude. Concoct differs in approach: it plans
first, builds commits against the real repository state, validates and
repairs, stays local by default, and treats GitHub mutation defensively.

## Licence

MIT. See [LICENSE](LICENSE).
