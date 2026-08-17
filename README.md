# wazup

What's up with this repo, right now. A thin CLI over `gh` and `git` that shows
your current branch's PR and CI status without you having to open a browser.

Requires the [GitHub CLI](https://cli.github.com/) (`gh`) to be installed and
authenticated (`gh auth login`).

## Commands

```
wazup           # repo, branch, PR, and CI status for the current directory
                # (recent local branches, with their PR if any, when on the
                # default branch with no PR of its own)
wazup ci        # just the CI status for the current branch/PR
wazup my        # your open PRs in this repo, or (outside a repo) your PRs
                # with activity in the last 7 days, across all repos
wazup review    # PRs awaiting your review, updated in the last 7 days
```

Add `-w`/`--why` to `wazup` or `wazup ci` to drill into a failing check —
prints the tail of the failing job's log, right up to the error.

wazup also keeps the active `gh` account in sync with whichever repo you're
in, switching it (permanently, like running `gh auth switch` by hand) when
the repo's owner doesn't match.

## Install

```
uv tool install wazup
```

## License

MIT
