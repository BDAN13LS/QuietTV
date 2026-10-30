"""Isolated executable fixture standing in for apps2samsung-cli v3.1.0 in bootstrap tests.

It reproduces ONLY the finite contract bootstrap.py relies on (read off the pinned upstream source
dea1eb391d559b3176f40f612550bdae0cebc493): the ``--version`` line, ``cert --json`` and
``cert --prepare --duids <DUID> --json`` output shapes, the 0/1/2/3 exit codes, and the fact that
``cert --prepare`` writes author.p12 under <data-dir>/Certificate/Jelly2Sams - <Level>/.

Every value a test feeds it is synthetic. It is NOT the real binary and talks to no TV. A test
points it at a scenario file with a leading ``--spec <path>`` launcher argument (which the real
runner cannot pass through the environment, so the environment stays contained) or, failing that,
the A2S_FIXTURE_SPEC environment variable.
"""
import json
import os
import sys
import time
from pathlib import Path

PROFILE_DIR_NAME = {"Public": "Jelly2Sams - Public", "Partner": "Jelly2Sams - Partner"}


def main() -> int:
    args = sys.argv[1:]
    spec_path = None
    if args[:1] == ["--spec"] and len(args) >= 2:
        spec_path, args = args[1], args[2:]
    spec_path = spec_path or os.environ.get("A2S_FIXTURE_SPEC")
    spec = json.loads(Path(spec_path).read_text("utf-8")) if spec_path else {}

    if spec.get("dumpEnvTo"):
        Path(spec["dumpEnvTo"]).write_text(json.dumps(dict(os.environ)), "utf-8")

    if spec.get("sleep"):
        time.sleep(float(spec["sleep"]))

    if args[:1] == ["--version"]:
        if "versionStdout" in spec:
            sys.stdout.write(spec["versionStdout"])
            return int(spec.get("versionExit", 0))
        print(spec.get("version", "3.1.0"))
        return int(spec.get("versionExit", 0))

    if args[:1] == ["cert"]:
        is_prepare = "--prepare" in args
        level = "Partner" if "--partner" in args else "Public"
        if is_prepare:
            write = spec.get("writeAuthor", {})
            if level in write:
                data_dir = os.environ.get("APPS2SAMSUNG_DATA_DIR", ".")
                dest = Path(data_dir) / "Certificate" / PROFILE_DIR_NAME[level] / "author.p12"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(write[level].encode("utf-8"))
            if "prepareStdout" in spec:
                sys.stdout.write(spec["prepareStdout"])
                return int(spec.get("prepareExit", 0))
            print(json.dumps(spec.get("prepare", {"ok": True})))
            return int(spec.get("prepareExit", 0))
        if "inspectStdout" in spec:
            sys.stdout.write(spec["inspectStdout"])
            return int(spec.get("inspectExit", 0))
        print(json.dumps(spec.get("inspect", {"ok": True, "profiles": []})))
        return int(spec.get("inspectExit", 0))

    sys.stderr.write("unknown command\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
