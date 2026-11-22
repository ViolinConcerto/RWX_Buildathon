# Blast Radius — change-aware test selection for sqlglot

Entry for the RWX **Blast Radius** track: given a patch to
[sqlglot](https://github.com/tobymao/sqlglot) @ `eb11aec4`, print which of its
1,620 tests to run and how to split them over ≤ 8 workers. Missing a failing
test costs 300 s; every selected second costs time.

## How it works

1. **Dependency map, built once from the pinned commit** (`maps/depmap.json.gz`, ~1 MB).
   The full suite is run under coverage with per-test contexts (setup/call/teardown,
   class fixtures and module collection attributed correctly), an audit hook that
   records files each test opens or lists, and hooks recording which sqlglot classes
   each test instantiates. Process pools are replaced by an in-process executor that
   still pickles, so pool-worker code (and the serde code pickling triggers) is
   attributed to the right test. See `tools/mapgen/`.
2. **Structural diff of each patch** (`selector/pydiff.py`). Comments and formatting
   do not change the AST; real edits are localised to statements and classified as
   *runtime* (inside a function or lambda body) or *definition-time* (module/class
   level, decorators, defaults).
3. **Selection** (`selector/static.py`):
   - runtime edits → tests that executed the edited statements;
   - definition-time edits → tests that instantiated the class (or a subclass) and,
     when the name is only read at run time, executed a line mentioning it;
     names consumed at import (metaclasses, class bodies) widen to class users;
   - fixtures / data files → tests that read them; added/removed files → directory readers;
   - syntax errors, removed modules, anything unparsable → whole suite.
4. **Split** with LPT for every k ≤ 8, picking the cheapest `max load + 2 s·k`.

Any exception for a commit falls back to the whole suite for that commit.

## Results (sample set, 30 commits, scorer's exact run flags)

| version | simulated | misses | CPU | total |
| --- | --- | --- | --- | --- |
| starter (all tests, 3 workers) | 1210.5 s | 0 | — | — |
| oracle (exact failing set) | 94.4 s | 0 | — | — |
| v1 static | 170.7 s | 0 | 1.1 s | 171.8 s |

Experiment log with hypotheses and measurements: [`experiments/LOG.md`](experiments/LOG.md).

## Layout

- `Dockerfile` — the entry (python:3.13-slim digest used for labeling + sqlglot tarball @ eb11aec).
- `selector/` — runtime code (`main.py` entry point).
- `maps/` — dependency map built from the pinned commit only.
- `tools/mapgen/` — map builder (coverage/reads/classes collector, merger).
- `tools/lab/` — lab image and scripts that reproduce the labels by running the suite.
- `starter/` — the official starter kit (scorer, sample set).

## Code brought in

- [sqlglot](https://github.com/tobymao/sqlglot) at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`
  (downloaded at build time; MIT).
- The official Blast Radius starter kit (`starter/`).

## License

MIT — see `LICENSE`.
