#!/usr/bin/env python3
"""disk_cleaner.py — an adaptive, safety-first disk investigator and cleaner.

This is the bundled helper for the `disk-cleaner` skill. It is deliberately
self-contained: standard library only, no third-party dependencies, no network
access. It runs on macOS and Linux (and other Unix-likes).

The design follows the skill's priorities, in order: safety first, then adaptive
scanning, clear classification, visual explanation, an approval workflow, and
finally reliable cleanup with a report.

Subcommands:
    scan     Walk safe roots, measure where space is actually used, classify it,
             and write a structured map (no deletion ever happens here).
    plan     Turn a scan into an approvable cleanup plan grouped by category.
    serve    Serve the local browser dashboard (reads the scan/plan/report JSON).
    clean    Execute ONLY the cleanup units the user explicitly approved, with a
             full re-validation pass and an append-only audit log.
    report   Print / write a human-readable summary of what was recovered.

Everything the tool produces lives under a single state directory (default
~/.disk-cleaner) so behaviour is identical regardless of the current directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote


# ---------------------------------------------------------------------------
# State / paths
# ---------------------------------------------------------------------------

def state_dir() -> str:
    d = os.environ.get("DISK_CLEANER_HOME") or os.path.join(
        os.path.expanduser("~"), ".disk-cleaner"
    )
    os.makedirs(d, exist_ok=True)
    return d


def state_path(name: str) -> str:
    return os.path.join(state_dir(), name)


SCAN_FILE = "scan.json"
PLAN_FILE = "plan.json"
REPORT_FILE = "report.json"
PROGRESS_FILE = "progress.json"
AUDIT_FILE = "actions.log.jsonl"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def human(n: int) -> str:
    """Human-readable bytes, e.g. 1.2 GB."""
    step = 1024.0
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    x = float(n)
    for u in units:
        if x < step or u == units[-1]:
            return f"{x:.0f} {u}" if u == "B" else f"{x:.1f} {u}"
        x /= step
    return f"{n} B"


# ---------------------------------------------------------------------------
# Risk model
# ---------------------------------------------------------------------------
# Four risk levels drive every safety decision:
#   safe              -> regenerated automatically, no user-visible loss. Delete OK.
#   safe_redownload   -> safe, but may cost time/bandwidth to rebuild/redownload. Delete OK.
#   review            -> could matter to the user. NEVER auto-cleaned; if approved,
#                        move to Trash (recoverable) rather than delete.
#   protected         -> sensitive or unclassifiable. Never an action target, ever.
#
# Each (category -> risk, action) mapping. `action` is what cleanup would do IF the
# user approves: "delete" (direct, recoverable only from backups), "trash" (move to
# the OS Trash), or "none" (cannot be a cleanup target at all).

RISK_DELETE = "delete"
RISK_TRASH = "trash"
RISK_NONE = "none"

CATEGORIES = {
    # user-facing label                risk               action
    "safe cache":                    ("safe",            RISK_DELETE),
    "temporary files":               ("safe",            RISK_DELETE),
    "logs":                          ("safe",            RISK_DELETE),
    "developer cache":               ("safe_redownload", RISK_DELETE),
    "browser cache":                 ("safe_redownload", RISK_DELETE),
    "package manager cache":         ("safe_redownload", RISK_DELETE),
    "build artifacts":               ("safe_redownload", RISK_DELETE),
    "downloaded models or generated data": ("safe_redownload", RISK_DELETE),
    "large app data":                ("review",          RISK_TRASH),
    "virtual machines or containers":("review",          RISK_TRASH),
    "games or media":                ("review",          RISK_TRASH),
    "downloads or desktop":          ("review",          RISK_TRASH),
    "large personal files":          ("review",          RISK_TRASH),
    "review required":               ("review",          RISK_TRASH),
    "protected, not touched":        ("protected",       RISK_NONE),
}


def risk_of(category: str) -> str:
    return CATEGORIES.get(category, ("protected", RISK_NONE))[0]


def action_of(category: str) -> str:
    return CATEGORIES.get(category, ("protected", RISK_NONE))[1]


# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------
# IMPORTANT: known locations are HINTS, not assumptions. The scanner is driven by
# measured disk usage; these rules only *label* whatever was actually found. We
# never assume any app is installed.
#
# A rule matches against the path components (lowercased). Each rule returns the
# index of the component it matched on, used as a "specificity" score: a deeper
# match wins, because e.g. ".../Chrome/Default/Cache" (Cache is deep) should beat a
# shallow "this is a browser profile" match, while ".../Chrome/Default/Cookies"
# lets the protected Cookies rule win. On ties, the more protective rule wins.

# Sensitive leaf names / components that must always be protected, wherever found.
PROTECTED_NAMES = {
    ".ssh", ".gnupg", ".gpg", "keychains", "login data", "cookies", "key4.db",
    "logins.json", "key3.db", ".password-store", ".aws", ".kube", ".docker",
    "wallet", "wallets", "id_rsa", "id_ed25519", "credentials", "secrets",
}
PROTECTED_SUBSTR = [
    "keychain", "password-store", "/.ssh", "/.gnupg",
]
PROTECTED_EXT = {".kdbx", ".keychain", ".key", ".pem", ".ovpn", ".mobileconfig"}

# Cloud-sync roots (do not touch — deleting "frees" nothing and may delete from cloud).
CLOUD_SUBSTR = [
    "library/mobile documents",      # iCloud Drive
    "library/cloudstorage",          # macOS Files-On-Demand providers
    "/dropbox", "/google drive", "/googledrive", "/onedrive", "/box sync",
    "/pcloud", "/mega",
]

# Personal-data roots that are protected by default.
PROTECTED_PERSONAL = [
    "library/mail", "/mail", "library/messages", "library/calendars",
    "library/contacts", "photos library.photoslibrary", "/pictures",
    "library/application support/addressbook",
    "library/safari",  # history/bookmarks live here; its Cache is separate (see below)
]

# Documents-ish: protected (the user's actual files), but Downloads/Desktop are
# explicitly "review" per the risk model.
PROTECTED_DOCS = ["/documents"]
REVIEW_PERSONAL = ["/downloads", "/desktop", "/movies", "/music"]


def classify(path: str) -> tuple[str, str]:
    """Return (category, reason) for a path. Pure function of the path string.

    Re-run at clean time as an independent safety check, so it must never depend
    on scan state.
    """
    low = path.lower()
    comps = [c for c in low.split(os.sep) if c]
    name = comps[-1] if comps else low
    base, ext = os.path.splitext(name)

    def has(substr_list) -> int:
        # return deepest index whose component-path prefix contains the substring
        best = -1
        joined = os.sep + os.sep.join(comps)
        for s in substr_list:
            if s in joined:
                # approximate depth by counting separators in the matched tail
                idx = joined.find(s)
                depth = joined.count(os.sep, 0, idx + len(s))
                best = max(best, depth)
        return best

    # ---- PROTECTED (highest priority; sensitive or unclassifiable) ----------
    if name in PROTECTED_NAMES or base in PROTECTED_NAMES:
        return "protected, not touched", "sensitive credential or key store"
    if ext in PROTECTED_EXT:
        return "protected, not touched", "looks like a key / certificate / profile"
    if has(PROTECTED_SUBSTR) >= 0:
        return "protected, not touched", "sensitive credential or key store"
    if has(CLOUD_SUBSTR) >= 0 or \
       any(c in low for c in ("onedrive", "dropbox", "google drive", "pcloud",
                              "/megasync", "tresorit", "/icloud")):
        return "protected, not touched", "cloud-sync data — deleting may remove from the cloud or break sync"
    if has(PROTECTED_PERSONAL) >= 0:
        return "protected, not touched", "personal data (mail / photos / messages / history)"
    if has(PROTECTED_DOCS) >= 0:
        return "protected, not touched", "documents folder — your own files"

    # ---- Browser caches (safe) vs browser profiles (protected) --------------
    # A profile dir holds cookies/logins/history (protected). Only the clearly
    # cache-only subdirs are safe to clear. Note we deliberately do NOT match the
    # whole "Service Worker" dir — it holds registrations and IndexedDB-backed
    # offline state, not just cache; only its "CacheStorage" subdir is cache.
    browser_cache_dirs = ("cache", "code cache", "gpucache", "cachestorage",
                          "cache_data", "cache2", "dawncache",
                          "graphitedawncache", "scriptcache")
    if any(c in browser_cache_dirs for c in comps) and \
       any(b in low for b in ("chrome", "chromium", "firefox", "safari", "edge",
                              "brave", "vivaldi", "opera", "arc")):
        return "browser cache", "browser cache — rebuilds itself as you browse"

    # ---- Toolchains / runtimes / environments (review, NOT auto-safe) -------
    # These dirs install actual toolchains, SDKs, language runtimes, or virtual
    # environments — not throwaway cache. Removing them can break a developer's
    # setup, so they need explicit per-item review even though they're rebuildable.
    toolchain_dirs = (".rustup", ".pyenv", ".rbenv", ".nvm", ".nodenv", ".sdkman",
                      ".asdf", ".conda", "miniconda3", "anaconda3", "miniforge3",
                      ".gem", ".rvm", "android-sdk", ".android", "flutter",
                      ".volta", "virtualenvs", ".virtualenvs")
    if any(c in toolchain_dirs for c in comps):
        return ("review required",
                "developer toolchain / runtime / environment — removing it can "
                "break your setup; review before removing")

    # ---- Developer & package-manager caches (safe_redownload) ---------------
    dev_components = {
        "node_modules": "build artifacts",
        "deriveddata": "developer cache",
        ".gradle": "developer cache",
        ".m2": "package manager cache",
        ".cargo": "package manager cache",
        ".npm": "package manager cache",
        ".yarn": "package manager cache",
        ".pnpm-store": "package manager cache",
        ".cocoapods": "package manager cache",
        "__pycache__": "developer cache",
        ".pytest_cache": "developer cache",
        ".mypy_cache": "developer cache",
        ".ruff_cache": "developer cache",
        ".gradle_cache": "developer cache",
        ".nuget": "package manager cache",
        ".ivy2": "package manager cache",
        ".sbt": "package manager cache",
        ".dub": "package manager cache",
    }
    # known model / generated-data caches (kept above generic .cache match below)
    model_hints = ["huggingface", "/.ollama", "torch/hub", "/.keras", "whisper",
                   "lm-studio", "lmstudio", "stable-diffusion", "stablediffusion",
                   "comfyui/models", "sentence-transformers", "/transformers"]
    if any(m in low for m in model_hints):
        return ("downloaded models or generated data",
                "downloaded model/AI cache — large; can be re-downloaded if needed")

    if ".cache" in comps and comps.index(".cache") == len(comps) - 1:
        # the umbrella ~/.cache dir itself — descend to label sub-caches
        return "safe cache", "cache folder — apps recreate this as needed"

    for comp, cat in dev_components.items():
        if comp in comps:
            why = {
                "build artifacts": "build output / dependencies — rebuild with your package manager",
                "developer cache": "developer tool cache — regenerated automatically",
                "package manager cache": "package manager cache — re-downloaded on demand",
            }[cat]
            return cat, why

    # build output dirs that commonly hold regenerable artifacts
    if name in ("build", "dist", ".next", ".nuxt", ".turbo", ".parcel-cache",
                "target", ".dart_tool", "out", ".angular") and \
       _looks_like_project(comps):
        return "build artifacts", "build output — regenerated when you rebuild"

    # ---- Generic OS caches / temp / logs (safe) -----------------------------
    if "caches" in comps or "cache" in comps or ".cache" in comps:
        return "safe cache", "application cache — apps recreate this as needed"
    if any(c in ("tmp", "temp", "temporaryitems", "tmpdir") for c in comps) or \
       low.startswith("/tmp") or low.startswith("/var/tmp") or "/var/folders/" in low:
        return "temporary files", "temporary files — safe to clear"
    if "logs" in comps or "log" in comps or ext == ".log":
        return "logs", "log files — safe to clear"

    # ---- Virtual machines & containers (review) -----------------------------
    vm_ext = {".vmdk", ".qcow2", ".vdi", ".vhd", ".vhdx", ".hds", ".pvm", ".vmwarevm"}
    vm_hints = ["virtualbox vms", "/parallels", ".vagrant", "/lima/", "/colima/",
                "docker/volumes", "com.docker.docker", "containers/storage",
                "/.minikube", "/utm/"]
    if ext in vm_ext or any(h in low for h in vm_hints) or \
       name.endswith((".pvm", ".vmwarevm", ".utm")):
        return ("virtual machines or containers",
                "virtual machine / container storage — review before removing")

    # ---- Games & media (review) ---------------------------------------------
    game_hints = ["steamapps", "/steam/", "steamlibrary", "/epic games", "/gog",
                  "/origin games", "battle.net", "/riot games", "minecraft"]
    if any(h in low for h in game_hints):
        return "games or media", "game install / data — review before removing"
    media_ext = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".wav", ".aiff", ".flac"}
    archive_ext = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
                   ".dmg", ".iso", ".pkg"}
    if ext in media_ext:
        return "games or media", "media file — review before removing"
    if ext in archive_ext:
        return "large personal files", "archive / disk image — review before removing"

    # ---- Location-based review hints ---------------------------------------
    if has(REVIEW_PERSONAL) >= 0:
        return "downloads or desktop", "Downloads/Desktop/media — review before removing"

    # ---- Default: unsure -> review (never auto-clean) -----------------------
    return ("review required",
            "couldn't confidently classify this — left for you to review")


def _looks_like_project(comps: list[str]) -> bool:
    # crude: build/dist dirs are only "build artifacts" when not at the very top
    # of home; avoids mislabelling, say, ~/Desktop "build".
    return len(comps) >= 3


# ---------------------------------------------------------------------------
# Safe-root resolution
# ---------------------------------------------------------------------------

def default_roots() -> list[str]:
    home = os.path.expanduser("~")
    roots = [home]
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir and os.path.isdir(tmpdir):
        roots.append(os.path.realpath(tmpdir.rstrip(os.sep)))
    return roots


# Paths we must never traverse into or act on, even if a root contains them.
def hard_protected_prefixes() -> list[str]:
    home = os.path.expanduser("~")
    pfx = [
        os.path.join(home, ".ssh"),
        os.path.join(home, ".gnupg"),
        os.path.join(home, ".password-store"),
        os.path.join(home, "Library", "Keychains"),
        os.path.join(home, "Library", "Mail"),
        os.path.join(home, "Library", "Messages"),
        os.path.join(home, "Documents"),
        os.path.join(home, "Library", "Mobile Documents"),
        os.path.join(home, "Library", "CloudStorage"),
        "/System", "/usr", "/bin", "/sbin", "/etc", "/var/db", "/Library",
        "/private/etc", "/boot", "/proc", "/sys", "/dev",
    ]
    return [os.path.realpath(p) for p in pfx]


def is_under(path: str, prefix: str) -> bool:
    try:
        path = os.path.realpath(path)
    except OSError:
        return False
    prefix = os.path.realpath(prefix)
    return path == prefix or path.startswith(prefix + os.sep)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

class ScanStats:
    def __init__(self):
        self.dirs = 0
        self.files = 0
        self.errors = 0


# Umbrella cache dirs hold many *different* sub-caches (pip, a model cache, a
# browser cache…). We descend one level into them so each sub-cache is labelled
# with its real category instead of being lumped together and cleaned blind.
UMBRELLA = {".cache", "caches"}


def compute_size(path: str, root_dev: int, stats: ScanStats, cache: dict) -> int:
    """Recursive, memoized directory size. Never follows symlinks, never crosses
    filesystem boundaries, tolerates permission errors. Each directory is walked
    once thanks to the cache, so building the tree stays close to O(files)."""
    cached = cache.get(path)
    if cached is not None:
        return cached
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    stats.errors += 1
                    continue
                if entry.is_symlink() or st.st_dev != root_dev:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stats.dirs += 1
                    total += compute_size(entry.path, root_dev, stats, cache)
                elif entry.is_file(follow_symlinks=False):
                    stats.files += 1
                    total += st.st_size
    except (PermissionError, OSError):
        stats.errors += 1
    cache[path] = total
    return total


def _file_node(path: str, size: int, depth: int) -> dict:
    cat, reason = classify(path)
    return {
        "path": path, "name": os.path.basename(path), "size": size,
        "is_dir": False, "category": cat, "risk": risk_of(cat),
        "action": action_of(cat), "reason": reason, "depth": depth, "children": [],
    }


def build_tree(path: str, root_dev: int, stats: ScanStats, report_min: int,
               max_depth: int, cache: dict, depth: int = 0) -> dict | None:
    """Build a hierarchical node tree of significant directories and large files.

    A subtree we'd clean wholesale (safe / safe_redownload) is kept as a single
    leaf — no point enumerating the thousands of files inside a cache. Protected
    subtrees are also left opaque (we don't read into people's private data).
    Everything else is descended so individual large folders and files surface.
    Only *direct* children are added at each level, so nothing is double-counted."""
    size = compute_size(path, root_dev, stats, cache)
    category, reason = classify(path)
    risk = risk_of(category)
    name = os.path.basename(path) or path

    node = {
        "path": path, "name": name, "size": size, "is_dir": True,
        "category": category, "risk": risk, "action": action_of(category),
        "reason": reason, "depth": depth, "container": name.lower() in UMBRELLA,
        "children": [],
    }

    # An umbrella cache dir is descended even though it's "safe", so its varied
    # sub-caches get their own labels.
    leaf = (risk in ("safe", "safe_redownload")) and name.lower() not in UMBRELLA
    if depth >= max_depth or risk == "protected" or leaf:
        return node

    try:
        entries = list(os.scandir(path))
    except (PermissionError, OSError):
        entries = []
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            st = entry.stat(follow_symlinks=False)
            if st.st_dev != root_dev:
                continue
        except OSError:
            continue
        if entry.is_dir(follow_symlinks=False):
            if compute_size(entry.path, root_dev, stats, cache) >= report_min:
                child = build_tree(entry.path, root_dev, stats, report_min,
                                   max_depth, cache, depth + 1)
                if child:
                    node["children"].append(child)
        elif entry.is_file(follow_symlinks=False) and st.st_size >= report_min:
            node["children"].append(_file_node(entry.path, st.st_size, depth + 1))

    node["children"].sort(key=lambda n: -n["size"])
    return node


def _unit(node: dict) -> dict:
    return {
        "path": node["path"], "name": node["name"], "size": node["size"],
        "category": node["category"], "risk": node["risk"],
        "action": node["action"], "reason": node["reason"],
        "is_dir": node["is_dir"],
    }


def collect_candidates(node: dict, out: list) -> None:
    """Pick cleanup units: the most specific actionable node in each subtree.

    A safe/safe_redownload subtree is emitted whole (one tidy line — we clean it
    wholesale, no need to enumerate inside). Protected subtrees are skipped
    entirely. For an unknown/review *container*, we recurse first and only emit
    the container itself if nothing more specific underneath explained the space —
    that keeps a generic "review required" home folder from swallowing the safe
    caches and individual large files nested inside it."""
    risk = node["risk"]
    if risk == "protected":
        return
    if risk in ("safe", "safe_redownload") and not node.get("container"):
        out.append(_unit(node))
        return
    # umbrella cache, or review / unknown container: try children first
    before = len(out)
    for child in node.get("children", []):
        collect_candidates(child, out)
    if len(out) == before:
        # nothing below explained the space — this node is the review unit
        out.append(_unit(node))


def summarize(candidates: list, tree: list) -> dict:
    by_category: dict = {}
    by_risk: dict = {}
    for c in candidates:
        bc = by_category.setdefault(c["category"], {"size": 0, "count": 0})
        bc["size"] += c["size"]; bc["count"] += 1
        br = by_risk.setdefault(c["risk"], {"size": 0, "count": 0})
        br["size"] += c["size"]; br["count"] += 1

    def risk_size(r):
        return by_risk.get(r, {"size": 0})["size"]

    flat = sorted(candidates, key=lambda c: -c["size"])
    return {
        "by_category": by_category,
        "by_risk": by_risk,
        "estimated_recoverable": {
            "safe": risk_size("safe"),
            "safe_redownload": risk_size("safe_redownload"),
            "review": risk_size("review"),
            "total_safe": risk_size("safe") + risk_size("safe_redownload"),
        },
        "top_consumers": [
            {"path": c["path"], "name": c["name"], "size": c["size"],
             "category": c["category"], "risk": c["risk"]}
            for c in flat[:15]
        ],
    }


def cmd_scan(args) -> int:
    if getattr(args, "demo", False):
        demo_root = build_demo()
        log(f"Demo mode: built a harmless fake home at {demo_root} (sparse files, "
            f"no real disk used). Scanning it instead of your machine.")
        roots = [demo_root]
    else:
        roots = [os.path.realpath(os.path.expanduser(r)) for r in
                 (args.root or default_roots())]
    report_min = parse_size(args.min_size)

    disk = shutil.disk_usage(roots[0])
    stats = ScanStats()
    trees = []
    log(f"Scanning {len(roots)} root(s). This reads sizes only — nothing is deleted.")
    for r in roots:
        if not os.path.isdir(r):
            continue
        try:
            root_dev = os.stat(r).st_dev
        except OSError:
            continue
        log(f"  • {r}")
        t = build_tree(r, root_dev, stats, report_min, args.max_depth, {})
        if t:
            trees.append(t)

    candidates: list = []
    for t in trees:
        collect_candidates(t, candidates)
    candidates.sort(key=lambda c: -c["size"])
    # Stable ids so the user/agent can approve units by id (dc_001…) rather than
    # by re-typing or re-interpreting a path — less ambiguity, less room for error.
    for i, c in enumerate(candidates, 1):
        c["id"] = f"dc_{i:03d}"

    scan = {
        "version": 1,
        "generated_at": now_iso(),
        "platform": sys.platform,
        "home": os.path.expanduser("~"),
        "roots": roots,
        "report_min_bytes": report_min,
        "disk": {
            "mount": roots[0], "total": disk.total, "used": disk.used,
            "free": disk.free,
        },
        "stats": {"dirs": stats.dirs, "files": stats.files, "errors": stats.errors},
        "tree": trees,
        "candidates": candidates,
        "summary": summarize(candidates, trees),
        "note": "Sizes are apparent file sizes; nothing has been deleted.",
    }
    out = state_path(SCAN_FILE)
    with open(out, "w") as f:
        json.dump(scan, f, indent=2)
    install_dashboard()
    build_snapshot()

    est = scan["summary"]["estimated_recoverable"]
    log("")
    log(f"Scan complete. Disk: {human(disk.used)} used of {human(disk.total)} "
        f"({human(disk.free)} free).")
    log(f"Safe to clean (caches/temp/logs/builds): ~{human(est['total_safe'])}")
    log(f"Needs your review:                       ~{human(est['review'])}")
    log(f"Saved map: {out}")
    if args.json:
        print(json.dumps(scan["summary"], indent=2))
    return 0


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def cmd_plan(args) -> int:
    scan = load_json(state_path(SCAN_FILE))
    if not scan:
        log("No scan found. Run `scan` first.")
        return 1
    groups: dict = {}
    for c in scan["candidates"]:
        g = groups.setdefault(c["category"], {
            "category": c["category"], "risk": c["risk"], "action": c["action"],
            "size": 0, "items": [],
        })
        g["size"] += c["size"]
        g["items"].append({**c, "approved": False})

    order = list(CATEGORIES.keys())
    ordered = sorted(groups.values(),
                     key=lambda g: (order.index(g["category"])
                                    if g["category"] in order else 99))
    plan = {
        "version": 1,
        "generated_at": now_iso(),
        "scan_generated_at": scan["generated_at"],
        "groups": ordered,
        "approved_paths": [],
    }
    with open(state_path(PLAN_FILE), "w") as f:
        json.dump(plan, f, indent=2)

    log("Cleanup plan (nothing approved yet):\n")
    for g in ordered:
        tag = {"delete": "delete", "trash": "→ Trash", "none": "PROTECTED"}[g["action"]]
        log(f"  [{g['risk']:<15}] {g['category']:<34} {human(g['size']):>10}  ({tag})")
    log(f"\nSaved plan: {state_path(PLAN_FILE)}")
    if args.json:
        print(json.dumps(plan, indent=2))
    return 0


# ---------------------------------------------------------------------------
# clean
# ---------------------------------------------------------------------------

def trash_path(target: str) -> str:
    """Move a path to the OS Trash (recoverable). Returns the new location."""
    home = os.path.expanduser("~")
    name = os.path.basename(target.rstrip(os.sep))
    if sys.platform == "darwin":
        trash = os.path.join(home, ".Trash")
    else:  # freedesktop
        trash = os.path.join(
            os.environ.get("XDG_DATA_HOME", os.path.join(home, ".local", "share")),
            "Trash", "files")
    os.makedirs(trash, exist_ok=True)
    dest = os.path.join(trash, name)
    i = 1
    while os.path.exists(dest):
        dest = os.path.join(trash, f"{name}.{i}")
        i += 1
    # freedesktop trashinfo (best effort; harmless on macOS to skip)
    if sys.platform != "darwin":
        info_dir = os.path.join(os.path.dirname(trash), "info")
        os.makedirs(info_dir, exist_ok=True)
        with open(os.path.join(info_dir, os.path.basename(dest) + ".trashinfo"), "w") as f:
            f.write("[Trash Info]\nPath=%s\nDeletionDate=%s\n" %
                    (quote(os.path.realpath(target)), now_iso()))
    shutil.move(target, dest)
    return dest


def audit(record: dict) -> None:
    with open(state_path(AUDIT_FILE), "a") as f:
        f.write(json.dumps(record) + "\n")


def validate_target(path: str, expected_action: str,
                    allowed_roots: list[str] | None = None) -> tuple[bool, str]:
    """Independent re-validation at clean time. This is the last line of defence;
    it does NOT trust the plan and re-derives everything from the path itself.

    `allowed_roots` are the roots the scan actually covered (e.g. an external
    volume passed via --root); cleanup is permitted only inside one of them plus
    the always-safe home/temp areas. All the other guardrails still apply."""
    real = os.path.realpath(path)
    home = os.path.realpath(os.path.expanduser("~"))

    if not os.path.exists(path):
        return False, "no longer exists"
    if os.path.islink(path):
        return False, "is a symlink (refusing to follow)"
    # must live under a safe root: the always-allowed home/temp, plus any root the
    # scan actually covered (so external volumes scanned with --root are cleanable).
    roots = [home] + [os.path.realpath(os.path.expanduser(r))
                      for r in (os.environ.get("TMPDIR", ""), "/tmp", "/var/tmp")
                      if r]
    roots += [os.path.realpath(r) for r in (allowed_roots or [])]
    if not any(is_under(real, r) for r in roots):
        return False, "outside the scanned safe roots"
    # never the home dir, a top-level home child that is huge, or system roots
    if real == home or real == "/" or os.path.dirname(real) == os.path.dirname(home) and real != home:
        pass  # dirname-of-home check below is the real guard
    if real == home or real == "/":
        return False, "refusing to act on a top-level / home directory"
    # depth guard: never act on home's immediate parent or shallow system paths
    if len(real.rstrip(os.sep).split(os.sep)) < 4:
        return False, "path is too shallow to be safe"
    # hard protected prefixes
    for p in hard_protected_prefixes():
        if is_under(real, p):
            return False, f"inside a protected area ({p})"
    # re-classify: must not be protected, and action must match
    category, _ = classify(path)
    risk = risk_of(category)
    action = action_of(category)
    if risk == "protected" or action == RISK_NONE:
        return False, f"reclassified as protected ({category})"
    if action != expected_action:
        return False, f"action changed since plan ({action} != {expected_action})"
    return True, "ok"


def cmd_clean(args) -> int:
    plan = load_json(state_path(PLAN_FILE))
    if not plan:
        log("No plan found. Run `plan` first.")
        return 1

    # Roots the scan actually covered, so cleanup can reach an external volume the
    # user explicitly scanned with --root (validate_target still guards each item).
    scan = load_json(state_path(SCAN_FILE)) or {}
    allowed_roots = scan.get("roots", [])

    # Resolve which units are approved. Approval is granular on purpose:
    #   --id dc_003        a specific unit by its stable id (repeatable)
    #   --path P           a specific unit by path (repeatable)
    #   --category C       a whole category, e.g. "browser cache" (repeatable)
    #   --safe-only        every pure-safe unit (temp/logs/app caches) — no rebuild cost
    #   --rebuildable      every safe-but-rebuildable unit (package/build/browser/MODEL caches)
    #   --all-safe         both of the above (kept for convenience)
    # Pure-safe and rebuildable are deliberately separate: wiping AI models or a
    # toolchain cache is "recoverable but annoying", not the same as clearing temp.
    approved: list[dict] = []
    cats = set(args.category or [])
    ids = set(args.id or [])
    explicit = set(os.path.realpath(os.path.expanduser(p)) for p in (args.path or []))
    for g in plan["groups"]:
        if g["action"] == RISK_NONE:
            continue  # protected groups can never be approved
        want_group = (
            (args.safe_only and g["risk"] == "safe")
            or (args.rebuildable and g["risk"] == "safe_redownload")
            or (args.all_safe and g["risk"] in ("safe", "safe_redownload"))
            or g["category"] in cats
        )
        for item in g["items"]:
            if want_group or item.get("id") in ids \
               or os.path.realpath(item["path"]) in explicit:
                approved.append(item)

    if not approved:
        log("Nothing approved. Approve units with --id dc_003, --path <dir>, "
            "--category \"safe cache\", --safe-only, --rebuildable, or --all-safe. "
            "(Protected items are never eligible.)")
        return 1

    dry = args.dry_run
    total_before = shutil.disk_usage(os.path.expanduser("~"))
    freed_now = 0      # bytes truly reclaimed (direct deletes)
    trashed = 0        # bytes moved to Trash — only freed once the Trash is emptied
    done, skipped = [], []
    progress = {"total": len(approved), "completed": 0, "freed_now": 0,
                "trashed": 0, "dry_run": dry, "started_at": now_iso(),
                "current": ""}
    write_progress(progress)

    log(f"{'DRY RUN — ' if dry else ''}Cleaning {len(approved)} approved unit(s)...")
    for item in approved:
        path = item["path"]
        ok, why = validate_target(path, item["action"], allowed_roots)
        progress["current"] = path
        if not ok:
            skipped.append({**item, "skipped_reason": why})
            log(f"  ✗ skip  {human(item['size']):>9}  {path}  ({why})")
            audit({"ts": now_iso(), "action": "skip", "path": path,
                   "size": item["size"], "reason": why, "dry_run": dry})
            progress["completed"] += 1
            write_progress(progress)
            continue

        size = dir_size(path)
        verb = "trash" if item["action"] == RISK_TRASH else "delete"
        try:
            if not dry:
                if item["action"] == RISK_TRASH:
                    dest = trash_path(path)
                else:
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
                    dest = None
            else:
                dest = "(dry-run)"
            if verb == "trash":
                trashed += size
            else:
                freed_now += size
            done.append({**item, "freed": size, "verb": verb, "dest": dest})
            label = "→ Trash" if verb == "trash" else "delete"
            log(f"  ✓ {label:<7}{human(size):>9}  {path}")
            audit({"ts": now_iso(), "action": verb, "path": path, "size": size,
                   "dest": dest, "dry_run": dry, "category": item["category"]})
        except Exception as e:  # noqa: BLE001 — never abort the whole run on one item
            skipped.append({**item, "skipped_reason": str(e)})
            log(f"  ✗ error {human(item['size']):>9}  {path}  ({e})")
            audit({"ts": now_iso(), "action": "error", "path": path,
                   "size": item["size"], "reason": str(e), "dry_run": dry})
        progress["completed"] += 1
        progress["freed_now"] = freed_now
        progress["trashed"] = trashed
        write_progress(progress)

    total_after = shutil.disk_usage(os.path.expanduser("~"))
    report = {
        "version": 1,
        "generated_at": now_iso(),
        "dry_run": dry,
        "approved_count": len(approved),
        "cleaned_count": len(done),
        "skipped_count": len(skipped),
        # Honest accounting: deletes free space now; trashed items only free space
        # once the Trash is emptied (they sit on the same volume until then).
        "freed_now": freed_now,
        "moved_to_trash": trashed,
        "potential_after_empty_trash": freed_now + trashed,
        "estimated_freed": freed_now,  # back-compat: the truly-reclaimed figure
        "disk_before": {"free": total_before.free, "used": total_before.used,
                        "total": total_before.total},
        "disk_after": {"free": total_after.free, "used": total_after.used,
                       "total": total_after.total},
        "actual_free_delta": total_after.free - total_before.free,
        "cleaned": done,
        "skipped": skipped,
    }
    with open(state_path(REPORT_FILE), "w") as f:
        json.dump(report, f, indent=2)
    progress["current"] = ""
    progress["finished_at"] = now_iso()
    write_progress(progress)
    snap = build_snapshot()

    log("")
    verb = "Would free" if dry else "Freed"
    log(f"{'DRY RUN complete. ' if dry else 'Cleanup complete. '}"
        f"{verb} ~{human(freed_now)} now"
        + (f", and moved ~{human(trashed)} to the Trash "
           f"(frees up once you empty it)." if trashed else ".")
        + f" {len(done)} cleaned, {len(skipped)} skipped.")
    if snap:
        log(f"Visual report (open in any browser): {snap}")
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def cmd_report(args) -> int:
    report = load_json(state_path(REPORT_FILE))
    if not report:
        log("No cleanup report yet. Run `clean` first.")
        return 1
    log("Cleanup report")
    log("=" * 40)
    log(f"When:            {report['generated_at']}")
    log(f"Mode:            {'DRY RUN' if report['dry_run'] else 'real cleanup'}")
    log(f"Units cleaned:   {report['cleaned_count']} (of {report['approved_count']} approved)")
    log(f"Units skipped:   {report['skipped_count']}")
    freed_now = report.get("freed_now", report.get("estimated_freed", 0))
    trashed = report.get("moved_to_trash", 0)
    log(f"Freed now:       ~{human(freed_now)}  (space reclaimed immediately)")
    if trashed:
        log(f"Moved to Trash:  ~{human(trashed)}  (recoverable; frees space when you empty the Trash)")
        log(f"Potential total: ~{human(report.get('potential_after_empty_trash', freed_now + trashed))}  (after emptying the Trash)")
    log(f"Free before:     {human(report['disk_before']['free'])}")
    log(f"Free after:      {human(report['disk_after']['free'])}")
    if report["cleaned"]:
        log("\nCleaned:")
        for c in sorted(report["cleaned"], key=lambda x: -x["freed"])[:20]:
            label = "→Trash" if c["verb"] == "trash" else "delete"
            log(f"  {human(c['freed']):>9}  {label:<6} {c['category']:<22} {c['path']}")
    if report["skipped"]:
        log("\nSkipped (left untouched):")
        for c in report["skipped"][:20]:
            log(f"  {human(c['size']):>9}  {c['category']:<22} {c['path']}  ({c.get('skipped_reason','')})")
    if args.json:
        print(json.dumps(report, indent=2))
    return 0


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

def dashboard_template() -> str | None:
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                       "dashboard", "index.html")
    src = os.path.realpath(src)
    if os.path.exists(src):
        with open(src) as f:
            return f.read()
    return None


def install_dashboard() -> None:
    """Copy the bundled dashboard into the state dir so it can fetch the JSON
    same-origin when served."""
    html = dashboard_template()
    if html is not None:
        with open(state_path("index.html"), "w") as f:
            f.write(html)


def build_snapshot() -> str | None:
    """Write a self-contained report.html with the current data embedded inline,
    so the user can open it by double-click later — no server, no internet, works
    straight off the filesystem (file://)."""
    html = dashboard_template()
    if html is None:
        return None
    embedded = {
        "scan.json": load_json(state_path(SCAN_FILE)),
        "report.json": load_json(state_path(REPORT_FILE)),
        "progress.json": load_json(state_path(PROGRESS_FILE)),
    }
    # `</` is escaped so the JSON can't accidentally close the <script> tag.
    payload = json.dumps(embedded).replace("</", "<\\/")
    inject = f"<script>window.EMBEDDED = {payload};</script>\n</head>"
    html = html.replace("</head>", inject, 1)
    out = state_path("report.html")
    with open(out, "w") as f:
        f.write(html)
    return out


def _sparse(path: str, size: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        if size > 0:
            f.seek(size - 1)
            f.write(b"\0")  # sparse: apparent size = `size`, real blocks ~0


def build_demo() -> str:
    """Create a harmless fake home tree (sparse files — no real disk used) that
    exercises every category, so people can try the tool without any risk."""
    root = state_path("demo-home")
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)
    GB = 1024 ** 3
    MB = 1024 ** 2
    layout = {
        "Library/Caches/com.example.app/blob": 1300 * MB,        # safe cache
        "Library/Logs/system.log": 200 * MB,                     # logs (safe)
        ".cache/huggingface/models/demo-llm/model.bin": 3 * GB,  # model (redownload)
        ".ollama/models/blobs/sha256-demo": 2 * GB,              # model (redownload)
        "project/node_modules/dep/lib.js": 700 * MB,             # build artifacts
        "Library/Developer/Xcode/DerivedData/App/o.o": 900 * MB, # developer cache
        "Library/Application Support/Google/Chrome/Default/Cache/data": 600 * MB,  # browser cache
        "Library/Application Support/Google/Chrome/Default/Cookies": 4 * MB,       # protected
        "Library/Application Support/Google/Chrome/Default/Service Worker/x.db": 250 * MB,  # NOT cache -> review
        ".rustup/toolchains/stable/bin/rustc": 800 * MB,         # toolchain -> review
        "VMs/ubuntu.qcow2": 4 * GB,                              # VM -> review
        "Downloads/installer.dmg": 1200 * MB,                    # review
        ".ssh/id_rsa": 4 * 1024,                                 # protected
        "Documents/thesis.pdf": 300 * MB,                        # protected
    }
    for rel, size in layout.items():
        _sparse(os.path.join(root, rel), size)
    return root


def cmd_snapshot(args) -> int:
    out = build_snapshot()
    if not out:
        log("No data to snapshot yet. Run `scan` first.")
        return 1
    log(f"Self-contained visual report written to:\n  {out}")
    log("Open it in any browser (double-click) — no server or internet needed.")
    return 0


def cmd_serve(args) -> int:
    import http.server
    import socketserver

    install_dashboard()
    directory = state_dir()
    port = args.port

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=directory, **k)

        def log_message(self, *a):  # quiet
            pass

    for attempt in range(20):
        try:
            httpd = socketserver.TCPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        log("Could not bind a local port for the dashboard.")
        return 1

    url = f"http://127.0.0.1:{port}/index.html"
    log(f"Dashboard: {url}")
    log("(Local only — no internet, no external services. Ctrl-C to stop.)")
    if not args.no_open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("\nStopped.")
    return 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def dir_size(path: str) -> int:
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def parse_size(s: str | int) -> int:
    if isinstance(s, int):
        return s
    s = str(s).strip().upper()
    mult = 1
    for suffix, m in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2),
                      ("KB", 1024), ("B", 1), ("T", 1024**4), ("G", 1024**3),
                      ("M", 1024**2), ("K", 1024)):
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * m)
    return int(float(s) * mult)


