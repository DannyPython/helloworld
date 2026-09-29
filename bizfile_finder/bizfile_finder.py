#!/usr/bin/env python3
"""
Check which Client-Current organisations have a folder (and a BIZFILE/BIZNET
document) under the Cosec shared drive, WITHOUT copying anything.

Method (read-only; only names are listed, nothing is downloaded/modified):
    Folders under  Secretarial Work -> CLIENTS (Corp Sec) -> PTE Company  are filed inconsistently
    (A-C, D-F ... Y-Z, GROUPS, sub-folders inside those, ...), so the program does a full
    walk (Dijkstra, shallowest first, listing folders in parallel) of EVERY sub-folder at every depth, indexes each folder plus the
    BIZFILE/BIZNET files sitting in it, then looks for the client's folder anywhere in that index.
    If a name exists in several places, the copy that has a bizfile wins, then alphabetical over
    GROUPS, then latest FY, then shallowest; all other locations are listed in `note`.

Input : the client CSV (same layout as the Google Sheet:
        Client Name | Status | Connected folder link).
Output: same three columns first (same order, same row order, ready to paste back),
        then details: check_result, found_in, matched_path, bizfile, note ...
        Status = "Done" only when the folder AND the BIZFILE/BIZNET were found with an
        exact name match; everything else is "not done" and check_result says why.

Usage:
    python bizfile_finder.py clients.csv --local-root "H:/" -o results.csv        # Drive for Desktop
    python bizfile_finder.py clients.csv -o results.csv                           # Drive API (credentials.json)
    python bizfile_finder.py clients.csv --local-root "H:/" --compare             # check against manual Status
"""
import argparse
import bisect
import csv
import difflib
import heapq
import json
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]  # read-only on purpose
FOLDER_MIME = "application/vnd.google-apps.folder"
PATH_TO_PTE = ["Secretarial Work", "CLIENTS (Corp Sec)", "PTE Company"]
FUZZY_THRESHOLD = 0.90
NAME_COLUMN_HINTS = ("organization", "organisation", "client", "company", "name")

_SUFFIXES = r"\b(private limited|pte\.? ?ltd\.?|pte|ltd|limited|llp|inc|corp|co)\b"


