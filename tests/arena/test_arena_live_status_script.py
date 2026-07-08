from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path("tools/skills/civ6-arena-live/scripts/arena-live-status.sh")


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _run_status_script(tmp_path: Path, *, ss_output: str = "") -> subprocess.CompletedProcess[str]:
    fake_home = tmp_path / "home"
    fake_repo = fake_home / "projects" / "civ6-mcp"
    fake_bin = tmp_path / "bin"
    fake_repo.mkdir(parents=True)
    fake_bin.mkdir()

    _write_executable(
        fake_bin / "ssh",
        """#!/usr/bin/env bash
set -euo pipefail
cmd="${@: -1}"
eval "$cmd"
""",
    )
    _write_executable(
        fake_bin / "git",
        """#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "rev-parse --abbrev-ref HEAD") echo main ;;
  "rev-parse HEAD") echo deadbeef ;;
  "status --short") exit 0 ;;
  *) echo "unexpected git $*" >&2; exit 2 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "ps",
        """#!/usr/bin/env bash
exit 0
""",
    )
    _write_executable(
        fake_bin / "ss",
        f"""#!/usr/bin/env bash
cat <<'EOF'
{ss_output}
EOF
""",
    )
    _write_executable(
        fake_bin / "uv",
        """#!/usr/bin/env bash
echo UNSAFE_HOOK_RAN
""",
    )

    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    return subprocess.run(
        [str(SCRIPT)],
        cwd=Path.cwd(),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_arena_live_status_skips_hook_when_firetuner_socket_exists(tmp_path):
    result = _run_status_script(
        tmp_path,
        ss_output="ESTAB 0 0 127.0.0.1:44692 127.0.0.1:4318",
    )

    assert result.returncode == 0, result.stderr
    assert "HOOK_SKIPPED" in result.stdout
    assert "UNSAFE_HOOK_RAN" not in result.stdout
