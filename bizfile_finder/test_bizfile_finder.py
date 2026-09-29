import csv
from bizfile_finder import *


class FakeDrive:
    """tree: {parent_id: [ {id,name,mimeType,webViewLink} ]}"""
    calls = 0
    def __init__(self, tree): self.tree = tree
    def children(self, pid, folders_only=False):
        self.calls += 1
        for f in self.tree.get(pid, []):
            if not folders_only or f["mimeType"] == FOLDER_MIME:
                yield f


def F(i, n): return dict(id=i, name=n, mimeType=FOLDER_MIME, webViewLink=f"L{i}")
def D(i, n, m=""): return dict(id=i, name=n, mimeType="application/pdf", webViewLink=f"L{i}", modifiedTime=m)


def idx(tree):
    d = FakeDrive(tree)
    return Index(build_index(d, "pte"))


def tree():
    return {
        "pte": [F("ac", "A-C"), F("de", "D-F"), F("g", "GROUPS"), F("arc", "Archive"), D("x", "readme.pdf")],
        "ac": [F("c1", "Alpha Holdings Pte Ltd"), F("c2", "Acme & Sons Pte. Ltd. - FY06"),
               F("c5", "Both Pte Ltd"), F("misc", "Misc")],
        "misc": [F("deep", "Buried Pte Ltd")],                     # client filed 2 levels below a range folder
        "de": [F("c6", "Delta Pte Ltd - FY12"), F("c7", "Delta Pte Ltd - FY03")],
        "g": [F("gg", "Big Group"), F("c4", "Zeta Pte Ltd"), F("c8", "Both Pte Ltd")],
        "gg": [F("c9", "Nested Co Pte Ltd")],
        "arc": [F("c10", "Old Co Pte Ltd")],
        "c1": [D("f0", "BIZFILE 2023.pdf")], "c2": [D("f2", "random.pdf")],
        "c4": [D("f3", "BIZNET_2024.pdf")], "c5": [D("f4", "Bizfile.pdf")],
        "c6": [D("f5", "Bizfile.pdf")], "c8": [D("f6", "Bizfile.pdf")], "c9": [D("f7", "BIZFILE.pdf")],
        "deep": [D("f8", "BIZNET.pdf")], "c10": [D("f9", "BIZFILE.pdf")],
    }


def run(client, t=None):
    i = idx(t or tree())
    return check_client(i, client)


def test_alphabetical_top_level():
    r = run("ALPHA HOLDINGS PTE. LTD.")
    assert r["check_result"] == "FOUND" and r["location"] == ALPHA
    assert r["matched_path"] == "A-C/Alpha Holdings Pte Ltd" and r["found_in"] == "Alphabetical: A-C"


def test_full_dfs_finds_deeply_buried_and_nested_groups_and_other_top_folders():
    r = run("Buried Pte. Ltd.")
    assert r["check_result"] == "FOUND" and r["matched_path"] == "A-C/Misc/Buried Pte Ltd"
    assert r["found_in"] == "Alphabetical: A-C > Misc"
    n = run("Nested Co Pte Ltd")
    assert n["location"] == GROUPS and n["found_in"] == "GROUPS > Big Group"
    o = run("Old Co Pte Ltd")
    assert o["check_result"] == "FOUND" and o["location"] == OTHER and o["found_in"] == "Other: Archive"


def test_groups_direct():
    z = run("Zeta")
    assert z["check_result"] == "FOUND" and z["location"] == GROUPS and z["found_in"] == "GROUPS"


def test_fy_variants_pick_latest_and_note():
    r = run("Delta Pte. Ltd.")
    assert r["matched_folder"].endswith("FY12") and "other folders" in r["note"]


def test_no_bizfile_flagged():
    assert run("Acme and Sons")["check_result"] == "NO_BIZFILE"


def test_alphabetical_beats_groups_when_both_have_bizfile():
    r = run("Both Pte Ltd")
    assert r["location"] == ALPHA and "GROUPS/Both Pte Ltd" in r["note"]


def test_copy_with_bizfile_beats_copy_without():
    t = tree()
    t["c5"] = []                                    # alphabetical copy now has no bizfile; GROUPS copy has
    r = run("Both Pte Ltd", t)
    assert r["check_result"] == "FOUND" and r["location"] == GROUPS


