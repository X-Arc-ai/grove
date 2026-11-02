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
`ubuntu-latest`. The project has no YAML parser, so the lint reads the files line by line. On one
line it fails closed: a structural line it cannot read is a finding, never a skip. The text inside
a block scalar (`run: |`, `key: >-`) is not structure and is not read. Keys compare without case.
Its findings:

| Finding | Meaning |
|---|---|
| `runner/not-allowed` | any other value: a pool label, `self-hosted`, a list, another expression |
| `runner/block-form` | `runs-on:` with its value on the lines below it (a list, `group:`, a plain scalar that goes on in the next line) |
| `runner/reusable-call` | a job that calls a workflow of another repository, whose runner is out of reach |
| `runner/symlink` | a workflow file, `.github/workflows` or `.github` that is a symbolic link |
| `runner/unreadable` | a line the scanner cannot read: an explicit `?` key, a tag, anchor or alias in a key's place, a merge key `<<`, a double-quoted key with an escape, a flow mapping that holds `runs-on` or `uses` on that line, a job that is only an alias, a line at column 0 that is not a key, or a line break that YAML does not break at (NEL, U+2028, U+2029, VT, FF) |

A job that calls a workflow of this repository (`uses: ./.github/workflows/...`) is allowed: the
lint reads the called file too. Values are compared after quotes and a trailing comment are
dropped and spaces and tabs collapsed; any other whitespace, such as a no-break space, makes the
value differ.

Out of the lint's reach: YAML structure that spans lines, which only a parser can follow (a flow
collection or a quoted scalar that goes on over several lines, with `runs-on` or `uses` on a later
line), and a workflow file that a job writes at run time. Each of these is a deliberate act, not
an accident. Failing closed has a cost too: text that looks like a flow mapping with `runs-on` or
`uses`, also inside a quoted value, is a finding.

The guarantee for a public repository does not rest on this lint. The variable and the pool are
removed before the repository goes public, so a label that no runner carries makes a job queue in
plain view; it never runs on a self-hosted runner.

Run it locally from `server/`:

```sh
uv run python -m grove.lints runner
```