def load_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def write_progress(p: dict) -> None:
    with open(state_path(PROGRESS_FILE), "w") as f:
        json.dump(p, f)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="disk_cleaner",
        description="Adaptive, safety-first disk investigator and cleaner.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="Measure where space is used (no deletion).")
    sp.add_argument("--root", action="append",
                    help="Root to scan (repeatable). Default: home + temp.")
    sp.add_argument("--min-size", default="200MB",
                    help="Only report dirs at least this big (default 200MB).")
    sp.add_argument("--max-depth", type=int, default=7,
                    help="Maximum directory depth to drill (default 7).")
    sp.add_argument("--json", action="store_true", help="Also print summary JSON.")
    sp.add_argument("--demo", action="store_true",
                    help="Scan a harmless built-in fake home (sparse files) so you "
                         "can try the tool with zero risk to real data.")
    sp.set_defaults(func=cmd_scan)

    pp = sub.add_parser("plan", help="Build an approvable cleanup plan from a scan.")
    pp.add_argument("--json", action="store_true")
    pp.set_defaults(func=cmd_plan)

    cp = sub.add_parser("clean", help="Clean ONLY approved cleanup units.")
    cp.add_argument("--id", action="append",
                    help="Approve a unit by its stable id, e.g. dc_003 (repeatable).")
    cp.add_argument("--path", action="append", help="Approve a specific path (repeatable).")
    cp.add_argument("--category", action="append",
                    help="Approve a whole category, e.g. \"safe cache\" (repeatable).")
    cp.add_argument("--safe-only", action="store_true",
                    help="Approve only pure-safe units (temp, logs, app caches) — "
                         "nothing that costs time/bandwidth to rebuild.")
    cp.add_argument("--rebuildable", action="store_true",
                    help="Approve safe-but-rebuildable units (package/build/browser "
                         "caches, downloaded models). May need re-download/rebuild.")
    cp.add_argument("--all-safe", action="store_true",
                    help="Approve every safe AND rebuildable unit (both of the above).")
    cp.add_argument("--dry-run", action="store_true",
                    help="Show exactly what would happen; delete nothing.")
    cp.set_defaults(func=cmd_clean)

    rp = sub.add_parser("report", help="Print the last cleanup report.")
    rp.add_argument("--json", action="store_true")
    rp.set_defaults(func=cmd_report)

    svp = sub.add_parser("serve", help="Serve the local browser dashboard.")
    svp.add_argument("--port", type=int, default=8765)
    svp.add_argument("--no-open", action="store_true")
    svp.set_defaults(func=cmd_serve)

    snp = sub.add_parser(
        "snapshot",
        help="Write a self-contained report.html you can open by double-click.")
    snp.set_defaults(func=cmd_snapshot)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