def test_bizfile_in_subfolder_of_client_folder():
    t = {"pte": [F("a", "A-C")], "a": [F("c1", "Acme Pte Ltd")], "c1": [F("s", "Corp docs")],
         "s": [F("s2", "BIZFILE")], "s2": [D("f", "acra.pdf"), D("g", "BIZFILE 2024.pdf")]}
    r = run("Acme Pte Ltd", t)
    assert r["check_result"] == "FOUND" and "sub-folder 'Corp docs/BIZFILE'" in r["note"]
    assert r["bizfile_name"] == "BIZFILE 2024.pdf"


def test_newest_bizfile_used():
    t = {"pte": [F("a", "A-C")], "a": [F("c1", "Acme Pte Ltd")],
         "c1": [D("f1", "BIZFILE old.pdf", "2020-01-01"), D("f2", "BIZFILE new.pdf", "2024-05-01")]}
    r = run("Acme Pte Ltd", t)
    assert r["bizfile_name"] == "BIZFILE new.pdf" and "multiple bizfiles" in r["note"]


def test_no_folder_only_if_absent_everywhere():
    assert run("Nonexistent Co")["check_result"] == "NO_FOLDER"


def test_fuzzy_needs_review():
    assert run("Alpha Holding Pte Ltd")["check_result"] == "FOUND_FUZZY_REVIEW"


def test_cycle_and_max_depth():
    t = {"pte": [F("a", "A-C")], "a": [F("c1", "Acme Pte Ltd")], "c1": [F("a", "loop")]}   # id 'a' repeats
    assert len(build_index(FakeDrive(t), "pte")) == 1
    t2 = {"pte": [F("a", "A-C")], "a": [F("m", "Misc")], "m": [F("d", "Deep Pte Ltd")]}
    assert len(build_index(FakeDrive(t2), "pte", max_depth=2)) == 1


def test_fka_and_junk(tmp_path):
    assert name_variants("Blue Monk Pte. Ltd. (f.k.a. Atelier Pte. Ltd.)") == \
        ["Blue Monk Pte. Ltd.", "Atelier Pte. Ltd."]
    t = {"pte": [F("a", "A-C")], "a": [F("c1", "Atelier Pte Ltd")], "c1": [D("f0", "BIZFILE.pdf")]}
    assert run("Blue Monk Pte. Ltd. (f.k.a. Atelier Pte. Ltd.)", t)["check_result"] == "FOUND"
    p = tmp_path / "c.csv"
    p.write_text("Client Name (x),Status,Link\nClient Name,,\nl,,\nAcme Pte Ltd,Done,u\nBeta Pte Ltd,,\n")
    assert read_clients(str(p)) == ["Acme Pte Ltd", "Beta Pte Ltd"]
    assert read_clients(str(p), skip_done=True) == ["Beta Pte Ltd"]


def test_confusable_numbered_entities_never_fuzzy_match():
    assert confusable("sgsupergreen a", "sgsupergreen b")
    assert confusable("tangerine capital i", "tangerine capital ii")
    assert not confusable("epsilon marine services", "epsilon marine service")
    t = {"pte": [F("a", "S-U")],
         "a": [F("c1", "SGSuperGreen-B Pte. Ltd."), F("c2", "Tangerine Capital II Pte. Ltd.")],
         "c1": [D("f", "BIZFILE.pdf")], "c2": [D("g", "BIZFILE.pdf")]}
    i = idx(t)
    for wrong in ("SGSuperGreen-A Pte. Ltd.", "Tangerine Capital I Pte. Ltd.", "Tangerine Capital III Pte. Ltd."):
        assert check_client(i, wrong)["check_result"] == "NO_FOLDER"
    assert check_client(i, "SGSuperGreen-B Pte. Ltd.")["check_result"] == "FOUND"


def test_leading_the():
    t = {"pte": [F("a", "S-U")], "a": [F("c1", "Blue Boy Agency Pte Ltd")], "c1": [D("f", "BIZFILE.pdf")]}
    assert run("The Blue Boy Agency Pte. Ltd.", t)["check_result"] == "FOUND"


def test_local_drive(tmp_path):
    g = tmp_path / "PTE Company"
    (g / "A-C" / "Misc" / "Alpha Pte Ltd").mkdir(parents=True)
    (g / "A-C" / "Misc" / "Alpha Pte Ltd" / "BIZFILE.pdf").write_text("x")
    (g / "GROUPS" / "Zeta Pte Ltd").mkdir(parents=True)
    i = Index(build_index(LocalDrive(), str(g)))
    a = check_client(i, "Alpha Pte. Ltd.")
    assert a["check_result"] == "FOUND" and a["found_in"] == "Alphabetical: A-C > Misc"
    assert a["bizfile_name"] == "BIZFILE.pdf"
    z = check_client(i, "Zeta Pte. Ltd.")
    assert z["check_result"] == "NO_BIZFILE" and z["location"] == GROUPS


