# disk-cleaner-skill

A safe, conversational **disk-cleanup skill** for Claude / Codex agents on macOS
and Linux. Ask your assistant to free up space and it figures out *where your
space actually went*, explains it in plain language, shows a local visual
dashboard, and **never deletes anything without your explicit approval**.

## Install

Add it to any of your agents in one line:

```bash
npx skill add https://github.com/jcordon5/disk-cleaner-skill
```

Then just talk to your assistant: *"my disk is full, can you help me clean it
up?"*

Or install manually and verify it runs:

```bash
git clone https://github.com/jcordon5/disk-cleaner-skill
cd disk-cleaner-skill
python3 -m py_compile scripts/disk_cleaner.py   # sanity check (no deps)
python3 -m unittest discover -s tests            # run the safety tests
```

### Try it risk-free

Want to see exactly what it does before pointing it at your real machine? Run the
built-in demo — it scans a harmless fake home (sparse files, **no real disk
used**) that exercises every category:

```bash
python3 scripts/disk_cleaner.py scan --demo
python3 scripts/disk_cleaner.py serve          # open the dashboard
python3 scripts/disk_cleaner.py clean --safe-only --dry-run
```

> **Status: v0.1-alpha.** Solid and well-tested as a **scan + dashboard +
> dry-run** tool, and safe for cleaning your own caches with explicit approval.
> Review the dry-run before any real deletion, especially on someone else's
> machine.

## What it's for

Recovering disk space without needing to understand terminals, caches, package
managers, app containers, Docker, browser profiles, virtual machines, or system
folders. It's **adaptive** — it isn't hardcoded to specific apps. It measures
real disk usage on *your* machine, classifies what it finds by safety, and only
ever touches what you approve.

What it does, in order:

1. **Scan** adaptively — measures where space went, drilling only into folders
   that actually consume meaningful space.
2. **Classify** everything by safety: safe cache · safe-to-rebuild · review
   required · protected.
3. **Show** a **local browser dashboard** (no internet, nothing leaves your
   machine).
4. **Explain** the findings in plain language and **ask before deleting
   anything**.
5. **Clean only what you approve** — deleting regenerable caches directly and
   moving everything else to the Trash.
6. **Report** how much space was recovered, with a self-contained HTML report.

## Safety first

- Nothing is deleted during discovery — scanning only measures.
- **Protected data is never offered for cleanup**: SSH/GPG keys, keychains,
  browser logins, documents, photos, mail, cloud-sync folders, system
  directories — and it's refused *again* at delete time by an independent
  re-validation pass.
- Anything that isn't a pure cache goes to the **Trash**, so it's recoverable.
- Every action is written to an append-only audit log
  (`~/.disk-cleaner/actions.log.jsonl`).

## Using it directly (the bundled CLI)

The assistant drives this for you, but you can run it yourself too. No
dependencies beyond Python 3 — standard library only, no network access.

```bash
python3 scripts/disk_cleaner.py scan      # measure usage (never deletes)
python3 scripts/disk_cleaner.py scan --demo            # try it on a fake home
python3 scripts/disk_cleaner.py serve     # open the live local dashboard
python3 scripts/disk_cleaner.py plan      # build an approvable plan (units get ids)
python3 scripts/disk_cleaner.py clean --safe-only --dry-run    # preview pure-safe
python3 scripts/disk_cleaner.py clean --rebuildable --dry-run  # preview rebuildable
python3 scripts/disk_cleaner.py clean --id dc_003 --id dc_007  # approve by id
python3 scripts/disk_cleaner.py clean --category "browser cache"
python3 scripts/disk_cleaner.py report    # freed-now vs moved-to-trash
python3 scripts/disk_cleaner.py snapshot  # self-contained report.html (no server)
```

Approval is granular by design: `--safe-only` clears only throwaway caches/temp/
logs; `--rebuildable` (package/build/browser caches and downloaded AI models) is
a **separate** opt-in because those cost time/bandwidth to get back; `--all-safe`
is both. Review items (VMs, downloads, toolchains, large media) only ever move to
the Trash, never deleted.

`scan` and `clean` also write `~/.disk-cleaner/report.html` automatically — a
single self-contained file (data embedded inline) you can open by double-click
anytime, no server or internet needed. All state lives in `~/.disk-cleaner/`
(override with the `DISK_CLEANER_HOME` environment variable).

## The risk model

| Level | Meaning | Action if approved |
|-------|---------|--------------------|
| **safe** | Regenerated automatically, no user-visible loss (temp, logs, app caches) | delete |
| **safe, may re-download** | Safe, but costs time/bandwidth to rebuild (package/build/browser/model caches) | delete |
| **review required** | Could matter; only you can judge (VMs, games, media, downloads, big projects) | move to Trash |
| **protected** | Sensitive or unclassifiable (keys, documents, photos, cloud-sync, system) | never touched |

See [`references/risk-model.md`](references/risk-model.md) for the full
classification and safety details.

## Layout

```
disk-cleaner-skill/
├── SKILL.md                  # assistant behavior & conversation flow
├── README.md
├── scripts/disk_cleaner.py   # the adaptive scanner / cleaner CLI
├── dashboard/index.html      # local-only visual dashboard
├── references/risk-model.md  # full classification & safety details
├── tests/test_disk_cleaner.py # safety tests (classification + delete-time backstop)
└── evals/evals.json          # test prompts
```

## License

MIT
