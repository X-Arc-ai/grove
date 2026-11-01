# CI

One check is required on every pull request into `develop` and `main`: `Validate`
(`.github/workflows/validate.yml`). It runs ruff, the four repo lints, the migrations on an empty
Neo4j Community store (a service container at the compose pin), the tests, and the web checks.

## Where the jobs run

Every job names its runner with one expression:

```yaml
runs-on: ${{ vars.XARC_CI_RUNNER || 'ubuntu-latest' }}
```

With the repository variable `XARC_CI_RUNNER` unset, every job runs on GitHub's hosted
`ubuntu-latest`. While the repository is private during the build, the variable names a
self-hosted runner pool that serves this repository only. Before the repository goes public, the
variable and the pool are removed, so a public repository never has a self-hosted runner, and
`Validate` runs on `ubuntu-latest` again with no change to any workflow file.

## The runner lint

`python -m grove.lints runner` keeps that fallback true. It reads every file under
`.github/workflows/` and allows two `runs-on` values only: the expression above and
`ubuntu-latest`. Its findings:

| Finding | Meaning |
|---|---|
| `runner/not-allowed` | any other value: a pool label, `self-hosted`, a list, another expression |
| `runner/block-form` | `runs-on:` with its value on the lines below it (a list, `group:`, `labels:`) |
| `runner/reusable-call` | a job that calls a workflow of another repository, whose runner is out of reach |
| `runner/symlink` | a workflow file that is a symbolic link, whose target is out of reach |

A job that calls a workflow of this repository (`uses: ./.github/workflows/...`) is allowed: the
lint reads the called file too. The lint does not read YAML in flow style or YAML anchors for
`runs-on`, and it cannot see a workflow file that a job writes at run time.

Run it locally from `server/`:

```sh
uv run python -m grove.lints runner
```
