from bizfile_finder import *


class FakeDrive:
    """tree: {parent_id: [ {id,name,mimeType,webViewLink} ]}"""
    calls = 0
    def __init__(self, tree): self.tree = tree
    def children(self, pid, folders_only=False):
        for f in self.tree.get(pid, []):
            if not folders_only or f["mimeType"] == FOLDER_MIME:
                yield f


def F(i, n): return dict(id=i, name=n, mimeType=FOLDER_MIME, webViewLink=f"L{i}")
def D(i, n): return dict(id=i, name=n, mimeType="application/pdf", webViewLink=f"L{i}")


def tree():
    return {
        "pte": [F("ac", "A-C"), F("de", "D-E"), F("g", "GROUPS"), D("x", "readme.pdf")],
        "ac": [F("c1", "Alpha Holdings Pte Ltd"), F("c2", "Acme & Sons Pte. Ltd. - FY06"),
               F("c5", "Both Pte Ltd")],
        "de": [F("c6", "Delta Pte Ltd - FY12"), F("c7", "Delta Pte Ltd - FY03")],
        "g": [F("gg", "Big Group"), F("c4", "Zeta Pte Ltd"), F("c8", "Both Pte Ltd")],
        "gg": [F("c9", "Nested Co Pte Ltd")],
        "c1": [D("f0", "BIZFILE 2023.pdf")], "c2": [D("f2", "random.pdf")],
        "c4": [D("f3", "BIZNET_2024.pdf")], "c5": [D("f4", "Bizfile.pdf")],
        "c6": [D("f5", "Bizfile.pdf")], "c8": [D("f6", "Bizfile.pdf")], "c9": [D("f7", "BIZFILE.pdf")],
    }


def run(client):
    d = FakeDrive(tree())
    return check_client(d, Index(build_index(d, "pte")), client)


def test_alphabetical_without_fy_suffix():
    r = run("ALPHA HOLDINGS PTE. LTD.")
    assert r["status"] == "FOUND" and r["location"] == ALPHA and r["matched_path"] == "A-C/Alpha Holdings Pte Ltd"


def test_fy_variants_pick_latest_and_note():
    r = run("Delta Pte. Ltd.")
    assert r["matched_folder"].endswith("FY12") and "multiple" in r["note"]


def test_no_bizfile_flagged():
    assert run("Acme and Sons")["status"] == "NO_BIZFILE"


def test_groups_fallback_and_nested():
    z = run("Zeta")
    assert z["status"] == "FOUND" and z["location"] == GROUPS
    assert run("Nested Co Pte Ltd")["location"] == GROUPS


def test_alphabetical_wins_over_groups():
    r = run("Both Pte Ltd")
    assert r["location"] == ALPHA


def test_no_folder_only_if_absent_from_both():
    assert run("Nonexistent Co")["status"] == "NO_FOLDER"


def test_fuzzy_needs_review():
    assert run("Alpha Holding Pte Ltd")["status"] == "FOUND_FUZZY_REVIEW"


def test_fka_and_junk(tmp_path):
    assert name_variants("Blue Monk Pte. Ltd. (f.k.a. Atelier Pte. Ltd.)") == \
        ["Blue Monk Pte. Ltd.", "Atelier Pte. Ltd."]
    tree_ = {"pte": [F("a", "A-C")], "a": [F("c1", "Atelier Pte Ltd")], "c1": [D("f0", "BIZFILE.pdf")]}
    d = FakeDrive(tree_)
    idx = Index(build_index(d, "pte"))
    assert check_client(d, idx, "Blue Monk Pte. Ltd. (f.k.a. Atelier Pte. Ltd.)")["status"] == "FOUND"
    p = tmp_path / "c.csv"
    p.write_text("Client Name (x),Status,Link\nClient Name,,\nl,,\nAcme Pte Ltd,Done,u\nBeta Pte Ltd,,\n")
    assert read_clients(str(p)) == ["Acme Pte Ltd", "Beta Pte Ltd"]
    assert read_clients(str(p), skip_done=True) == ["Beta Pte Ltd"]


def test_local_drive(tmp_path):
    g = tmp_path / "PTE Company"
    (g / "A-C" / "Alpha Pte Ltd").mkdir(parents=True)
    (g / "A-C" / "Alpha Pte Ltd" / "BIZFILE.pdf").write_text("x")
    (g / "GROUPS" / "Zeta Pte Ltd").mkdir(parents=True)
    d = LocalDrive()
    idx = Index(build_index(d, str(g)))
    assert check_client(d, idx, "Alpha Pte. Ltd.")["status"] == "FOUND"
    z = check_client(d, idx, "Zeta Pte. Ltd.")
    assert z["status"] == "NO_BIZFILE" and z["location"] == GROUPS


def test_resolve_local_root(tmp_path):
    pte = tmp_path / "Secretarial Work" / "CLIENTS (Corp Sec)" / "PTE Company"
    pte.mkdir(parents=True)
    for r in (tmp_path, tmp_path / "Secretarial Work", tmp_path / "Secretarial Work" / "CLIENTS (Corp Sec)", pte):
        assert resolve_local_root(str(r)) == str(pte)