# ----------------------------------------------------------------- matching
def normalize(name: str) -> str:
    """Lower-case, drop FY suffix, legal suffixes and punctuation."""
    s = strip_fy(name).lower().replace("&", " and ")
    s = re.sub(_SUFFIXES, " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"^the ", "", s)


FY_RE = re.compile(r"\s*[-–—]?\s*FY\s?(\d{2,4})\s*$", re.I)


def strip_fy(name: str) -> str:
    return FY_RE.sub("", name).strip()


def fy_of(name: str) -> int:
    m = FY_RE.search(name)
    if not m:
        return -1
    y = int(m.group(1))
    return y + 2000 if y < 100 else y


FKA_RE = re.compile(r"\(\s*(?:f\.?k\.?a\.?|formerly(?: known as)?)\s*:?\s*(.+?)\s*\)", re.I)
JUNK_NAMES = {"client name", "client", "name", "organization", "organisation"}


def name_variants(client: str):
    """'New Co (f.k.a. Old Co)' -> ['New Co', 'Old Co']; the folder may carry either name."""
    variants = [FKA_RE.sub("", client).strip()]
    variants += [m.strip() for m in FKA_RE.findall(client)]
    return [v for v in variants if v]


def is_junk(name: str) -> bool:
    return len(name) < 3 or name.lower() in JUNK_NAMES


def is_bizfile(name: str) -> bool:
    return bool(re.search(r"biz\s*file|biz\s*net", name, re.I))


# -------------------------------------------------------------- Drive access
class Drive:
    """Thin read-only wrapper: every call is a files.list on a single parent."""

    def __init__(self, service_factory):
        """service_factory() -> a NEW googleapiclient service. The library is not thread-safe,
        so every worker thread lazily builds and keeps its own."""
        self._factory = service_factory
        self._local = threading.local()
        self._lock = threading.Lock()
        self.calls = 0

    @property
    def svc(self):
        if not hasattr(self._local, "svc"):
            self._local.svc = self._factory()
        return self._local.svc

    def _count(self):
        with self._lock:
            self.calls += 1

    def children(self, parent_id, folders_only=False):
        q = f"'{parent_id}' in parents and trashed = false"
        if folders_only:
            q += f" and mimeType = '{FOLDER_MIME}'"
        token = None
        while True:
            self._count()
            r = self.svc.files().list(
                q=q, pageSize=1000, pageToken=token,
                fields="nextPageToken, files(id, name, mimeType, webViewLink, modifiedTime)",
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            yield from r.get("files", [])
            token = r.get("nextPageToken")
            if not token:
                break

    def find_folder(self, name, parent_id=None, drive_id=None):
        esc = name.replace("\\", "\\\\").replace("'", "\\'")
        q = f"name = '{esc}' and mimeType = '{FOLDER_MIME}' and trashed = false"
        if parent_id:
            q += f" and '{parent_id}' in parents"
        kw = dict(q=q, fields="files(id, name, parents)", supportsAllDrives=True,
                  includeItemsFromAllDrives=True, pageSize=50)
        if drive_id:
            kw.update(corpora="drive", driveId=drive_id)
        else:
            kw.update(corpora="allDrives")
        self._count()
        return self.svc.files().list(**kw).execute().get("files", [])


class LocalDrive:
    """Same interface as Drive, but walks a Drive-for-Desktop mount (e.g. G:\\Shared drives\\...).
    Only lists names via os.scandir; file contents are never opened, so nothing is
    downloaded in Stream mode. Ids are absolute paths."""
    calls = 0
    _lock = threading.Lock()

    def children(self, parent_id, folders_only=False):
        with self._lock:
            self.calls += 1
        try:
            entries = sorted(os.scandir(parent_id), key=lambda e: e.name)
        except OSError as exc:
            print(f"warning: cannot list {parent_id}: {exc}", file=sys.stderr)
            return
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                is_dir = e.is_dir(follow_symlinks=False)   # never follow links (no cycles)
            except OSError:
                continue
            if folders_only and not is_dir:
                continue
            item = dict(id=e.path, name=e.name, webViewLink=e.path,
                        mimeType=FOLDER_MIME if is_dir else "file")
            if not is_dir and is_bizfile(e.name):           # only these are ever stat'ed
                try:
                    item["modifiedTime"] = datetime.fromtimestamp(e.stat().st_mtime).isoformat()
                except OSError:
                    pass
            yield item


def resolve_local_root(root: str) -> str:
    """Accept H:\\, H:\\Secretarial Work, ...\\CLIENTS (Corp Sec) or ...\\PTE Company itself."""
    if os.path.basename(os.path.normpath(root)).lower() == PATH_TO_PTE[-1].lower():
        return root
    for i in range(len(PATH_TO_PTE)):
        cur = root
        for name in PATH_TO_PTE[i:]:
            hit = next((e.name for e in _safe_scandir(cur)
                        if e.is_dir() and e.name.lower() == name.lower()), None)
            if hit is None:
                break
            cur = os.path.join(cur, hit)
        else:
            return cur
    sys.exit(f"Could not find 'PTE Company' under {root!r}. "
             "Point --local-root at the PTE Company folder itself.")


def _safe_scandir(path):
    try:
        return list(os.scandir(path))
    except OSError:
        return []


def build_service(credentials_file, token_file):
    """Authenticate once; returns a factory that builds a fresh Drive service per thread."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file(
                credentials_file, SCOPES).run_local_server(port=0)
        Path(token_file).write_text(creds.to_json())
    return lambda: build("drive", "v3", credentials=creds, cache_discovery=False)


# --------------------------------------------------------------- index build
def resolve_pte_root(drive: Drive, shared_drive_id=None, pte_folder_id=None):
    if pte_folder_id:
        return pte_folder_id
    parent = shared_drive_id
    for name in PATH_TO_PTE:
        hits = drive.find_folder(name, parent, shared_drive_id)
        if not hits:
            sys.exit(f"Folder not found: {name!r} (under {parent or 'any drive'}). "
                     "Pass --pte-folder-id to skip path resolution.")
        if len(hits) > 1 and parent is None:
            print(f"warning: {len(hits)} folders named {name!r}; using first. "
                  "Use --shared-drive-id or --pte-folder-id to be exact.", file=sys.stderr)
        parent = hits[0]["id"]
    return parent


ALPHA, GROUPS, OTHER = "ALPHABETICAL", "GROUPS", "OTHER"
LOC_RANK = {ALPHA: 0, GROUPS: 1, OTHER: 2}
GROUPS_RE = re.compile(r"^\s*groups?\s*$", re.I)
RANGE_RE = re.compile(r"^\s*[A-Za-z]\s*[-–]\s*[A-Za-z]\s*$")     # A-C, D-F, ...
ROMAN_RE = re.compile(r"^[ivx]+$")


def confusable(a: str, b: str) -> bool:
    """True if two normalised names differ only in a distinguishing token (letter, digit,
    roman numeral): 'sgsupergreen a' vs 'sgsupergreen b', 'tangerine capital i' vs 'ii'.
    Such pairs are different companies, so fuzzy matching must never join them."""
    def marks(k):
        toks = k.split()
        return sorted(t for t in toks if len(t) == 1 or ROMAN_RE.match(t)) + re.findall(r"\d+", k)
    return marks(a) != marks(b)


def classify(top: str) -> str:
    if GROUPS_RE.match(top):
        return GROUPS
    return ALPHA if RANGE_RE.match(top) else OTHER


def _entry(f, parts, location):
    path = "/".join(parts)
    label = {ALPHA: f"Alphabetical: {parts[0]}", GROUPS: "GROUPS"}.get(location, f"Other: {parts[0]}")
    inner = "/".join(parts[1:-1])
    return dict(id=f["id"], name=f["name"], path=path, depth=len(parts), location=location,
                found_in=label + (f" > {inner}" if inner else ""), bizfiles=[],
                link=f.get("webViewLink") or f"https://drive.google.com/drive/folders/{f['id']}")


def _scan(drive, pid):
    """One listing -> (sub-folders, [(bizfile name, link, modified)]). Thread-safe."""
    subs, biz = [], []
    for k in sorted(drive.children(pid), key=lambda k: k["name"].lower()):
        if k["mimeType"] == FOLDER_MIME:
            subs.append(k)
        elif is_bizfile(k["name"]):
            biz.append((k["name"], k.get("webViewLink", ""), k.get("modifiedTime", "")))
    return subs, biz


CHECKPOINT_VERSION = 1


def _save_checkpoint(path, root_id, entries, seen, visited, heap, scanned):
    """Atomically write the whole scan state (index + queue of folders still to list)."""
    pos = {id(e): i for i, e in enumerate(entries)}
    data = dict(version=CHECKPOINT_VERSION, root=root_id, scanned=scanned, complete=not heap,
                entries=entries, seen=seen, visited=sorted(visited),
                frontier=[[c, t, fid, parts, pos.get(id(e), -1)] for c, t, fid, parts, e in heap])
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _load_checkpoint(path, root_id):
    """-> (entries, seen, visited, heap, scanned) or None if absent/for another root/unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    if d.get("version") != CHECKPOINT_VERSION or d.get("root") != root_id:
        print(f"warning: {path} belongs to a different folder/version - ignoring it", file=sys.stderr)
        return None
    entries = d["entries"]
    for e in entries:
        e["bizfiles"] = [tuple(b) for b in e["bizfiles"]]
    heap = [(c, t, fid, parts, entries[i] if i >= 0 else None) for c, t, fid, parts, i in d["frontier"]]
    heapq.heapify(heap)
    return entries, d["seen"], set(d["visited"]), heap, d["scanned"]


def build_index(drive, root_id: str, seen=None, max_depth=None, progress_every=0,
                algo="dijkstra", workers=1, prune=False, checkpoint=None, checkpoint_every=60,
                resume=False, scan=True, should_stop=None, info=None, chunk=None):
    """
    Walk EVERY folder below PTE Company (range folders A-C.., GROUPS, anything else, any depth).
    One listing per folder; metadata only, nothing is opened/downloaded.
    Each folder becomes an entry {name, path, location, found_in, link, bizfiles=[...]} where
    bizfiles are the BIZFILE/BIZNET *files sitting directly in that folder*. The range folders /
    GROUPS themselves are containers, not companies, so they are not indexed.

    algo="dijkstra": priority queue keyed by cost = depth (every folder step costs 1), so folders
        are settled shallowest-first. Folders at the same cost are independent, so they are listed
        concurrently by `workers` threads - that is where the speed-up comes from (listing
        latency, not traversal order, is the bottleneck).
    algo="dfs": the old depth-first order (kept for comparison; no checkpointing).
    prune=True: do not descend below a folder that directly contains a BIZFILE/BIZNET file.

    Checkpointing (dijkstra only): with `checkpoint=<file>` the full state - index so far AND the
    queue of folders still to list - is saved every `checkpoint_every` seconds, when `should_stop()`
    turns true, on Ctrl+C/any error, and at the end. `resume=True` continues from that file;
    `scan=False` loads it as-is and lists nothing more. info["complete"] tells if the walk finished.
    """
    entries, visited, t0 = [], {root_id}, time.time()
    heap = [(0, "", root_id, [], None)]        # (cost, tie-break, folder id, path parts, entry)
    seen_paths, scanned = [], 0
    if checkpoint and algo != "dfs" and (resume or not scan):
        st = _load_checkpoint(checkpoint, root_id)
        if st:
            entries, seen_paths, visited, heap, scanned = st
            print(f"Loaded checkpoint: {len(entries)} folders indexed, {scanned} listed, "
                  f"{len(heap)} still queued", file=sys.stderr)
        elif not scan:
            sys.exit(f"No usable checkpoint at {checkpoint!r}")
    state = dict(scanned=scanned, done_now=0)

    def visit(item, subs, biz):
        """Apply one finished listing; return the child work items to schedule."""
        _, _, _, parts, entry = item
        if entry is not None:
            entry["bizfiles"].extend(biz)
        state["scanned"] += 1
        state["done_now"] += 1
        if progress_every and state["scanned"] % progress_every == 0:
            print(f"  ... {state['scanned']} folders scanned, {len(heap)} queued "
                  f"({time.time() - t0:.0f}s)", file=sys.stderr)
        if (max_depth is not None and len(parts) >= max_depth) or (prune and biz):
            return []
        kids = []
        for k in subs:
            if k["id"] in visited:            # cycle / duplicate-parent guard
                continue
            visited.add(k["id"])
            kparts = parts + [k["name"]]
            seen_paths.append("/".join(kparts))
            loc = classify(kparts[0])
            e = None
            if not (len(kparts) == 1 and loc in (ALPHA, GROUPS)):
                e = _entry(k, kparts, loc)
                entries.append(e)
            kids.append((len(kparts), "/".join(kparts).lower(), k["id"], kparts, e))
        return kids

    def save():
        if checkpoint and algo != "dfs":
            _save_checkpoint(checkpoint, root_id, entries, seen_paths, visited, heap, state["scanned"])

    pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    chunk = chunk or max(32, workers * 16)
    try:
        if algo == "dfs":
            stack = [heap[0]]
            while stack:
                item = stack.pop()
                subs, biz = _scan(drive, item[2])
                stack.extend(reversed(visit(item, subs, biz)))
            heap.clear()
        else:
            last_save = time.time()
            while heap and scan:
                if should_stop and should_stop():
                    break
                cost, batch = heap[0][0], []
                while heap and heap[0][0] == cost and len(batch) < chunk:   # shallowest-first
                    batch.append(heapq.heappop(heap))
                ids = [it[2] for it in batch]
                try:
                    results = list(pool.map(lambda pid: _scan(drive, pid), ids)) if pool else [
                        _scan(drive, pid) for pid in ids]
                except BaseException:            # Ctrl+C / API error: keep the queue consistent
                    for it in batch:
                        heapq.heappush(heap, it)
                    raise
                for item, (subs, biz) in zip(batch, results):
                    for kid in visit(item, subs, biz):
                        heapq.heappush(heap, kid)
                if checkpoint and time.time() - last_save >= checkpoint_every:
                    save()
                    last_save = time.time()
    finally:
        if pool:
            pool.shutdown(wait=False, cancel_futures=True)
        save()
    if info is not None:
        info.update(complete=not heap, queued=len(heap), scanned=state["scanned"])
    if seen is not None:
        seen.extend(seen_paths)
    return entries


class Index:
    def __init__(self, entries):
        self.by_key = {}
        for e in entries:
            self.by_key.setdefault(normalize(e["name"]), []).append(e)
        self._sorted = sorted(entries, key=lambda e: e["path"])
        self._paths = [e["path"] for e in self._sorted]
        self.size = len(entries)

    def descendants(self, e):
        prefix = e["path"] + "/"
        i = bisect.bisect_left(self._paths, prefix)
        while i < len(self._paths) and self._paths[i].startswith(prefix):
            yield self._sorted[i]
            i += 1

    def bizfiles_of(self, e):
        """(files, relative_subpath): files directly in the folder, else those in its
        shallowest descendant folder that has any."""
        if e["bizfiles"]:
            return e["bizfiles"], ""
        subs = sorted((d for d in self.descendants(e) if d["bizfiles"]),
                      key=lambda d: (d["depth"], d["path"]))
        if subs:
            return subs[0]["bizfiles"], subs[0]["path"][len(e["path"]) + 1:]
        return [], ""

    def rank(self, e):
        files, _ = self.bizfiles_of(e)
        return (0 if files else 1, LOC_RANK[e["location"]], -fy_of(e["name"]), e["depth"], e["path"])

    def lookup(self, client):
        """-> (match_type, entries) over ALL folders at any depth. Exact on every name variant
        (current + f.k.a.) first, then guarded fuzzy."""
        keys = [normalize(v) for v in name_variants(client)]
        for key in keys:
            if key in self.by_key:
                return "EXACT", list(self.by_key[key])
        for key in keys:
            for cand in difflib.get_close_matches(key, self.by_key, n=5, cutoff=FUZZY_THRESHOLD):
                if not confusable(key, cand):
                    return "FUZZY", list(self.by_key[cand])
        return "NONE", []


# ------------------------------------------------------------------- driver
STATUS_DONE, STATUS_TODO = "Done", "not done"


def read_rows(path, column=None):
    """Every CSV row in original order -> (header_name_col, header_status, header_link, rows).
    Each row: dict(name, status, link, skip)  where skip is '' or a check_result string."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        sys.exit("CSV is empty")
    cols = list(rows[0].keys())
    if column is None:
        column = next((c for h in NAME_COLUMN_HINTS for c in cols if h in c.lower()), cols[0])
        print(f"Using name column: {column!r}", file=sys.stderr)
    elif column not in cols:
        sys.exit(f"Column {column!r} not in CSV. Available: {cols}")
    scol = next((c for c in cols if c.strip().lower() == "status"), None)
    lcol = next((c for c in cols if "link" in c.lower()), None)
    out, seen = [], set()
    for r in rows:
        name = (r[column] or "").strip()
        if not name:
            continue
        row = dict(name=name, status=(r.get(scol) or "").strip() if scol else "",
                   link=(r.get(lcol) or "").strip() if lcol else "", skip="")
        if is_junk(name):
            row["skip"] = "SKIPPED_NOT_A_CLIENT"
        elif name.lower() in seen:
            row["skip"] = "DUPLICATE"
        seen.add(name.lower())
        out.append(row)
    return column, scol or "Status", lcol or "Connected folder link", out


def read_clients(path, column=None, skip_done=False):
    """Names only (junk/duplicates dropped; optionally rows already 'Done')."""
    _, _, _, rows = read_rows(path, column)
    return [r["name"] for r in rows if not r["skip"]
            and not (skip_done and r["status"].lower() == "done")]


def check_client(index, client):
    res = dict(client=client, check_result="", match_type="", found_in="", matched_folder="",
               matched_path="", location="", folder_link="", bizfile_name="", bizfile_link="",
               note="")
    mtype, entries = index.lookup(client)
    if not entries:
        res["check_result"] = "NO_FOLDER"
        return res
    ranked = sorted(entries, key=index.rank)            # bizfile present > alphabetical > GROUPS > latest FY > shallow
    top = ranked[0]
    res.update(match_type=mtype, matched_folder=top["name"], matched_path=top["path"],
               location=top["location"], found_in=top["found_in"], folder_link=top["link"])
    notes = []
    if len(ranked) > 1:
        notes.append("other folders with this name: " + "; ".join(e["path"] for e in ranked[1:]))
    files, sub = index.bizfiles_of(top)
    if not files:
        res["check_result"] = "NO_BIZFILE"            # flag: bizfile missing
        res["note"] = " | ".join(notes)
        return res
    if sub:
        notes.append(f"bizfile found in sub-folder {sub!r}")
    files = sorted(files, key=lambda f: (f[2], f[0]), reverse=True)   # newest first
    res.update(bizfile_name=files[0][0], bizfile_link=files[0][1])
    res["check_result"] = "FOUND" if mtype == "EXACT" else "FOUND_FUZZY_REVIEW"
    if mtype != "EXACT":
        notes.append("name only approximately matches - verify before connecting")
    if len(files) > 1:
        notes.append("multiple bizfiles (newest used): " + "; ".join(f[0] for f in files))
    res["note"] = " | ".join(notes)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("-o", "--output", default="bizfile_results.csv")
    ap.add_argument("--name-column")
    ap.add_argument("--skip-done", action="store_true",
                    help="do not re-check rows whose Status is already 'Done'")
    ap.add_argument("--compare", action="store_true",
                    help="treat the CSV's Done/not done Status as the manual answer, re-check every "
                         "row and report where the program disagrees")
    ap.add_argument("--shared-drive-id", help="ID of the shared drive holding 'Secretarial Work'")
    ap.add_argument("--pte-folder-id", help="ID of the 'PTE Company' folder (skips path lookup)")
    ap.add_argument("--algo", choices=("dijkstra", "dfs"), default="dijkstra",
                    help="folder traversal order (default dijkstra = shallowest first)")
    ap.add_argument("--workers", type=int, default=8,
                    help="folders listed concurrently (default 8; use 1 to disable parallelism)")
    ap.add_argument("--prune", action="store_true",
                    help="do not descend below a folder that directly contains BIZFILE/BIZNET (faster)")
    ap.add_argument("--cache", default="bizfile_index.json", metavar="FILE",
                    help="checkpoint file: the scan is saved here (default bizfile_index.json)")
    ap.add_argument("--no-cache", action="store_true", help="do not write a checkpoint file")
    ap.add_argument("--resume", action="store_true",
                    help="continue the scan from the checkpoint (already-done folders are not re-listed)")
    ap.add_argument("--fresh", action="store_true", help="ignore/overwrite an existing checkpoint")
    ap.add_argument("--match-only", action="store_true",
                    help="use the checkpoint as it is (even if incomplete); list nothing more")
    ap.add_argument("--checkpoint-every", type=int, default=60, metavar="SEC")
    ap.add_argument("--max-depth", type=int, help="optional safety limit on folder depth below PTE Company")
    ap.add_argument("--dump-folders", metavar="FILE",
                    help="write every folder path visited to FILE (for diagnosing 0 matches)")
    ap.add_argument("--local-root", help="path to the drive (e.g. H:/) or to the 'PTE Company' folder "
                    "as mounted by Google Drive for Desktop (Stream mode). No credentials needed.")
    ap.add_argument("--credentials", default="credentials.json", help="OAuth client secrets")
    ap.add_argument("--token", default="token.json")
    a = ap.parse_args(argv)

    name_col, status_col, link_col, rows = read_rows(a.csv, a.name_column)
    if a.local_root:
        if not os.path.isdir(a.local_root):
            sys.exit(f"--local-root is not a folder: {a.local_root!r}")
        drive, pte = LocalDrive(), resolve_local_root(a.local_root)
        print(f"Using PTE Company folder: {pte}", file=sys.stderr)
    else:
        drive = Drive(build_service(a.credentials, a.token))
        pte = resolve_pte_root(drive, a.shared_drive_id, a.pte_folder_id)
    print("Indexing folder names (read-only)...", file=sys.stderr)
    seen, info = [], {}
    cache = None if (a.no_cache or a.algo == "dfs") else a.cache
    if cache and os.path.exists(cache) and not (a.resume or a.fresh or a.match_only):
        sys.exit(f"A checkpoint already exists: {cache}\n"
                 "  --resume       continue where it stopped (nothing already scanned is repeated)\n"
                 "  --match-only   just match the CSV against what was scanned so far\n"
                 "  --fresh        throw it away and start over")
    stop = {"now": False}

    def on_sigint(signum, frame):
        stop["now"] = True
        signal.signal(signal.SIGINT, signal.SIG_DFL)            # a 2nd Ctrl+C aborts immediately
        print("\nStop requested: finishing the current batch and saving the checkpoint "
              "(Ctrl+C again = abort now; the last autosave is kept)...", file=sys.stderr)
    try:
        signal.signal(signal.SIGINT, on_sigint)
    except ValueError:                                          # not the main thread
        pass
    t0 = time.time()
    index = Index(build_index(drive, pte, seen=seen, max_depth=a.max_depth, progress_every=500,
                              algo=a.algo, workers=max(1, a.workers), prune=a.prune,
                              checkpoint=cache, checkpoint_every=a.checkpoint_every,
                              resume=a.resume and not a.fresh, scan=not a.match_only,
                              should_stop=lambda: stop["now"], info=info))
    tops = sorted({p.split("/")[0] for p in seen})
    print(f"Top-level folders: {', '.join(tops)}", file=sys.stderr)
    print(f"Scanned {len(seen)} folders in {drive.calls} listings, {time.time() - t0:.0f}s "
          f"({a.algo}, {a.workers} workers, metadata only)", file=sys.stderr)
    if a.dump_folders:
        Path(a.dump_folders).write_text("\n".join(seen), encoding="utf-8")
        print(f"Wrote folder names seen to {a.dump_folders}", file=sys.stderr)
    partial = not info.get("complete", True)
    if partial:
        print(f"\n*** SCAN INCOMPLETE: {info.get('queued', '?')} folders were still queued. ***\n"
              "    'NOT_FOUND_SO_FAR' below only means 'not seen yet', not 'missing'.", file=sys.stderr)
        if cache:
            print(f"    Checkpoint saved to {cache}. Continue later with the same command plus --resume.\n",
                  file=sys.stderr)
    n_todo = sum(1 for r in rows if not r["skip"])
    if index.size < max(1, n_todo // 4) and not partial:
        print("\nWARNING: very few folders found. Folder names seen "
              f"(first 25 of {len(seen)}):", file=sys.stderr)
        for x in seen[:25]:
            print("   ", x, file=sys.stderr)
        print("Check the 'Using PTE Company folder' line, or use --dump-folders.\n", file=sys.stderr)

    fields = [name_col, status_col, link_col, "check_result", "found_in", "matched_folder",
              "matched_path", "match_type", "bizfile_name", "bizfile_link", "note"]
    if a.compare:
        fields += ["manual_status", "agrees"]
    results, mismatches = [], []
    for r in rows:
        manual = r["status"].lower()
        if r["skip"]:
            res = dict(client=r["name"], check_result=r["skip"])
            status, link = r["status"], r["link"]
        elif r["status"].lower() == "done" and a.skip_done and not a.compare:
            res = dict(client=r["name"], check_result="SKIPPED_ALREADY_DONE")
            status, link = r["status"], r["link"]
        else:
            res = check_client(index, r["name"])
            if partial and res["check_result"] == "NO_FOLDER":
                res.update(check_result="NOT_FOUND_SO_FAR", note="scan incomplete - may still be found")
            status = STATUS_DONE if res["check_result"] == "FOUND" else STATUS_TODO
            link = res["folder_link"]
        row = {name_col: r["name"], status_col: status, link_col: link}
        for k in fields[3:]:
            row[k] = res.get(k, "")
        if a.compare:
            row["manual_status"] = r["status"]
            known = manual in ("done", "not done")
            ok = "" if not known or r["skip"] else ("yes" if manual == status.lower() else "NO")
            row["agrees"] = ok
            if ok == "NO":
                mismatches.append(row)
        results.append(row)

    with open(a.output, "w", newline="", encoding="utf-8-sig") as fh:   # utf-8-sig: opens cleanly in Excel
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(results)

    from collections import Counter
    print("\nResult", file=sys.stderr)
    for k, v in Counter(x["check_result"] for x in results).items():
        print(f"  {k:24s} {v}", file=sys.stderr)
    print("Found in:", dict(Counter(x["found_in"].split(":")[0].split(" >")[0]
                                   for x in results if x["found_in"])), file=sys.stderr)
    if a.compare:
        cmp_rows = [x for x in results if x["agrees"]]
        print(f"\nCompare with manual Status: {sum(x['agrees']=='yes' for x in cmp_rows)}/"
              f"{len(cmp_rows)} agree", file=sys.stderr)
        for x in mismatches:
            print(f"  MISMATCH  {x[name_col]!r}: manual={x['manual_status']!r} "
                  f"program={x[status_col]!r} ({x['check_result']}) {x['note']}", file=sys.stderr)
    print(f"Wrote {a.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
