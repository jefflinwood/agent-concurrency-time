# Agent concurrency timing

A Python harness for measuring delivery of ten Ridgeline Coffee changes with
**1, 3, 5, or 10 total concurrent agents**, three trials per setting. Implementation
and integration share the same agent budget. Everything happens in local Git;
the harness does not push branches or open pull requests.

The primary outcome is elapsed wall time from first task dispatch through final
verification of all ten changes on that run's `main`. Total elapsed time includes
clone, environment setup, and baseline verification and is reported separately.
An unsuccessful run has a failure outcome, never a successful completion time.

## Protocol

1. Create a new clone for each run and check out the full SHA in
   [benchmark/config.json](benchmark/config.json). The default SHA was read from
   the local Ridgeline clone; public-remote availability still needs preflight
   verification. Local uncommitted changes are never included.
2. Create a fresh virtual environment, install Ridgeline's dev requirements with
   the published constraints, record the installed dependency versions, and run
   the baseline suite. A setup or baseline failure is a recorded failed run.
3. Dispatch the fixed task list in ID order, with at most N jobs active. Each task
   receives a separate branch/worktree starting at the latest passing `main`
   **at dispatch time**. Concurrent tasks therefore diverge; sequential tasks
   benefit from earlier integrated work. This policy is part of the treatment.
4. Once an implementation passes its acceptance checks and the Python tests,
   commit its changes and enqueue it for integration. Integration has priority
   whenever a slot is free, without interrupting an active implementation.
   Integration is serialized and uses one slot from the same N-slot pool.
5. Merge into a separate candidate worktree. Launch a fresh integration agent
   even for a clean merge. It resolves conflicts and repairs regressions. Run
   original baseline tests, candidate tests, and every integrated acceptance
   contract plus the incoming contract. Advance `main` only after passing.
6. Allow two fresh integration attempts per task. Failed implementations are
   not retried. Exhausted repairs leave `main` unchanged; other tasks continue.
   Finally run all ten contracts regardless of earlier failures.

Every implementation and repair invocation uses `codex exec --ephemeral`, a new
prompt, and no resumed session. Integration retries receive the current files,
contracts, and previous validation failure explicitly. No conversation state is
retained. Nested agents are disabled and prohibited in prompts. There is no
extra planner or uncounted integration agent. Test and Git subprocesses consume
machine resources but are not agents. An agent slot remains occupied during its
job's Git and validation operations; this scheduling overhead is included.
A fully successful run uses at least 20 fresh invocations (10 implementation,
10 integration), plus any repair retries; N limits simultaneous work, not total
invocations.

One trial block contains each concurrency setting once in a seeded random order.
There are three blocks, for 12 runs total. Runs execute sequentially. Task order
stays fixed to isolate concurrency. If multiple jobs finish together, the
scheduler processes that completed batch in task ID order. Otherwise integration
uses completion order. Three trials provide exploratory evidence; publish the
individual results and failures alongside any median.

## Workload v1

The ten tasks implement computed model properties and expose them through
existing authenticated API serializers. Each also adds focused Python tests and
README documentation. They require no migrations or dependencies. Exact rules,
rounding, null handling, and API paths are in
[benchmark/tasks.json](benchmark/tasks.json).

| ID | Feature | Shared model / API serializer |
| --- | --- | --- |
| 01 | Finished coffee stock valuation | Product / product_to_dict |
| 02 | Finished coffee restock signals | Product / product_to_dict |
| 03 | Green coffee remaining stock value | GreenCoffeeLot / lot_to_dict |
| 04 | Roast weight loss reporting | RoastBatch / batch_to_dict |
| 05 | Order shipment coverage | Order / order_to_dict |
| 06 | Order days overdue | Order / order_to_dict |
| 07 | Customer delivered revenue | Customer / customer_to_dict |
| 08 | Customer active subscription quantities | Customer / customer_to_dict |
| 09 | Truck shipment capacity utilization | Shipment / shipment_to_dict |
| 10 | Subscription normalized monthly demand | Subscription / subscription_to_dict |

All tasks touch `app/models.py`, `app/api.py`, and `README.md`. Tasks 01/02,
05/06, and 07/08 also share classes and serializer bodies. Nested order and
account serialization crosses additional task boundaries. Git still may merge
some edits cleanly; the benchmark records actual conflicts rather than forcing
them. The workload is intentionally narrow: results describe additive model/API
work with shared files, not general software engineering or migration-heavy work.

[benchmark/acceptance.py](benchmark/acceptance.py) contains 36 public contract
cases. They run from an evaluation directory outside agent worktrees, alongside
an immutable copy of the original tests. The runner hashes those files before
each check. Candidate tests also run, so tests added by agents participate in the
gate. Fixtures come from the committed baseline plus benchmark-specific data.
Public checks are not a security boundary against a deliberately cheating agent;
README quality and quality of agent-authored tests require review. The automated
completion criterion is functional acceptance plus regression tests.

## Run

Prerequisites: Git, Python 3.11 or later, the configured Codex CLI version, working Codex
authentication, network access to clone and install packages, and access to the
configured Terra model. The harness itself uses only Python's standard library.
`--ignore-user-config` avoids inherited user model/config defaults; credentials
still come from the existing Codex authentication setup. The model is never
silently substituted. Run artifacts may contain source code and agent output;
credentials are not copied into results.

