"""Credential staging must use Paramiko's writable, exclusive file mode."""

from types import SimpleNamespace

from paramiko.file import BufferedFile

from features.cluster.deployment_api import write_remote_private_file


def test_private_file_is_writable_and_private_before_first_write():
    events = []
    payloads = []

    class RemoteFile(BufferedFile):
        def chmod(self, mode):
            events.append(("chmod", mode))

        def _write(self, data):
            assert events == [("chmod", 0o600)]
            payloads.append(bytes(data))
            return len(data)

    def open_remote(path, mode):
        assert path == "/tmp/credential"
        assert "x" in mode
        handle = RemoteFile()
        handle._set_mode(mode)
        return handle

    write_remote_private_file(SimpleNamespace(open=open_remote), "/tmp/credential", b"test-payload")
    assert payloads == [b"test-payload"]
