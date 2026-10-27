"""Both-ways check: disable each guard in a scratch copy, confirm its test fails."""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(sys.argv[1])
MUTATIONS = [
    ("duplicate-key hook", "tvkit/strictjson.py",
     '            raise StrictJsonError(f"duplicate JSON property: {key!r}")\n', "            pass\n",
     "test_public_toolkit_capsule.SchemaRules.test_duplicate_json_property"),
    ("symlink-ancestor check (archive)", "tvkit/capsule.py",
     '                if kinds.get(ancestor) == "symlink":\n                    raise CapsuleError("payload member is below a symbolic link")\n',
     "                pass\n",
     "test_public_toolkit_capsule.HostileArchives.test_symlink_child_escape_in_archive"),
    ("member byte hash", "tvkit/capsule.py",
     "if member_digest != entry.sha256 or member_size != entry.size:", "if False:",
     "test_public_toolkit_capsule.Tampering.test_tampered_member_with_recomputed_outer_hash"),
    ("device id binding", "tvkit/capsule.py",
     '!= capsule.device["deviceIdSha256"]:', '== "never":',
     "test_public_toolkit_capsule.DeviceBinding.test_wrong_device_identifier"),
    ("link escape check (lexical + resolved layers)", "tvkit/capsule.py",
     "            if _link_escapes(path, entry.link_target, install_root):\n                raise CapsuleError(f\"{where} is a symbolic link that escapes the package root\")\n        entries[path] = entry\n",
     "            if False:\n                pass\n        entries[path] = entry\n",
     "test_public_toolkit_capsule.SchemaRules.test_symlink_target_escapes"),
    ("protected ids", "tvkit/profile.py",
     "return any(token in lowered for token in self.protected_substrings)", "return False",
     "test_public_toolkit_capsule.SchemaRules.test_protected_package_and_app_ids"),
    ("registry guard assertions", "tvkit/plan.py",
     "    for change in changes:\n        expect =", "    for change in []:\n        expect =",
     "test_public_toolkit_plan.RegistrySql.test_restore_refused_when_state_differs_and_nothing_changes"),
    ("adapter-absent refusal", "tvkit/transport.py",
     '    if not name:\n        raise TransportUnavailable("no transport adapter named; this toolkit ships none")\n',
     "    return _Pretend()\n",
     "test_public_toolkit_plan.ApplyCli.test_apply_without_adapter_fails_closed"),
    ("deny-value scan", "tvkit/export.py",
     '                hits.append(f"deny-value-#{item.index}")\n', "                pass\n",
     "test_public_toolkit_export.PlantedSecrets.test_deny_values_refused_without_printing"),
    ("mac pattern", "tvkit/export.py",
     '    ("mac-address", re.compile(', '    ("mac-address-off", re.compile(',
     "test_public_toolkit_export.PlantedSecrets.test_builtin_patterns"),
    ("https-only", "tvkit/fetch.py",
     '    if parts.scheme != "https":\n', '    if parts.scheme == "never":\n',
     "test_public_toolkit_fetch.FetchCase.test_http_and_other_schemes_refused"),
    ("credential redaction", "tvkit/fetch.py",
     '        return f"{parts.scheme}://{host}{port}{parts.path}"', "        return url",
     "test_public_toolkit_fetch.FetchCase.test_https_success_with_redacted_source"),
    ("url control-char reject", "tvkit/fetch.py",
     "    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in source):", "    if False:",
     "test_public_toolkit_fetch.FetchCase.test_url_with_space_never_leaks_through_cli"),
    ("broad opener wrap", "tvkit/fetch.py",
     "    except Exception as exc:  # noqa: BLE001 - any message may echo the URL",
     "    except (urllib.error.URLError, OSError) as exc:",
     "test_public_toolkit_fetch.FetchCase.test_unexpected_library_errors_are_wrapped"),
    ("resolved link check", "tvkit/capsule.py",
     "        if _resolved_link_escapes(path, kinds, links, install_root):", "        if False:",
     "test_public_toolkit_capsule.SchemaRules.test_symlink_escape_through_other_links"),
    ("install pin", "tvkit/plan.py",
     "        if not is_sha256(install.get(\"sha256\")):", "        if False:",
     "test_public_toolkit_plan.RestoreOrder.test_install_needs_pin_and_valid_id"),
    ("device facts / config export rule", "tvkit/export.py",
     "        if json_hit:", "        if False:",
     "test_public_toolkit_export.ForbiddenFiles.test_device_facts_and_filled_config_refused"),
    ("PNG chunk CRC", "tvkit/pngmeta.py",
     "        if zlib.crc32(ctype + body) & 0xFFFFFFFF != crc:", "        if False:",
     "test_public_toolkit_frontend.PngFilter.test_damaged_files_refused"),
    ("PNG metadata chunk drop", "tvkit/pngmeta.py",
     'DROPPED_TYPES = frozenset({b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"caBX"})', "DROPPED_TYPES = frozenset()",
     "test_public_toolkit_frontend.PngFilter.test_metadata_and_email_removed_image_bytes_identical"),
    ("every exported PNG filtered", "tvkit/export.py",
     '        if not names and relpath.lower().endswith(".png"):', "        if False:",
     "test_public_toolkit_frontend.OptionalIncludes.test_present_entries_scanned_and_shipped"),
    ("unfiltered image formats", "tvkit/export.py",
     '            return f"image-format-not-filtered:{suffix}"', "            pass",
     "test_public_toolkit_export.PlantedSecrets.test_unfiltered_image_formats_refused"),
    ("transform anchor exactly once", "tvkit/frontend_export.py",
     "    if found != 1:", "    if found < 1:",
     "test_public_toolkit_frontend.WeatherTransform.test_anchor_must_match_exactly_once"),
    ("leftover brand image refs", "tvkit/frontend_export.py",
     "    if _LEFTOVER_BRAND.search(text):", "    if False:",
     "test_public_toolkit_frontend.WeatherTransform.test_brand_png_imports_dropped"),
    ("README public marker", "tvkit/export.py",
     '        if relpath in markers and markers[relpath][0].encode("utf-8") not in raw:', "        if False:",
     "test_public_toolkit_frontend.OptionalIncludes.test_readme_needs_public_marker"),
    ("absent optional entries listed", "tvkit/export.py",
     "            report.absent_optional.append(entry)\n", "            pass\n",
     "test_public_toolkit_frontend.OptionalIncludes.test_absent_entries_listed"),
    ("frontend file accounted for", "tvkit/export.py",
     '                    report.violations.append(Violation(full, "frontend-file-not-accounted"))',
     "                    pass",
     "test_public_toolkit_frontend.FrontendExport.test_unaccounted_file_refused"),
    ("frontend references resolve", "tvkit/export.py",
     '            report.violations.append(Violation(full, f"unresolved-reference:{shown}"))', "            pass",
     "test_public_toolkit_frontend.FrontendExport.test_broken_reference_refused"),
    ("secret reference withheld", "tvkit/export.py",
     '            shown = ref if not scan_bytes(ref.encode("utf-8"), deny) else "<withheld>"', "            shown = ref",
     "test_public_toolkit_frontend.FrontendExport.test_reference_naming_a_secret_is_withheld"),
    ("write-time re-check (source + output)", "tvkit/export.py",
     "        if _sha(raw) != source_sha:", "        if False:",
     "test_public_toolkit_frontend.FrontendExport.test_source_changed_after_scan_refused"),
]
PRETEND = '''

class _Pretend(Transport):
    name = "pretend"
    implemented = True
    capabilities = frozenset({
        "read-device-facts", "inspect-path", "stage-file", "extract-tar", "chown-entries",
        "chmod-entries", "xattr-restore", "verify-tree", "move-into-place", "run-guarded-sql",
        "system-key", "manual-acceptance", "remove-package", "run-guarded-sql-on-copy",
        "system-key-read", "install-package", "remove-scratch"})

    def read_device_facts(self):
        import test_public_toolkit_fixtures as fx
        return dict(fx.FACTS)

    def run(self, step):
        return StepResult(True, True)
'''

