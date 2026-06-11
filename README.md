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
python3 scripts/disk_cleaner.py serve     # open the live local dashboard
python3 scripts/disk_cleaner.py plan      # build an approvable plan
python3 scripts/disk_cleaner.py clean --all-safe --dry-run   # preview
python3 scripts/disk_cleaner.py clean --category "browser cache"
python3 scripts/disk_cleaner.py report    # what was recovered
python3 scripts/disk_cleaner.py snapshot  # self-contained report.html (no server)
```

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
└── evals/evals.json          # test prompts
```

## License

MIT
