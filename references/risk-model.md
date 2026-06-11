# Risk model & classification details

This file documents how `disk_cleaner.py` classifies what it finds. You rarely
need it during a normal cleanup — the tool applies these rules itself and the
SKILL.md summary is enough. Read it when a user asks *why* something landed in a
particular category, or when you're deciding how to describe an unusual item.

## The four levels and what the tool does with each

| Level | Meaning | Cleanup action if approved |
|-------|---------|----------------------------|
| `safe` | Regenerated automatically, no user-visible loss | **delete** (direct) |
| `safe_redownload` | Safe, but costs time/bandwidth to rebuild or re-download | **delete** (direct) |
| `review` | Could matter; only the user can judge | **move to Trash** |
| `protected` | Sensitive or unclassifiable | **never an action target** |

Direct deletion is reserved for the two safe levels precisely because they're
the only things that come back on their own. Anything in `review` goes to the
Trash so it's recoverable. Anything `protected` is never offered for cleanup.

## How classification works

Classification is a **pure function of the path** (`classify()` in the script),
which is important: it runs once during the scan to label things, and again at
delete time as an independent safety re-check. Because it doesn't depend on scan
state, it can't be tricked by a stale plan.

Known locations are treated as **hints**, not assumptions. The scanner follows
measured disk usage; these rules only attach a label to whatever was actually
found. No app is ever assumed to be installed.

Precedence: more specific / deeper matches win, and protective rules win ties.
For example, a browser profile folder is protected, but its `Cache` subfolder is
`safe_redownload` — the deeper, more specific Cache rule applies there, while
`Cookies` and `Login Data` inside the same profile stay protected.

### Categories → level (with the hints used)

**safe**
- `safe cache` — folders named `Cache`/`Caches`/`.cache` and their app subfolders.
- `temporary files` — `tmp`, `temp`, the system temp area (`/var/folders`, `/tmp`).
- `logs` — `logs` folders and `.log` files.

**safe_redownload**
- `developer cache` — `DerivedData`, `.gradle`, `__pycache__`, `.pytest_cache`,
  `.mypy_cache`, `.rustup`, and similar tool caches.
- `package manager cache` — `.m2`, `.cargo`, `.npm`, `.yarn`, `.pnpm-store`,
  `.cocoapods`, `.nuget`, `.ivy2`, etc.
- `browser cache` — `Cache`/`Code Cache`/`GPUCache`/`cache2` inside a browser's
  folder (Chrome/Chromium/Firefox/Safari/Edge/Brave/Vivaldi/Opera/Arc).
- `build artifacts` — `node_modules`, and `build`/`dist`/`target`/`.next`/
  `.nuxt`/`.turbo` inside a project.
- `downloaded models or generated data` — `huggingface`, `.ollama`, `torch/hub`,
  `.keras`, `whisper`, `lm-studio`, Stable Diffusion / ComfyUI model dirs. Large
  and re-downloadable, but call these out specifically — re-downloading can be
  many GB.

**review**
- `virtual machines or containers` — `.vmdk`/`.qcow2`/`.vdi`/`.vhd` images,
  VirtualBox/Parallels/UTM bundles, `.vagrant`, lima/colima, Docker volumes.
- `games or media` — Steam/Epic/GOG/Battle.net/Minecraft installs; video/audio
  files (`.mp4`, `.mov`, `.mkv`, `.wav`, `.flac`, …).
- `large personal files` — archives and disk images (`.zip`, `.tar.gz`, `.7z`,
  `.dmg`, `.iso`, `.pkg`).
- `downloads or desktop` — contents of `Downloads`, `Desktop`, `Movies`,
  `Music`.
- `review required` — the default for any large item the tool can't confidently
  classify. **Unknowns are never "safe".**

**protected** (never offered for cleanup)
- Credentials & keys: `.ssh`, `.gnupg`, `.password-store`, keychains, `.aws`,
  `.kube`, `.docker` config, `.kdbx`/`.pem`/`.key`/`.ovpn`/`.mobileconfig` files.
- Browser identity: `Cookies`, `Login Data`, `key4.db`, `logins.json`.
- Personal data: `Documents`, Mail, Messages, Contacts, Calendars, Photos
  library, Safari history/bookmarks.
- Cloud sync: iCloud Drive (`Mobile Documents`), `CloudStorage`, Dropbox, Google
  Drive, OneDrive, Box, pCloud, MEGA — deleting these frees nothing locally and
  can remove files from the cloud.
- System areas: `/System`, `/usr`, `/bin`, `/etc`, `/Library`, `/var/db`, etc.

## Safety backstop at delete time

Independently of the plan, `clean` re-validates every target and **skips** any
that fail. A target is refused if it:
- no longer exists, or is a symlink (we never follow links out of a safe area);
- is outside the scanned safe roots (home / temp);
- is the home directory itself, `/`, or too shallow to be safe;
- is inside a hard-protected prefix (`.ssh`, `Keychains`, `Documents`,
  `Mobile Documents`, `CloudStorage`, system dirs, …);
- re-classifies as `protected`, or its action changed since the plan was built.

This is why approving a broad category can never reach protected data: even if a
path somehow made it into an approval list, the backstop re-derives its risk from
the path alone and refuses it.

## Tuning notes

- On a large drive, raise `--min-size` (e.g. `1GB`) so only the big wins show.
- On a nearly-full small disk, lower it (e.g. `100MB`) so smaller recoveries
  count.
- The estimate only includes what's listed as a candidate; sub-threshold caches
  aren't counted, which keeps the number honest (you only act on what you see).
