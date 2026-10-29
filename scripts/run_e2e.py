"""Run a disposable Docker stack, verify failed startup, and preserve diagnostics."""

from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    """Execute E2E tests and remove only this run's containers, network and volumes.

    No development ports or volumes are used. Logs and JUnit output are retained
    in artifacts/ on success and failure. Requires Docker Compose v2 with --wait.
    """
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    compose = [
        "docker",
        "compose",
        "-p",
        f"leaseflow-e2e-{uuid4().hex[:8]}",
        "-f",
        str(ROOT / "compose.e2e.yml"),
    ]
    try:
        subprocess.run(compose + ["build"], cwd=ROOT, check=True, timeout=600)
        subprocess.run(
            compose + ["up", "-d", "--wait", "--wait-timeout", "120", "db", "mock-bigpms", "app"],
            cwd=ROOT,
            check=True,
            timeout=180,
        )
        subprocess.run(
            compose + ["run", "--rm", "--no-deps", "tests"], cwd=ROOT, check=True, timeout=180
        )

        # A real connection refusal must prevent the API worker from starting.
        failed_start = subprocess.run(
            compose
            + ["run", "--rm", "--no-deps", "-e", "BIGPMS_API_URL=http://mock-bigpms:9999", "app"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = failed_start.stdout + failed_start.stderr
        (artifacts / "startup-failure.log").write_text(output, encoding="utf-8")
        if (
            failed_start.returncode == 0
            or "registration failed after 3 attempts" not in output
            or "Application startup failed" not in output
        ):
            raise RuntimeError(
                "Expected failed registration and nonzero startup exit; see artifacts/startup-failure.log"
            )
        print("E2E startup failure check passed.")
        return 0
    except (subprocess.SubprocessError, OSError, RuntimeError) as error:
        print(f"E2E run failed: {error}", file=sys.stderr)
        return 1
    finally:
        # Keep cleanup independent from log capture so failed diagnostics cannot leak containers.
        try:
            logs = subprocess.run(
                compose + ["logs", "--no-color"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
            (artifacts / "compose.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
        except (subprocess.SubprocessError, OSError) as error:
            print(f"Could not collect Docker logs: {error}", file=sys.stderr)
        try:
            cleanup = subprocess.run(
                compose + ["down", "-v", "--remove-orphans"], cwd=ROOT, timeout=60
            )
            if cleanup.returncode:
                print(f"Cleanup failed for project {compose[3]}", file=sys.stderr)
        except (subprocess.SubprocessError, OSError) as error:
            print(f"Could not clean up project {compose[3]}: {error}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
