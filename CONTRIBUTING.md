# Contributing

Thanks for your interest — contributions are welcome, and there is one thing
about this repository worth knowing before you open a PR.

## This repository is generated

It is a **published mirror** of one package inside a private development
monorepo. The monorepo is the source of truth; this tree is a build output of
it. Every commit here titled `Sync from monorepo @ <sha>` was produced by a
script that **replaces the entire working tree** with a freshly exported copy.

The practical consequence: a change that exists only here does not survive.
The next sync overwrites it, because the exporter reproduces the monorepo's
state rather than merging with this one.

That is not a reason to avoid sending changes. It just means a merged PR is
the *start* of the process rather than the end of it.

## What happens to your PR

1. We review it here.
2. We re-home the change into the corresponding path in the monorepo,
   committing it with `--author` set to you, so your authorship is preserved
   in the source of truth. (Sync commits carry no contributor attribution, so
   this is the step that makes it durable.)
3. It lands back here in a later sync commit, as part of the exported tree.

So don't be alarmed if your commit does not stay visible as a distinct commit
in this repo's history. The check that it worked is that **your change is
present in the files** after the next sync. If it ever disappears, that is a
bug in our process — please open an issue and we will fix it.

Our publish tooling refuses to push over a mirror carrying commits it has not
absorbed, specifically so a merged PR cannot be quietly reverted.

## Practical guidance

- **Keep PRs focused.** One concern per PR. Small, self-contained changes are
  much easier to re-home into a different tree layout.
- **Prefer additions and targeted edits** over sweeping reformatting. A
  whole-file reflow conflicts with the monorepo copy and usually cannot be
  taken as-is.
- **Some paths cannot round-trip.** Parts of the package are held back from
  publication, and generated or vendored directories are exported rather than
  authored here. If a change belongs to one of those, we will say so and work
  out where it should really go.
- **Don't base long-lived branches on this repo's history.** Sync commits
  rewrite the tree wholesale, so a long-running fork drifts badly. Branch,
  send the PR, and let it come back through a sync.
- **Issues and bug reports are just as useful as patches**, and have none of
  the above complications.

## Licensing

By contributing you agree that your contribution is licensed under this
repository's `LICENSE`.
