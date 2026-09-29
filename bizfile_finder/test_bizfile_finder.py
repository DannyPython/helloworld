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


def test_all():
    tree = {
        "pte": [F("a", "A"), F("g", "GROUP")],
        "a": [F("c1", "Alpha Holdings Pte Ltd - FY03"), F("c2", "Alpha Holdings Pte Ltd - FY12"),
              F("c3", "Acme & Sons Pte. Ltd. - FY06")],
        "g": [F("gg", "Big Group"), ],
        "gg": [F("c4", "Zeta Pte Ltd - FY12")],
        "c1": [D("f0", "BIZFILE 2023.pdf")], "c2": [D("f1", "Bizfile.pdf")],
        "c3": [D("f2", "random.pdf")], "c4": [D("f3", "BIZNET_2024.pdf")],
    }
    d = FakeDrive(tree)
    idx = Index(build_index(d, "pte"))
    r = check_client(d, idx, "ALPHA HOLDINGS PTE. LTD.")
    assert r["status"] == "FOUND" and r["matched_folder"].endswith("FY12") and "multiple" in r["note"]
    assert check_client(d, idx, "Acme and Sons")["status"] == "NO_BIZFILE"
    z = check_client(d, idx, "Zeta")
    assert z["status"] == "FOUND" and z["location"] == "GROUP"
    assert check_client(d, idx, "Nonexistent Co")["status"] == "NO_FOLDER"
    assert check_client(d, idx, "Alpha Holding Pte Ltd")["status"] == "FOUND_FUZZY_REVIEW"


def test_fka_and_junk(tmp_path):
    assert name_variants("Blue Monk Pte. Ltd. (f.k.a. Atelier Pte. Ltd.)") == \
        ["Blue Monk Pte. Ltd.", "Atelier Pte. Ltd."]
    tree = {"pte": [F("a", "A")], "a": [F("c1", "Atelier Pte Ltd - FY06")],
            "c1": [D("f0", "BIZFILE.pdf")]}
    d = FakeDrive(tree)
    idx = Index(build_index(d, "pte"))
    assert check_client(d, idx, "Blue Monk Pte. Ltd. (f.k.a. Atelier Pte. Ltd.)")["status"] == "FOUND"
    p = tmp_path / "c.csv"
    p.write_text("Client Name (x),Status,Link\nClient Name,,\nl,,\nAcme Pte Ltd,Done,u\nBeta Pte Ltd,,\n")
    assert read_clients(str(p)) == ["Acme Pte Ltd", "Beta Pte Ltd"]
    assert read_clients(str(p), skip_done=True) == ["Beta Pte Ltd"]


def test_local_drive(tmp_path):
    g = tmp_path / "PTE Company"
    (g / "A" / "Alpha Pte Ltd - FY12").mkdir(parents=True)
    (g / "A" / "Alpha Pte Ltd - FY12" / "BIZFILE.pdf").write_text("x")
    (g / "GROUP" / "Grp" / "Zeta Pte Ltd - FY03").mkdir(parents=True)
    d = LocalDrive()
    idx = Index(build_index(d, str(g)))
    assert check_client(d, idx, "Alpha Pte. Ltd.")["status"] == "FOUND"
    z = check_client(d, idx, "Zeta Pte. Ltd.")
    assert z["status"] == "NO_BIZFILE" and z["location"] == "GROUP"


def test_resolve_local_root(tmp_path):
    pte = tmp_path / "Secretarial Work" / "CLIENTS (Corp Sec)" / "PTE Company"
    pte.mkdir(parents=True)
    for r in (tmp_path, tmp_path / "Secretarial Work", tmp_path / "Secretarial Work" / "CLIENTS (Corp Sec)", pte):
        assert resolve_local_root(str(r)) == str(pte)
