#!/usr/bin/env python3
"""
Check which Client-Current organisations have a folder (and a BIZFILE/BIZNET
document) under the Cosec shared drive, WITHOUT copying anything.

Drive path walked (read-only, metadata only, nothing is downloaded/modified):
    Shared Drive -> Secretarial Work -> CLIENTS (Corp Sec) -> PTE Company
        -> <alphabetical group folders> / GROUP -> "<Company name> - FYMM"
            -> BIZFILE / BIZNET file

Input : CSV with the client names (column auto-detected or --name-column).
Output: CSV with status, folder link, bizfile link per client.

Usage:
    python bizfile_finder.py clients.csv -o results.csv
    python bizfile_finder.py clients.csv --name-column "Organization Name"
"""
import argparse
import csv
import difflib
import re
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]  # read-only on purpose
FOLDER_MIME = "application/vnd.google-apps.folder"
PATH_TO_PTE = ["Secretarial Work", "CLIENTS (Corp Sec)", "PTE Company"]
GROUP_FOLDER_NAME = "GROUP"
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


def build_index(drive: Drive, pte_id: str, max_depth: int = 3):
    """
    Walk PTE Company -> letter folders / GROUP (folders only, metadata only).
    Returns list of dicts: {id, name, link, location}. A 'client folder' is any
    folder whose name carries an FY suffix; other folders are recursed into.
    """
    found, stack = [], [(pte_id, "", 0)]
    while stack:
        pid, trail, depth = stack.pop()
        for f in drive.children(pid, folders_only=True):
            path = f"{trail}/{f['name']}" if trail else f["name"]
            if FY_RE.search(f["name"]):
                loc = "GROUP" if path.upper().startswith(GROUP_FOLDER_NAME) else "ALPHABETICAL"
                found.append(dict(id=f["id"], name=f["name"], path=path, location=loc,
                                  link=f.get("webViewLink") or
                                  f"https://drive.google.com/drive/folders/{f['id']}"))
            elif depth < max_depth:
                stack.append((f["id"], path, depth + 1))
    return found


class Index:
    def __init__(self, entries):
        self.by_key = {}
        for e in entries:
            self.by_key.setdefault(normalize(e["name"]), []).append(e)

    def lookup(self, client):
        """-> (match_type, [entries]) ; entries sorted latest FY first."""
        key = normalize(client)
        if key in self.by_key:
            return "EXACT", sorted(self.by_key[key], key=lambda e: -fy_of(e["name"]))
        close = difflib.get_close_matches(key, self.by_key, n=1, cutoff=FUZZY_THRESHOLD)
        if close:
            return "FUZZY", sorted(self.by_key[close[0]], key=lambda e: -fy_of(e["name"]))
        return "NONE", []


# ------------------------------------------------------------------- driver
def read_clients(path, column=None):
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
    return [r[column].strip() for r in rows if r[column].strip()]


def check_client(drive, index, client):
    res = dict(client=client, status="", match_type="", matched_folder="", location="",
               folder_link="", bizfile_name="", bizfile_link="", note="")
    mtype, entries = index.lookup(client)
    if not entries:
        res["status"] = "NO_FOLDER"
        return res
    top = entries[0]
    res.update(match_type=mtype, matched_folder=top["name"], location=top["location"],
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
    ap.add_argument("--shared-drive-id", help="ID of the shared drive holding 'Secretarial Work'")
    ap.add_argument("--pte-folder-id", help="ID of the 'PTE Company' folder (skips path lookup)")
    ap.add_argument("--credentials", default="credentials.json", help="OAuth client secrets")
    ap.add_argument("--token", default="token.json")
    a = ap.parse_args(argv)

    clients = read_clients(a.csv, a.name_column)
    drive = Drive(build_service(a.credentials, a.token))
    pte = resolve_pte_root(drive, a.shared_drive_id, a.pte_folder_id)
    print("Indexing folder names (read-only)...", file=sys.stderr)
    index = Index(build_index(drive, pte))
    print(f"Indexed {sum(map(len, index.by_key.values()))} client folders "
          f"in {drive.calls} API calls", file=sys.stderr)

    results = [check_client(drive, index, c) for c in clients]
    with open(a.output, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
        w.writeheader(); w.writerows(results)

    from collections import Counter
    for k, v in Counter(r["status"] for r in results).items():
        print(f"  {k:20s} {v}", file=sys.stderr)
    print(f"Wrote {a.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
