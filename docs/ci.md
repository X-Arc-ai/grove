# CI

One check runs on every pull request into `develop` and `main`: `Validate`
(`.github/workflows/validate.yml`). It runs ruff, the four repo lints, the migrations on an empty
Neo4j Community store (a service container at the compose pin), the tests, and the web checks.

## Where the jobs run

`Validate` names its runner with one expression:

```yaml
runs-on: ${{ vars.XARC_CI_RUNNER || 'ubuntu-latest' }}
```

With the repository variable `XARC_CI_RUNNER` unset, the job runs on GitHub's hosted
`ubuntu-latest`. While the repository is private during the build, the variable names a
self-hosted runner pool that serves this repository only. Before the repository goes public, the
variable and the pool are removed, so a public repository never has a self-hosted runner, and
`Validate` runs on `ubuntu-latest` again with no change to any workflow file.

## The runner lint

`python -m grove.lints runner` keeps that fallback true. A job's `runs-on` may hold two values
only: the expression above and `ubuntu-latest`. The lint parses every `.yml` and `.yaml` file under
`.github/workflows/` with a YAML parser (PyYAML, a dev dependency) and walks `jobs`, so quotes,
comments, escapes, flow style, block scalars, anchors, aliases and merge keys read as the parser
reads them. The names `jobs`, `runs-on` and `uses` compare without case, and spaces and tabs inside
a value collapse. Any other difference in a value, such as a no-break space, makes it differ.

| Finding | Meaning |
|---|---|
| `runner/not-allowed` | a job's `runs-on` that holds anything else: a pool label, `self-hosted`, a list, a mapping, another expression, nothing |
| `runner/reusable-call` | a job's `uses` that is not a workflow file in `./.github/workflows/`, since the runner of any other workflow is out of reach |
| `runner/symlink` | a workflow file, `.github/workflows` or `.github` that is a symbolic link |
| `runner/unreadable` | a file the parser refuses or that holds more than one document; a root, `jobs` or job that is not a mapping; a key there that is not a scalar; a job with neither `runs-on` nor `uses`; a tracked workflow that is not on disk; or a line break that YAML versions read differently (NEL, U+2028, U+2029, VT, FF, U+001C to U+001E) |

A job that calls a workflow in `./.github/workflows/` is allowed, because the lint reads that file
too. A finding gives a path, a line and a rule, never the matched text. When a value comes through
an alias, the line is where the anchor stands.

Out of the lint's reach: a workflow file that a job writes at run time, and any text that GitHub's
own parser reads differently from PyYAML. The guarantee for a public repository does not rest on
this lint. The variable and the pool are removed before the repository goes public, so a label that
no runner carries makes a job queue in plain view; it never runs on a self-hosted runner.

Run it locally from `server/`:

```sh
uv run python -m grove.lints runner
```
