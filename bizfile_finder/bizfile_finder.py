#!/usr/bin/env python3
"""
Check which Client-Current organisations have a folder (and a BIZFILE/BIZNET
document) under the Cosec shared drive, WITHOUT copying anything.

Drive path walked (read-only, metadata only, nothing is downloaded/modified):
    Shared Drive -> Secretarial Work -> CLIENTS (Corp Sec) -> PTE Company
        -> <alphabetical range folders, e.g. A-C> / <company folder>
       (fallback only if not found there: PTE Company -> GROUPS -> <company folder>)
            -> BIZFILE / BIZNET file

Input : CSV with the client names (column auto-detected or --name-column).
Output: CSV with status, folder link, bizfile link per client.

Usage:
    python bizfile_finder.py clients.csv --local-root "G:\\Shared drives\\X\\Secretarial Work\\CLIENTS (Corp Sec)\\PTE Company"
        (Google Drive for Desktop, Stream mode: no credentials.json / token.json)
    python bizfile_finder.py clients.csv -o results.csv   (Drive API, needs credentials.json)
    python bizfile_finder.py clients.csv --name-column "Organization Name"
"""
import argparse
import csv
import difflib
import os
import re
import sys
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
    return re.sub(r"\s+", " ", s).strip()


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

    def __init__(self, service):
        self.svc = service
        self.calls = 0

    def children(self, parent_id, folders_only=False):
        q = f"'{parent_id}' in parents and trashed = false"
        if folders_only:
            q += f" and mimeType = '{FOLDER_MIME}'"
        token = None
        while True:
            self.calls += 1
            r = self.svc.files().list(
                q=q, pageSize=1000, pageToken=token,
                fields="nextPageToken, files(id, name, mimeType, webViewLink)",
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
        self.calls += 1
        return self.svc.files().list(**kw).execute().get("files", [])


class LocalDrive:
    """Same interface as Drive, but walks a Drive-for-Desktop mount (e.g. G:\\Shared drives\\...).
    Only lists names via os.scandir; file contents are never opened, so nothing is
    downloaded in Stream mode. Ids are absolute paths."""
    calls = 0

    def children(self, parent_id, folders_only=False):
        self.calls += 1
        try:
            entries = sorted(os.scandir(parent_id), key=lambda e: e.name)
        except OSError as exc:
            print(f"warning: cannot list {parent_id}: {exc}", file=sys.stderr)
            return
        for e in entries:
            if e.name.startswith("."):
                continue
            is_dir = e.is_dir()
            if folders_only and not is_dir:
                continue
            yield dict(id=e.path, name=e.name, webViewLink=e.path,
                       mimeType=FOLDER_MIME if is_dir else "file")


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
    return build("drive", "v3", credentials=creds, cache_discovery=False)


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


ALPHA, GROUPS = "ALPHABETICAL", "GROUPS"
GROUPS_RE = re.compile(r"^\s*groups?\s*$", re.I)


def _entry(f, path, location):
    parts = path.split("/")
    if location == ALPHA:
        found_in = f"Alphabetical: {parts[0]}"
    else:
        found_in = "GROUPS" + (f" > {parts[1]}" if len(parts) > 2 else "")
    return dict(id=f["id"], name=f["name"], path=path, location=location, found_in=found_in,
                link=f.get("webViewLink") or f"https://drive.google.com/drive/folders/{f['id']}")


def build_index(drive: Drive, pte_id: str, seen=None):
    """
    PTE Company
      +- <alphabetical range folders: A-C, D-E, ...> / <company folder>   -> ALPHABETICAL
      +- GROUPS / <company folder>  (or GROUPS / <group> / <company>)     -> GROUPS
    Folders only, metadata only. Client folders are NOT required to carry an FY suffix.
    Returns a list of dicts {id, name, path, location, link}.
    """
    found = []
    for top in drive.children(pte_id, folders_only=True):
        is_groups = bool(GROUPS_RE.match(top["name"]))
        loc = GROUPS if is_groups else ALPHA
        if seen is not None:
            seen.append(top["name"])
        for f in drive.children(top["id"], folders_only=True):
            path = f"{top['name']}/{f['name']}"
            if seen is not None:
                seen.append(path)
            found.append(_entry(f, path, loc))
            if is_groups:  # tolerate GROUPS/<group name>/<company>
                for g in drive.children(f["id"], folders_only=True):
                    gpath = f"{path}/{g['name']}"
                    if seen is not None:
                        seen.append(gpath)
                    found.append(_entry(g, gpath, GROUPS))
    return found


class Index:
    """Two pools searched in order: alphabetical folders first, GROUPS only as fallback."""

    def __init__(self, entries):
        self.pools = {ALPHA: {}, GROUPS: {}}
        for e in entries:
            self.pools[e["location"]].setdefault(normalize(e["name"]), []).append(e)

    @property
    def size(self):
        return sum(len(v) for pool in self.pools.values() for v in pool.values())

    def lookup(self, client):
        """-> (match_type, [entries]) ; entries sorted latest FY first.
        Per pool (ALPHABETICAL, then GROUPS): exact on every name variant
        (current + f.k.a.), then fuzzy."""
        keys = [normalize(v) for v in name_variants(client)]
        for loc in (ALPHA, GROUPS):
            pool = self.pools[loc]
            for key in keys:
                if key in pool:
                    return "EXACT", sorted(pool[key], key=lambda e: -fy_of(e["name"]))
            for key in keys:
                close = difflib.get_close_matches(key, pool, n=1, cutoff=FUZZY_THRESHOLD)
                if close:
                    return "FUZZY", sorted(pool[close[0]], key=lambda e: -fy_of(e["name"]))
        return "NONE", []


# ------------------------------------------------------------------- driver
def read_clients(path, column=None, skip_done=False):
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
    status_col = next((c for c in cols if c.strip().lower() == "status"), None)
    names, seen = [], set()
    for r in rows:
        n = r[column].strip()
        if is_junk(n) or n.lower() in seen:
            continue
        if skip_done and status_col and r[status_col].strip().lower() == "done":
            continue
        seen.add(n.lower())
        names.append(n)
    return names


def check_client(drive, index, client):
    res = dict(client=client, status="", match_type="", matched_folder="", matched_path="", location="", found_in="",
               folder_link="", bizfile_name="", bizfile_link="", note="")
    mtype, entries = index.lookup(client)
    if not entries:
        res["status"] = "NO_FOLDER"
        return res
    top = entries[0]
    res.update(match_type=mtype, matched_folder=top["name"], matched_path=top["path"], location=top["location"],
               found_in=top["found_in"],
               folder_link=top["link"])
    if len(entries) > 1:
        res["note"] = "multiple folders: " + "; ".join(e["path"] for e in entries)
    # only list the matched folder's files (few API calls, nothing downloaded)
    files = [f for f in drive.children(top["id"]) if is_bizfile(f["name"])
             and f["mimeType"] != FOLDER_MIME]
    if not files:
        res["status"] = "NO_BIZFILE"           # flag: bizfile missing
        return res
    files.sort(key=lambda f: f["name"])
    res.update(bizfile_name=files[0]["name"], bizfile_link=files[0].get("webViewLink", ""))
    res["status"] = "FOUND" if mtype == "EXACT" else "FOUND_FUZZY_REVIEW"
    if len(files) > 1:
        res["note"] = (res["note"] + " | " if res["note"] else "") + \
            "multiple bizfiles: " + "; ".join(f["name"] for f in files)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("-o", "--output", default="bizfile_results.csv")
    ap.add_argument("--name-column")
    ap.add_argument("--skip-done", action="store_true",
                    help="skip rows whose Status column is already 'Done'")
    ap.add_argument("--shared-drive-id", help="ID of the shared drive holding 'Secretarial Work'")
    ap.add_argument("--pte-folder-id", help="ID of the 'PTE Company' folder (skips path lookup)")
    ap.add_argument("--dump-folders", metavar="FILE",
                    help="write every folder path visited to FILE (for diagnosing 0 matches)")
    ap.add_argument("--local-root", help="path to the 'PTE Company' folder as mounted by Google "
                    "Drive for Desktop (Stream mode). No credentials/token needed.")
    ap.add_argument("--credentials", default="credentials.json", help="OAuth client secrets")
    ap.add_argument("--token", default="token.json")
    a = ap.parse_args(argv)

    clients = read_clients(a.csv, a.name_column, a.skip_done)
    if a.local_root:
        if not os.path.isdir(a.local_root):
            sys.exit(f"--local-root is not a folder: {a.local_root!r}")
        drive, pte = LocalDrive(), resolve_local_root(a.local_root)
        print(f"Using PTE Company folder: {pte}", file=sys.stderr)
    else:
        drive = Drive(build_service(a.credentials, a.token))
        pte = resolve_pte_root(drive, a.shared_drive_id, a.pte_folder_id)
    print("Indexing folder names (read-only)...", file=sys.stderr)
    seen = []
    index = Index(build_index(drive, pte, seen=seen))
    n_idx = index.size
    print(f"Indexed {n_idx} client folders (of {len(seen)} folders seen) "
          f"in {drive.calls} listing calls", file=sys.stderr)
    if a.dump_folders:
        Path(a.dump_folders).write_text("\n".join(seen), encoding="utf-8")
        print(f"Wrote folder names seen to {a.dump_folders}", file=sys.stderr)
    if n_idx < max(1, len(clients) // 4):
        print("\nWARNING: very few client folders recognised. Folder names seen "
              f"(first 25 of {len(seen)}):", file=sys.stderr)
        for x in seen[:25]:
            print("   ", x, file=sys.stderr)
        print("Expected: PTE Company/<A-C ...>/<company> and PTE Company/GROUPS/<company>. "
              "Check the 'Using PTE Company folder' line, or use --dump-folders seen.txt.\n", file=sys.stderr)

    results = [check_client(drive, index, c) for c in clients]
    with open(a.output, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
        w.writeheader(); w.writerows(results)

    from collections import Counter
    for k, v in Counter(r["status"] for r in results).items():
        print(f"  {k:20s} {v}", file=sys.stderr)
    print("Found in:", dict(Counter(r["location"] for r in results if r["location"])),
          file=sys.stderr)
    print(f"Wrote {a.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
