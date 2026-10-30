"""Process-tree fixture for the bootstrap runner's overflow/timeout tests.

It (1) spawns a long-lived GRANDCHILD that keeps appending to a heartbeat file, (2) floods stdout so
the runner's byte cap trips, swallowing the broken-pipe error so it does NOT voluntarily exit, then
(3) after a delay writes a completion MARKER file. Crucially it does NOT clean up the grandchild
itself, so the ONLY thing that can stop the producer before the marker and reap the orphaned
grandchild is the runner's own process-tree kill. The grandchild self-exits after a few seconds as a
safety net against a leaked process. Self-contained and synthetic: stdlib only, no real state.
"""
import subprocess
import sys
import time
from pathlib import Path

# Appends to the heartbeat for a bounded time, then exits on its own as a leak safety net.
GRANDCHILD = (
    "import sys,time\n"
    "p=sys.argv[1]\n"
    "end=time.time()+6.0\n"
    "while time.time()<end:\n"
    "    try:\n"
    "        open(p,'a').write('.')\n"
    "    except OSError:\n"
    "        break\n"
    "    time.sleep(0.03)\n"
)


def main() -> int:
    heartbeat, marker, nbytes = sys.argv[1], sys.argv[2], int(sys.argv[3])
    # Spawned normally so it shares the runner-created session/process group and tree: the runner's
    # kill must be what reaps it. This process never kills it (no finally), so a surviving heartbeat
    # proves the runner failed to clean the tree.
    subprocess.Popen([sys.executable, "-I", "-c", GRANDCHILD, heartbeat])
    deadline = time.time() + 10.0  # wait until the grandchild is demonstrably alive and writing
    while not (Path(heartbeat).exists() and Path(heartbeat).stat().st_size > 0):
        if time.time() > deadline:
            break
        time.sleep(0.01)
    blob = b"x" * 65536
    written = 0
    while written < nbytes:
        try:
            sys.stdout.buffer.write(blob)
            sys.stdout.buffer.flush()
        except OSError:
            pass  # reader closed the pipe on overflow; keep running so only the runner can stop us
        written += len(blob)
    time.sleep(2.0)            # the runner should kill this process before this elapses
    Path(marker).write_text("COMPLETE", "utf-8")
    try:
        sys.stdout.write("COMPLETE")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
