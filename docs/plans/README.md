# Task graphs

One `<plan>.tasks.toml` per plan, reviewed and merged like any other file. The graph is the
human gate: routing (`lane`), merge authority (`verify`) and every dependency edge are ratified
here, before a single issue exists.

```sh
uv run dispatchkit validate docs/plans/<plan>.tasks.toml --strict
uv run dispatchkit apply    docs/plans/<plan>.tasks.toml            # dry run
uv run dispatchkit apply    docs/plans/<plan>.tasks.toml --push --repo owner/name
```

The directory is configurable — `paths.plans` in `.github/dispatchkit.toml` — and whatever it
is set to is inside the blast-radius fence, so a task can never auto-merge a change to its own
routing.

See [../design.md](../design.md), "Task graph format", for the schema.
