"""Worker token / credential SFTP transfer helpers.

Deployment tokens are uploaded as 0600 files and passed to remote
installers BY PATH: the token string must never appear in the remote
command argv, where any same-host user could read it via ps for the
whole install window.
"""

from __future__ import annotations


def write_remote_token_file(sftp, remote_path: str, token: str) -> None:
    """Create remote_path as a 0600 file containing token + newline."""
    with sftp.open(remote_path, "w") as token_file:
        sftp.chmod(remote_path, 0o600)
        token_file.write((token + "\n").encode("utf-8"))


def remove_remote_files_quietly(sftp, *remote_paths: str) -> None:
    """Best-effort cleanup of uploaded staging files on failure."""
    for remote_path in remote_paths:
        try:
            sftp.remove(remote_path)
        except OSError:
            pass
