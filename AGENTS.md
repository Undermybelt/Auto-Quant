# Auto-Quant Agent Contract

This fork is used as a local Auto-Quant execution harness for concurrent agent
strategy iteration and for handoffs from `ict-engine`.

## First Read

Before planning or editing, read:

- `AGENTS.md`
- `program.md`
- `README.md`
- `auto_quant_workspace.py`
- `run.py`
- `prepare.py`
- `config.json`
- the `ict-engine` handoff artifact, if one was provided

Then run `git status --short` and preserve unrelated dirty work. This checkout
often has live local strategies and data.

## Harness Loop

Use a plan -> work -> review loop for each lane.

- Plan: write a lane-local `plan.md` before editing strategies. Include the
  objective, source handoff path, workspace env, candidate ideas, verification
  command, and stop condition.
- Work: create, evolve, fork, or remove at most 3 active non-underscore
  strategy files in the lane strategies directory.
- Review: run the measured backtest command, inspect `run.log`, update
  `results.tsv`, and write a lane-local `review.md` with keep/discard evidence.

Do not treat "no strategies found" as completion. Do not summarize results from
memory; cite the current `run.log`, `results.tsv`, and strategy files.

## Parallel Workspace Contract

Repo-root behavior stays compatible with upstream when no env vars are set.
For concurrent agents, isolate mutable state with `AUTO_QUANT_WORKSPACE`.

Typical lane setup:

```bash
mkdir -p /tmp/aq-lane-a/user_data/strategies
cp config.json /tmp/aq-lane-a/config.json
printf 'commit\tevent\tstrategy_name\tsharpe\tmax_dd\tnote\n' > /tmp/aq-lane-a/results.tsv

AUTO_QUANT_WORKSPACE=/tmp/aq-lane-a \
AUTO_QUANT_DATA_DIR="$PWD/user_data/data" \
uv run run.py > /tmp/aq-lane-a/run.log 2>&1
```

Supported overrides:

- `AUTO_QUANT_WORKSPACE`: mutable lane root.
- `AUTO_QUANT_CONFIG`: config override.
- `AUTO_QUANT_USER_DATA`: user data root override.
- `AUTO_QUANT_DATA_DIR`: shared read-only candle data override.
- `AUTO_QUANT_STRATEGIES_DIR`: strategy directory override.
- `AUTO_QUANT_RESULTS_TSV`: lane-local results log override.

When `AUTO_QUANT_WORKSPACE` is available, do not mutate repo-root
`config.json`, `user_data/strategies`, `user_data/data`, or `results.tsv` for
that lane.

Repo admission rule for `ict-engine` lanes: generated strategies, lane plans,
reviews, logs, result journals, strategy libraries, and adoption bundles stay
under `AUTO_QUANT_WORKSPACE` or another `/tmp` run root until `ict-engine`
promotes them into an explicit evidence packet or practical-closure artifact.
No strategy sketch or candidate packet enters this repository only because a
backtest ran or a metric looked interesting.

## ict-engine Handoff

If an `ict-engine` handoff includes `agent_workflow`, follow it as the
authoritative lane setup:

- run its `setup_commands`
- export its `environment`
- keep artifacts under the listed workspace paths
- follow its plan/work/review phases
- return the listed expected artifacts to `ict-engine`

`ict-engine` remains the control plane. Auto-Quant run success, sparse positive
metrics, or a generated strategy file is candidate evidence only. It does not
imply `trade_usable=true`, promotion, or live readiness until `ict-engine`
adoption and promotion gates explicitly pass.

If the current candidate has not passed those gates and has not been packaged as
an `ict-engine` evidence packet, leave every mutable artifact in `/tmp` and keep
the repository change limited to harness code, tests, or durable documentation.

Minimum return packet for `ict-engine`:

- `plan.md`
- `run.log`
- `results.tsv`
- strategy file paths
- `review.md`
- `strategy_library.json` or an adoption bundle when a measured candidate
  survives review

## Verification

For harness code changes, use tests before claiming completion:

```bash
python3 -m unittest tests/test_auto_quant_workspace.py -v
.venv/bin/python -m py_compile auto_quant_workspace.py run.py prepare.py tests/test_auto_quant_workspace.py
```

For strategy iteration, run the lane-local backtest command and inspect the
current log. For docs-only changes, read back the changed section and report
that no runtime behavior changed.

## Do Not

- Do not run Claude Code Harness plugin installers, hooks, MCP setup, or bundled
  binaries from this repo unless the user explicitly asks for runtime bring-up.
- Do not add dependencies just to make a strategy idea work.
- Do not call live-trading commands. This repo is a dry-run/backtest harness.
- Do not overwrite unrelated local strategies, config, data, or results.
