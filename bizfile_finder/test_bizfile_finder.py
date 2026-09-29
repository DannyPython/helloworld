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
