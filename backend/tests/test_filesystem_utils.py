from __future__ import annotations

from pathlib import Path

import filesystem_utils
from filesystem_utils import detect_filesystem_type, warn_if_unreliable_file_lock_path


def test_detect_filesystem_type_prefers_longest_proc_mount_match() -> None:
    proc_mounts = "\n".join(
        [
            "server:/export /mnt/review nfs4 rw 0 0",
            "/dev/nvme0n1p2 /mnt ext4 rw 0 0",
            "/dev/nvme0n1p1 / ext4 rw 0 0",
        ]
    )

    assert (
        detect_filesystem_type(
            Path("/mnt/review/work/runtime.db"),
            proc_mounts_text=proc_mounts,
        )
        == "nfs4"
    )


def test_detect_filesystem_type_parses_mount_command_output() -> None:
    mount_output = "\n".join(
        [
            "/dev/disk3s1s1 on / (apfs, local, journaled)",
            "//server/share on /Volumes/review (smbfs, nodev, nosuid, mounted by demo)",
        ]
    )

    assert (
        detect_filesystem_type(
            Path("/Volumes/review/work/runtime.db"),
            mount_output=mount_output,
        )
        == "smbfs"
    )


def test_warn_if_unreliable_file_lock_path_warns_once(
    caplog,
    monkeypatch,
) -> None:
    caplog.set_level("WARNING")
    monkeypatch.setattr(
        filesystem_utils,
        "is_probably_network_filesystem",
        lambda path: (True, "nfs4"),
    )

    first = warn_if_unreliable_file_lock_path(
        Path("/mnt/review/work/runtime.db"),
        label="demo lock path",
    )
    second = warn_if_unreliable_file_lock_path(
        Path("/mnt/review/work/runtime.db"),
        label="demo lock path",
    )

    assert first == (True, "nfs4")
    assert second == (True, "nfs4")
    assert len(caplog.records) == 1
