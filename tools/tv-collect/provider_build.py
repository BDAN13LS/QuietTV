"""Build the reviewed provider payloads from the checked source tree.

The toolkit never ships opaque prebuilt binaries. This owner-run helper builds
the read-only inspector and the two upstream payload projects from the exact
sources in this repository, into a NEW owner-private directory OUTSIDE every
repository, and records every artifact's absolute path and SHA-256 in a
``quiettv-provider-artifacts/1`` manifest for the provider configuration.

CLI:
    python provider_build.py --dotnet <abs dotnet> --out-dir <new private dir>
    python provider_build.py --dotnet <abs dotnet> --out-dir <dir> --dry-run
    python provider_build.py --check <manifest.json> [--provider-config <config.json>]

No TV, network device, SDK device command, signing or credential is used.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "helper") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "helper"))
if str(REPO_ROOT / "public-toolkit") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "public-toolkit"))
import owner_init as oi  # noqa: E402
from tvkit import provider  # noqa: E402
from tvkit.provider import proc  # noqa: E402

MANIFEST_SCHEMA = "quiettv-provider-artifacts/1"
MANIFEST_NAME = "provider-artifacts.json"
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
BUILD_SECONDS = 900

INSPECTOR_PROJECT = REPO_ROOT / "tools" / "tv-inspect" / "QuietTVInspect.csproj"
ARCHIVE_PROJECT = (REPO_ROOT / "public-toolkit" / "vendor" / "root-payload-source"
                   / "SamsungTvArchiveRoot.csproj")
AGENT_PROJECT = (REPO_ROOT / "public-toolkit" / "vendor" / "root-payload-source"
                 / "SamsungTvRootAgent.csproj")

INSPECTOR_NAMES = ("QuietTVInspect.dll", "QuietTVInspect.runtimeconfig.json",
                   "QuietTVInspect.deps.json")
ROOT_NAMES = ("SamsungTvArchiveRoot.dll", "SamsungTvRootAgent.dll")
ARTIFACT_NAMES = INSPECTOR_NAMES + ROOT_NAMES


class BuildError(RuntimeError):
    def __init__(self, message: str):
        super().__init__(message)


def _relative(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def _plan(out_dir: str) -> dict:
    return {
        "schema": MANIFEST_SCHEMA,
        "sources": {
            "inspector": _relative(INSPECTOR_PROJECT),
            "archiveRoot": _relative(ARCHIVE_PROJECT),
            "rootAgent": _relative(AGENT_PROJECT),
        },
        "outputs": {
            "inspector": str(Path(out_dir) / "inspector"),
            "root": str(Path(out_dir) / "root"),
            "manifest": str(Path(out_dir) / MANIFEST_NAME),
        },
        "commands": [
            ["dotnet", "build", _relative(INSPECTOR_PROJECT), "-c", "Release",
             "--nologo", "-o", str(Path(out_dir) / "inspector")],
            ["dotnet", "build", _relative(ARCHIVE_PROJECT), "-c", "Release",
             "--nologo", "-o", str(Path(out_dir) / "root")],
            ["dotnet", "build", _relative(AGENT_PROJECT), "-c", "Release",
             "--nologo", "-o", str(Path(out_dir) / "root")],
        ],
        "artifacts": list(ARTIFACT_NAMES),
    }


def _dotnet(value: str) -> Path:
    if type(value) is not str or not value or not os.path.isabs(value):
        raise BuildError("--dotnet must be an explicit absolute executable path")
    path = Path(os.path.abspath(value))
    try:
        info = os.lstat(path)
    except OSError:
        raise BuildError("--dotnet is not an accessible executable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BuildError("--dotnet must be a plain regular executable file")
    if path.name.lower() not in ("dotnet", "dotnet.exe"):
        raise BuildError("--dotnet must be the dotnet executable")
    return path


def _build_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_NOLOGO": "1",
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
    })
    return env


def _run_build(dotnet: Path, project: Path, output: Path) -> None:
    try:
        result = proc.run(
            (str(dotnet), "build", str(project), "-c", "Release", "--nologo",
             "-o", str(output)),
            timeout=BUILD_SECONDS, max_stdout=8 * 1024 * 1024,
            max_stderr=1024 * 1024, env=_build_env(), cwd=str(REPO_ROOT))
    except proc.ProcessError as error:
        raise BuildError(str(error)) from None
    if result.returncode != 0:
        raise BuildError("a provider source project did not build; refusing")


def _hash(path: Path) -> tuple[str, int]:
    try:
        info = os.lstat(path)
    except OSError:
        raise BuildError("a built artifact is missing") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BuildError("a built artifact is not a plain regular file")
    if info.st_size > MAX_ARTIFACT_BYTES:
        raise BuildError("a built artifact is larger than the accepted bound")
    try:
        data = path.read_bytes()
    except OSError:
        raise BuildError("a built artifact could not be read") from None
    if len(data) > MAX_ARTIFACT_BYTES:
        raise BuildError("a built artifact is larger than the accepted bound")
    return hashlib.sha256(data).hexdigest(), len(data)


def _version(dotnet: Path) -> str:
    try:
        result = proc.run((str(dotnet), "--version"), timeout=30,
                          max_stdout=4096, max_stderr=4096, env=_build_env(),
                          cwd=str(REPO_ROOT))
    except proc.ProcessError:
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    text = result.stdout.decode("utf-8", "replace").strip()
    if not text or len(text) > 64:
        return "unknown"
    return text


def build(dotnet_value: str, out_dir: str) -> dict:
    dotnet = _dotnet(dotnet_value)
    target = oi.check_outside_repos(out_dir, must_not_exist=True)
    oi.make_private_dir(target)
    for name in ("inspector", "root"):
        oi.make_private_dir(target / name)
    try:
        _run_build(dotnet, INSPECTOR_PROJECT, target / "inspector")
        _run_build(dotnet, ARCHIVE_PROJECT, target / "root")
        _run_build(dotnet, AGENT_PROJECT, target / "root")
        artifacts = {}
        for name in INSPECTOR_NAMES:
            digest, size = _hash(target / "inspector" / name)
            artifacts[name] = {"path": str(target / "inspector" / name),
                               "sha256": digest, "bytes": size}
        for name in ROOT_NAMES:
            digest, size = _hash(target / "root" / name)
            artifacts[name] = {"path": str(target / "root" / name),
                               "sha256": digest, "bytes": size}
        dotnet_sha, _ = _hash(dotnet)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "builtBy": {"dotnet": str(dotnet), "sha256": dotnet_sha,
                        "version": _version(dotnet)},
            "sources": _plan(str(target))["sources"],
            "artifacts": artifacts,
            "note": ("Built locally from the checked source tree. No prebuilt binary "
                     "is distributed by the toolkit."),
        }
        encoded = oi.json_bytes(manifest)
        oi._write_new(target / MANIFEST_NAME, encoded)
        oi.make_private_file(target / MANIFEST_NAME)
        return manifest
    except BaseException:
        # Keep the private build directory for the owner to inspect; never touch
        # any other path. The owner may remove it manually.
        raise


def _manifest_path(value: str) -> Path:
    if type(value) is not str or not value or not os.path.isabs(value):
        raise BuildError("--check needs an explicit absolute manifest path")
    path = Path(os.path.abspath(value))
    oi.check_outside_repos(path, must_not_exist=False)
    try:
        info = os.lstat(path)
    except OSError:
        raise BuildError("the manifest is not accessible") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BuildError("the manifest must be a plain regular file")
    return path


def _load_manifest(path: Path) -> dict:
    try:
        if path.stat().st_size > 1024 * 1024:
            raise BuildError("the manifest is too large")
        raw = path.read_bytes()
        document = json.loads(raw.decode("ascii"))
    except BuildError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise BuildError("the manifest is not readable strict JSON") from None
    if not isinstance(document, dict) or document.get("schema") != MANIFEST_SCHEMA:
        raise BuildError(f"the manifest schema must be {MANIFEST_SCHEMA}")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(ARTIFACT_NAMES):
        raise BuildError("the manifest does not list exactly the fixed artifacts")
    return document


def check(manifest_value: str, config_value: str | None) -> dict:
    path = _manifest_path(manifest_value)
    manifest = _load_manifest(path)
    checked = {}
    for name in ARTIFACT_NAMES:
        item = manifest["artifacts"][name]
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"}:
            raise BuildError("a manifest artifact entry is malformed")
        artifact = item["path"]
        digest = item["sha256"]
        if type(artifact) is not str or not os.path.isabs(artifact):
            raise BuildError("a manifest artifact path is malformed")
        if (type(digest) is not str or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)):
            raise BuildError("a manifest artifact hash is malformed")
        actual, size = _hash(Path(os.path.abspath(artifact)))
        if actual != digest or size != item["bytes"]:
            raise BuildError("a built artifact does not match the manifest; refusing")
        checked[name] = {"sha256": actual, "bytes": size}
    result = {"schema": MANIFEST_SCHEMA, "ok": True, "artifacts": checked}
    if config_value is not None:
        try:
            config = provider.load_config(config_value)
        except provider.ProviderConfigError:
            raise
        for name, config_digest in (
                ("SamsungTvArchiveRoot.dll", config.archive_root_sha256),
                ("SamsungTvRootAgent.dll", config.root_agent_sha256)):
            if manifest["artifacts"][name]["sha256"] != config_digest:
                raise BuildError(
                    "the provider configuration does not pin this build's payload; refusing")
        result["providerConfigPayloadsMatch"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="provider_build.py",
        description="Build the reviewed provider payloads from source into a private "
                    "directory outside every repository, or verify a prior build.")
    ap.add_argument("--dotnet", help="explicit absolute dotnet executable")
    ap.add_argument("--out-dir", help="NEW owner-private output directory outside every repository")
    ap.add_argument("--dry-run", action="store_true", help="print the build plan; write nothing")
    ap.add_argument("--check", metavar="MANIFEST", help="verify an existing manifest and artifacts")
    ap.add_argument("--provider-config", help="optional owner config to compare root payload pins")
    args = ap.parse_args(argv)

    if args.check is not None:
        if args.out_dir is not None or args.dotnet is not None or args.dry_run:
            print("provider build refused: --check cannot be combined with build options",
                  file=sys.stderr)
            return 2
        try:
            result = check(args.check, args.provider_config)
        except provider.ProviderConfigError as exc:
            print("provider build refused: " + str(exc), file=sys.stderr)
            return 2
        except BuildError as exc:
            print("provider build refused: " + str(exc), file=sys.stderr)
            return 2
        except Exception:
            print("provider build refused: the artifacts could not be verified; refusing",
                  file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2))
        return 0

    if args.provider_config is not None:
        print("provider build refused: --provider-config is only used with --check",
              file=sys.stderr)
        return 2
    if args.dotnet is None or args.out_dir is None:
        print("provider build refused: --dotnet and --out-dir are required", file=sys.stderr)
        return 2
    if args.dry_run:
        try:
            dotnet = _dotnet(args.dotnet)
            oi.check_outside_repos(args.out_dir, must_not_exist=True)
        except (BuildError, oi.OwnerInitError) as exc:
            print("provider build refused: " + str(exc), file=sys.stderr)
            return 2
        plan = _plan(str(Path(os.path.abspath(args.out_dir))))
        plan["commands"] = [[str(dotnet) if index == 0 else value
                             for index, value in enumerate(command)]
                            for command in plan["commands"]]
        print(json.dumps(plan, indent=2))
        return 0
    try:
        manifest = build(args.dotnet, args.out_dir)
    except (BuildError, oi.OwnerInitError) as exc:
        print("provider build refused: " + str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("provider build refused: the local build did not complete; refusing",
              file=sys.stderr)
        return 2
    print(json.dumps({"schema": manifest["schema"], "ok": True,
                      "manifest": str(Path(os.path.abspath(args.out_dir)) / MANIFEST_NAME)},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
