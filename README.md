# wazup

What's up with this repo, right now. A thin CLI over `gh` and `git` that shows
your current branch's PR and CI status without you having to open a browser.

Requires the [GitHub CLI](https://cli.github.com/) (`gh`) to be installed and
authenticated (`gh auth login`).

## Commands

```
wazup           # repo, branch, PR, and CI status for the current directory
wazup ci        # just the CI status for the current branch/PR
wazup my        # your open PRs in this repo, or (outside a repo) your PRs
                # with activity in the last 7 days, across all repos
wazup review    # PRs awaiting your review, updated in the last 7 days
```

## Install

```
uv tool install wazup
```