EXTRA = {
    "link escape check (lexical + resolved layers)": [
        ("        if _resolved_link_escapes(path, kinds, links, install_root):", "        if False:"),
    ],
    "write-time re-check (source + output)": [
        ("        if len(data) != size or _sha(data) != digest or scan_bytes(data, deny) or _json_rule(relpath, data):",
         "        if False:"),
    ],
}

failures = 0
for label, rel, old, new, test in MUTATIONS:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shutil.copytree(REPO / "public-toolkit", root / "public-toolkit",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(REPO / "tests", root / "tests", ignore=shutil.ignore_patterns("__pycache__", "proxy"))
        target = root / "public-toolkit" / rel
        original = target.read_text(encoding="utf-8")
        text = original
        pairs = [(old, new)] + EXTRA.get(label, [])
        bad = [o for o, _ in pairs if text.count(o) != 1]
        if bad:
            print(f"BAD MUTATION {label}: an anchor is not unique")
            failures += 1
            continue
        for o, n in pairs:
            text = text.replace(o, n)
        if label == "adapter-absent refusal":
            text += PRETEND
        target.write_text(text, encoding="utf-8")
        base = [sys.executable, "-m", "unittest", test]
        mutated = subprocess.run(base, cwd=root / "tests", capture_output=True, text=True, timeout=300)
        target.write_text(original, encoding="utf-8")
        restored = subprocess.run(base, cwd=root / "tests", capture_output=True, text=True, timeout=300)
        ok = mutated.returncode != 0 and restored.returncode == 0
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}: guard off -> exit {mutated.returncode}; restored -> exit {restored.returncode}")
sys.exit(1 if failures else 0)
