"""Docker-based sandbox for executing tool calls in an isolated container.

The mount path is exposed at its exact host path inside the container, so
file paths are identical on both sides — no translation needed.
"""

import subprocess
import uuid
from pathlib import Path


class DockerSandbox:
    """Long-lived Docker container with --network=none.

    Usage:
        with DockerSandbox(mount_path, image) as sb:
            output = sb.exec("lake build", cwd="/path/to/project")
    """

    def __init__(self, mount_path: Path, image: str, use_sudo: bool = False):
        self.mount_path = mount_path.resolve()
        self.image = image
        self.use_sudo = use_sudo
        self.container_id: str | None = None

    def _docker(self, *args) -> list[str]:
        prefix = ["sudo"] if self.use_sudo else []
        return prefix + ["docker"] + list(args)

    def start(self) -> "DockerSandbox":
        mp = str(self.mount_path)
        result = subprocess.run(
            self._docker("run",
                "--detach",
                "--rm",           # auto-remove on stop
                "--network=none",
                "-v", f"{mp}:{mp}",
                self.image,
                "sleep", "infinity",
            ),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "permission denied" in stderr.lower():
                raise RuntimeError(
                    "Docker permission denied. Re-run with --sudo to have lea invoke "
                    "docker commands via sudo, or add your user to the docker group:\n"
                    "    sudo usermod -aG docker $USER   (then log out and back in)\n"
                    f"Docker said: {stderr}"
                )
            raise RuntimeError(f"Failed to start Docker container: {stderr}")
        self.container_id = result.stdout.strip()
        return self

    def stop(self):
        if self.container_id:
            subprocess.run(self._docker("stop", self.container_id), capture_output=True)
            self.container_id = None

    def exec(self, command: str, cwd: str | None = None, timeout: int = 120, stdin: bytes | None = None) -> str:
        """Run a shell command inside the container and return combined stdout+stderr."""
        if not self.container_id:
            raise RuntimeError("Sandbox not started. Call start() first.")

        exec_cmd = self._docker("exec")
        if stdin is not None:
            exec_cmd += ["-i"]
        if cwd:
            exec_cmd += ["-w", cwd]
        exec_cmd += [self.container_id, "/bin/bash", "-c", command]

        kwargs: dict = {"capture_output": True, "timeout": timeout}
        if stdin is not None:
            kwargs["input"] = stdin
        else:
            kwargs["stdin"] = subprocess.DEVNULL

        try:
            result = subprocess.run(exec_cmd, **kwargs)
            output = (result.stdout + result.stderr).decode("utf-8", errors="replace").strip()
            if not output:
                return f"(no output, exit code {result.returncode})"
            if len(output) > 10000:
                output = output[:10000] + "\n... (truncated)"
            return output
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout}s."

    def read_file(self, path: str) -> bytes | None:
        """Read a file from inside the container. Returns None if it doesn't exist or isn't accessible."""
        result = subprocess.run(
            self._docker("exec", self.container_id, "cat", path),
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
        return result.stdout if result.returncode == 0 else None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()
