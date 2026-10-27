"""Public source guard and attestation checker, including negative controls on planted material."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load("public_source_guard")
attest = _load("check_attestation")


def test_this_public_tree_is_clean():
    assert guard.scan(ROOT) == []


# Planted values are assembled at run time so this file itself stays clean under the guard.
J = "".join


@pytest.mark.parametrize("planted,rule", [
    (J(["mac = 'A4:B1", ":C2:D3:E4:F5'"]), "MAC address"),
    (J(["tv = '192.", "168.1.44'"]), "private LAN IP"),
    (J(["tv = '172.", "20.0.9'"]), "private LAN IP"),
    (J(["X-QuietTV-", "Key: 9f8e7d6c5b4a39281706"]), "control key header"),
    (J(["see private/", "signing/maintenance-token.txt"]), "private repository path"),
    (J(["path = 'C:\\\\", "Users\\\\jdoe\\\\x'"]), "personal user path"),
    (J(["path = '/ho", "me/jdoe/x'"]), "personal user path"),
    (J(["-----BEGIN RSA ", "PRIVATE KEY-----"]), "private key block"),
    ("ghp_" + "a" * 36, "GitHub token"),
    (J(["    runs-on: ", "self-hosted"]), "self-hosted runner"),
    (J(["on:\n  pull_request_", "target:\n"]), "pull_request_target"),
])
def test_guard_trips_on_planted_material(tmp_path, planted, rule):
    (tmp_path / "leak.py").write_text(planted, encoding="utf-8")
    problems = guard.scan(tmp_path)
    assert problems and rule in problems[0]


@pytest.mark.parametrize("name", ["capture.png", "control.key", ".env", "credentials.json", "pkg.whl"])
def test_guard_blocks_private_file_types(tmp_path, name):
    (tmp_path / name).write_bytes(b"x")
    assert guard.scan(tmp_path)


def test_guard_binary_allowlist_binds_exact_bytes(tmp_path):
    art = tmp_path / "artwork" / "logo.png"
    art.parent.mkdir()
    art.write_bytes(b"\x89PNG reviewed asset")
    allow = {"artwork/logo.png": hashlib.sha256(art.read_bytes()).hexdigest()}
    assert guard.scan(tmp_path) and guard.scan(tmp_path, allow_binaries=allow) == []
    art.write_bytes(b"\x89PNG swapped for a private screenshot")
    assert "differs from the reviewed bytes" in guard.scan(tmp_path, allow_binaries=allow)[0]
    (tmp_path / "artwork" / "new.png").write_bytes(b"x")
    assert any("new.png" in p for p in guard.scan(tmp_path, allow_binaries=allow))
    listing = tmp_path / "allow.txt"
    listing.write_text("# reviewed\nartwork/logo.png " + "0" * 64 + "\nbad line\n", encoding="utf-8")
    assert guard.load_binary_allowlist(listing) == {"artwork/logo.png": "0" * 64}


def _planted_fixture(tmp_path):
    fixture = tmp_path / "public-toolkit" / "tests" / "fixtures" / "fake_capsule.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(J(['{"tv": "192.', '168.4.4", "mac": "A4:B1', ':C2:D3:E4:F5", "key": "-----BEGIN ',
                          'PRIVATE KEY-----"}']), encoding="utf-8")
    return fixture


def test_guard_fixture_approval_is_hash_bound_and_keeps_denylist(tmp_path):
    fixture = _planted_fixture(tmp_path)
    rel = "public-toolkit/tests/fixtures/fake_capsule.json"
    approved = {rel: hashlib.sha256(fixture.read_bytes()).hexdigest()}
    assert len(guard.scan(tmp_path)) == 3                       # negative controls are seen without approval
    assert guard.scan(tmp_path, fixtures=approved) == []        # exact reviewed bytes pass
    deny = {hashlib.sha256(J(["192.", "168.4.4"]).encode()).hexdigest()}
    assert "denylisted owner value" in guard.scan(tmp_path, deny, fixtures=approved)[0]
    fixture.write_text(fixture.read_text(encoding="utf-8") + " ", encoding="utf-8")
    problems = guard.scan(tmp_path, fixtures=approved)
    assert any("changed since its fixture approval" in p for p in problems) and len(problems) == 4


def test_guard_fixture_approval_rejects_non_test_paths(tmp_path):
    listing = tmp_path / "approvals.txt"
    for line in ("src/quiettv_mcp/device.py " + "0" * 64 + " because I said so",
                 "tests/../src/x.py " + "0" * 64 + " sneaky traversal attempt",
                 "tests/fake.json " + "0" * 64,
                 "tests/fake.json " + "0" * 64 + " short"):
        listing.write_text(line + "\n", encoding="utf-8")
        with pytest.raises(guard.ApprovalError):
            guard.load_fixture_approvals(listing)
    listing.write_text("# reviewed\ntests/fixtures/fake.json " + "1" * 64 + " planted fake LAN values for refusal tests\n",
                       encoding="utf-8")
    assert guard.load_fixture_approvals(listing) == {"tests/fixtures/fake.json": "1" * 64}


def test_guard_allows_example_values(tmp_path):
    (tmp_path / "ok.md").write_text("ip 10.20.30.40, mac 02:00:00:00:00:01, header X-QuietTV-Key: <from file>",
                                    encoding="utf-8")
    assert guard.scan(tmp_path) == []


def test_guard_hashed_denylist(tmp_path):
    secret_value = "ZZ-OWNER-SERIAL-77"
    (tmp_path / "a.txt").write_text(f"serial {secret_value}", encoding="utf-8")
    deny = {hashlib.sha256(secret_value.encode()).hexdigest()}
    assert guard.scan(tmp_path) == []
    assert "denylisted owner value" in guard.scan(tmp_path, deny)[0]


def test_ci_template_is_read_only_and_pinned():
    import re
    text = (ROOT / "community" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert re.search(r"^permissions:\n  contents: read\n", body, re.M)
    assert "pull_request_target" not in body and "secrets." not in body and "self-hosted" not in body
    assert re.findall(r"runs-on: (\S+)", body) == ["ubuntu-24.04", "ubuntu-24.04"]
    uses = re.findall(r"uses: (\S+)", body)
    assert uses and all(re.fullmatch(r"actions/(checkout|setup-python|setup-node|setup-dotnet)@[0-9a-f]{40}", u) for u in uses)
    assert body.count("persist-credentials: false") == body.count("actions/checkout@")
    names = re.findall(r"^    name: (.+)$", body, re.M)
    assert sorted(names) == ["Offline checks", "Public source guard"]


def test_community_templates_present():
    for rel in ("CONTRIBUTING.md", "CODEOWNERS", "PULL_REQUEST_TEMPLATE.md", "ISSUE_TEMPLATE/bug_report.yml",
                "ISSUE_TEMPLATE/feature_request.yml"):
        assert (ROOT / "community" / rel).is_file(), rel
    assert "@BDAN13LS" in (ROOT / "community" / "CODEOWNERS").read_text(encoding="utf-8")
    pr = (ROOT / "community" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    for needed in ("Exact tested commit SHA", "TV model", "firmware", "Restore evidence", "Offline test results"):
        assert needed in pr


def _attestation(sha: str) -> dict:
    return {
        "schema": "quiettv-hardware-attestation/1", "commit_sha": sha, "profile": "UN43U8000FBXZA-1300.8",
        "tested_by": "maintainer", "tested_at": "2026-10-26", "offline_checks": "pass",
        "device_checks": [{"name": "tv_status + tv_capture", "result": "pass", "evidence": "capture id cap-000001"}],
        "restore_evidence": "restore plan verified; rollback capsule sha256 recorded privately",
        "video_and_sound": "not-affected",
    }


def test_attestation_bound_to_exact_sha():
    sha = "a" * 40
    assert attest.check(_attestation(sha), sha) == []
    assert any("does not match" in e for e in attest.check(_attestation(sha), "b" * 40))
    assert attest.check(_attestation("abc123"), "abc123")


def test_attestation_rejects_incomplete_records():
    sha = "c" * 40
    for field, value in [("device_checks", []), ("restore_evidence", ""), ("profile", "generic-tv"),
                         ("video_and_sound", "assumed-from-capture"), ("offline_checks", "skipped")]:
        doc = _attestation(sha)
        doc[field] = value
        assert attest.check(doc, sha), field