From this directory:

```sh
python3 -m concurrency_harness.cli plan
# Run one scheduled entry as a pilot, in a separate output directory:
python3 -m concurrency_harness.cli run --run-index 0 --output results/pilot
# After preflight/pilot, run the full matrix:
python3 -m concurrency_harness.cli run --output results/experiment-001
python3 -m concurrency_harness.cli report --output results/experiment-001
```

`plan` performs no network access or agent launches. `run` launches agents and
uses the authenticated account. Review the plan first; index 0 is whichever
concurrency setting the seeded schedule selects, not necessarily one agent.
The runner refuses to overwrite a run directory. A failed run exits nonzero;
the full matrix continues through ordinary run failures. Use a new output
directory for reruns and preserve the failed trial. An explicit pilot is not
part of the published three-trial matrix.

Use `--config path/to/config.json` to define a separate experiment. Paths to
tasks, acceptance checks, and constraints resolve relative to the config file.
Do not mix configurations within an output directory. To use a local source
for offline development, change `repository` to its absolute path in a separate
config; this changes the recorded provenance. Each run still makes a fresh clone.

The default `"python": null` uses the exact interpreter launching the harness
(`sys.executable`), so `python3 -m concurrency_harness.cli run ...` uses that
Python to create each run's virtual environment. To select a specific interpreter,
set `"python"` to an executable such as `"python3.12"` or an absolute path.
Missing executables fail with an actionable error before cloning; explicit
selections never silently fall back. The resolved executable is saved in the
experiment config and its version in run metadata. Keep it fixed across trials.

The CLI version is checked exactly. Constraints pin package versions from the
available Ridgeline environment; installed versions are recorded with `pip freeze
--all`. Python 3.12 on Linux is configured in CI; local validation
was performed with Python 3.14. Packages, model access, public SHA availability,
and the real Codex subprocess must pass preflight before collecting results.
Constraints are version pins, not a wheel archive or package hash lock. Preserve
artifacts and an environment/container image for stronger long-term replication.

## Records and interpretation

Each run preserves:

- Input config and tasks, evaluation hashes, code hashes, dependency constraints
  and actual installed versions, CLI/Python/platform metadata, final commit SHA.
- UTC and monotonic timestamps in `events.jsonl`, every command's arguments,
  exit status, duration, timeout, stdout and stderr, plus agent prompts and JSONL.
- Role-tagged agent invocations, implementation and integration attempts, task
  dispatch/finish times, merge conflicts and affected files, validation failures,
  exceptions, and final task outcomes.
- The clone, all worktrees and commits, implementation patches and integration
  diffs in command logs, including rejected candidate worktrees.

`report.json` includes each run, failures, successful times, and the median of
successful runs separately from completion rate. Measurements include agent
seconds by role, conflict counts, failed checks, integration file-edit
occurrences, and raw token-usage records when the CLI emits them. Missing usage
is an empty list, not an inferred zero cost. Integration diffs against a task
branch include other integrated work; use `agent_edits` for files actually edited
during a repair invocation. File edits and retries are rework proxies, not a
semantic measure of wasted effort.

Time limits are 30 minutes per agent invocation and 10 minutes per other command.
Timeouts kill the subprocess process group and retain its logs. All ordinary
failures remain in the dataset, including setup failures that have no work time.
Raw logs include repeated or successful checks as well as failures. There is no
automatic deletion or resumption of trials.

For comparisons, keep the machine, Python, CLI, exact model identifier, reasoning
effort, instructions/skills, package constraints, task definitions, and retry
policy fixed. Avoid other heavy workloads. Record any rate limits or external
outages; concurrency-related contention is part of this local delivery outcome.
Remote model behavior and host-level Codex instructions can change even with
these inputs pinned. Record deviations when replicating on another machine.

## Verification and extension

```sh
python3 -m pip install pytest==8.4.2
python3 -m unittest discover -s tests -v
# Requires Ridgeline dependencies in the current Python environment:
python3 -m tests.verify_workload
# Or use an existing local Git repository as the clone source:
python3 -m tests.verify_workload --repository /absolute/path/to/ridgeline
```

Runner tests use deterministic stand-in agents with real local Git and pytest;
they exercise the shared cap, conflict integration, failure continuation, timeout
logging, and reporting. The workload verifier checks the pinned baseline, confirms
every acceptance case rejects it, and confirms an independent reference
implementation passes. The reference is only a harness self-check, never a
benchmark observation. GitHub CI runs both sets of Python checks without paid
agent calls. Ridgeline's existing Python CI workflow remains in every clone,
and the local runner invokes its pytest gate after integration.

To extend: create a new version of the task/config/acceptance files, add matching
`test_<task_id>` contract functions, and retain explicit overlap and edge cases.
The current loader deliberately requires ten tasks. Change that validation and
version the protocol if experimenting with a different workload size. Publish
the config, task contracts, code revision, constraints, all trial outcomes, and
raw artifacts needed to reproduce any reported aggregate.
