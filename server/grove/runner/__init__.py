"""The agent runner: the loop that starts OpenCode passes, and the API side that grants them.

`grove.runner.loop` runs on the box as the service `runner`, under the user `grove-runner`. It
holds one credential, the runner token, and no store or model credential. For each pass the API
grants a pass token bound to one world and one run, and a gateway run token; OpenCode gets those
two and nothing else. `docs/runner.md` has the whole flow.
"""