def test_resolve_local_root(tmp_path):
    pte = tmp_path / "Secretarial Work" / "CLIENTS (Corp Sec)" / "PTE Company"
    pte.mkdir(parents=True)
    for r in (tmp_path, tmp_path / "Secretarial Work", tmp_path / "Secretarial Work" / "CLIENTS (Corp Sec)", pte):
        assert resolve_local_root(str(r)) == str(pte)


def test_main_sheet_layout_and_compare(tmp_path):
    root = tmp_path / "PTE Company"
    for path in ("A-C/Alpha Pte Ltd", "A-C/Beta Pte Ltd", "GROUPS/Gamma Pte Ltd"):
        (root / path).mkdir(parents=True)
    (root / "A-C/Alpha Pte Ltd/BIZFILE.pdf").write_text("x")
    (root / "GROUPS/Gamma Pte Ltd/BIZNET.pdf").write_text("x")     # Beta has no bizfile
    src = tmp_path / "in.csv"
    src.write_text("Client Name (must remove non current clients),Status,Connected folder link\n"
                   "Alpha Pte. Ltd.,Done,\nBeta Pte. Ltd.,not done,\nClient Name,,\n"
                   "Gamma Pte. Ltd.,not done,\nMissing Pte. Ltd.,not done,\n")
    out = tmp_path / "out.csv"
    main([str(src), "--local-root", str(root), "-o", str(out), "--compare"])
    rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
    assert list(rows[0])[:3] == ["Client Name (must remove non current clients)", "Status",
                                 "Connected folder link"]
    assert [r["Client Name (must remove non current clients)"] for r in rows] == \
        ["Alpha Pte. Ltd.", "Beta Pte. Ltd.", "Client Name", "Gamma Pte. Ltd.", "Missing Pte. Ltd."]
    assert [r["Status"] for r in rows] == ["Done", "not done", "", "Done", "not done"]
    assert [r["check_result"] for r in rows] == ["FOUND", "NO_BIZFILE", "SKIPPED_NOT_A_CLIENT",
                                                 "FOUND", "NO_FOLDER"]
    assert rows[0]["found_in"] == "Alphabetical: A-C" and rows[3]["found_in"] == "GROUPS"
    assert [r["agrees"] for r in rows] == ["yes", "yes", "", "NO", "yes"]     # Gamma: manual said not done


def _snapshot(entries):
    return sorted((e["path"], tuple(sorted(b[0] for b in e["bizfiles"]))) for e in entries)


def test_dijkstra_dfs_and_parallel_give_identical_index():
    ref = _snapshot(build_index(FakeDrive(tree()), "pte", algo="dfs"))
    assert _snapshot(build_index(FakeDrive(tree()), "pte", algo="dijkstra")) == ref
    assert _snapshot(build_index(FakeDrive(tree()), "pte", algo="dijkstra", workers=8)) == ref
    assert len(ref) > 10


def test_dijkstra_settles_shallowest_first():
    order, depth = [], {"pte": 0}

    class Spy(FakeDrive):
        def children(self, pid, folders_only=False):
            order.append(depth[pid])
            for f in super().children(pid, folders_only):
                depth.setdefault(f["id"], depth[pid] + 1)
                yield f
    build_index(Spy(tree()), "pte", algo="dijkstra")
    assert order == sorted(order) and max(order) >= 3


def test_prune_skips_below_bizfile_folders():
    t = {"pte": [F("a", "A-C")], "a": [F("c1", "Acme Pte Ltd"), F("c2", "Beta Pte Ltd")],
         "c1": [D("f", "BIZFILE.pdf"), F("tax", "Tax")], "tax": [F("y", "2023")],
         "c2": [F("misc", "Misc")], "misc": [D("g", "BIZNET.pdf")]}
    full, pruned = FakeDrive(t), FakeDrive(t)
    assert len(build_index(full, "pte")) == 5                 # c1, tax, 2023, c2, misc
    assert len(build_index(pruned, "pte", prune=True)) == 3   # c1, c2, misc  (tax/2023 skipped)
    assert pruned.calls < full.calls
    r = check_client(Index(build_index(pruned, "pte", prune=True)), "Beta Pte Ltd")
    assert r["check_result"] == "FOUND"                       # bizfile in sub-folder still found
