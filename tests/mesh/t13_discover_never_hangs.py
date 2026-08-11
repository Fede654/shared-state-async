"""T13 — `discover` must complete, every time.

Strict condition
    Repeated `shared-state-async discover` invocations all terminate
    promptly.

Why it matters
    lime-packages#1198: `discover` hangs and prints nothing; the
    external script does get called, so the hang is after that. The
    reporter's workaround is to add a debug printout *anywhere* in
    `IOContext::closeAFD`, which makes it go away in Debug, Coverage and
    Release builds alike. That signature — behaviour changing because a
    printout perturbed frame layout — is what audit A1 predicts:
    `task.hh` has no symmetric transfer, so a coroutine that completes
    synchronously (which the close path always does) can have its frame
    destroyed while its own `resume()` is still on the stack.

    This is the only upstream bug report with a person waiting on it,
    and the only proposed fix is a printout nobody wants to merge.

Method
    Run `discover` many times against a stub that returns immediately,
    with a hard timeout on each. Any single hang is a failure.

Note on toolchains: the reporter saw this with GCC 12.2 targeting
znver3. Undefined behaviour is compiler- and layout-dependent, so a
green run here proves only "not reproducible on this build", not "the
bug is not real". Record the toolchain with the result.
"""

import subprocess
import time

ID = "T13"
TITLE = "discover never hangs (lime-packages#1198)"
EXPECT_TODAY = "GREEN"

TYPE = "probe"
RUNS = 40
PER_RUN_TIMEOUT = 10


def run(mesh):
    node = mesh.node("lime-a")
    node.clean_state()
    node.seed_config()
    node.set_peers([mesh.node("lime-b"), mesh.node("lime-c")])
    node.cli(f"register {TYPE} community 5 300", timeout=30)

    slowest = 0.0
    for i in range(RUNS):
        t0 = time.time()
        try:
            res = node.cli("discover", timeout=PER_RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, (f"discover hung on run {i + 1}/{RUNS} "
                           f"(no output after {PER_RUN_TIMEOUT}s) — "
                           f"lime-packages#1198 reproduced")
        elapsed = time.time() - t0
        slowest = max(slowest, elapsed)
        if res.returncode != 0:
            return False, (f"run {i + 1} exited {res.returncode}: "
                           f"{(res.stderr or '')[:120]}")

    cc = _compiler(mesh)
    return True, (f"{RUNS} runs completed, slowest {slowest:.2f}s — "
                  f"not reproduced on this build ({cc}); the report is "
                  f"against GCC 12.2/znver3")


def _compiler(mesh):
    try:
        out = subprocess.run("gcc --version", shell=True, capture_output=True,
                             text=True, timeout=10).stdout.splitlines()
        return out[0] if out else "unknown"
    except Exception:
        return "unknown"
