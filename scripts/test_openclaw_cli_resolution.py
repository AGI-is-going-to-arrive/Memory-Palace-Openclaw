#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import openclaw_memory_palace_release_gate as release_gate
import test_onboarding_apply_validate_e2e as onboarding_apply_validate
import test_openclaw_memory_palace_package_install as package_install


class OpenClawCliResolutionTests(unittest.TestCase):
    def test_onboarding_parse_args_prefers_openclaw_bin_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            wrapper = Path(tmp_dir) / "openclaw-wrapper"
            wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            wrapper.chmod(0o755)
            with mock.patch.dict(os.environ, {"OPENCLAW_BIN": str(wrapper)}, clear=False), mock.patch.object(
                onboarding_apply_validate.shutil, "which", return_value=None
            ), mock.patch.object(sys, "argv", ["test"]):
                args = onboarding_apply_validate.parse_args()
            self.assertEqual(args.openclaw_bin, str(wrapper))

    def test_package_install_accepts_explicit_openclaw_bin_outside_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            wrapper = Path(tmp_dir) / "openclaw-wrapper"
            wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            wrapper.chmod(0o755)
            with mock.patch.dict(os.environ, {"OPENCLAW_BIN": str(wrapper)}, clear=False), mock.patch.object(
                package_install.shutil, "which", return_value=None
            ):
                self.assertEqual(package_install.resolve_openclaw_bin_value(), str(wrapper))
                self.assertTrue(package_install.openclaw_bin_available())

    def test_release_gate_accepts_explicit_openclaw_bin_outside_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            wrapper = Path(tmp_dir) / "openclaw-wrapper"
            wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            wrapper.chmod(0o755)
            with mock.patch.dict(os.environ, {"OPENCLAW_BIN": str(wrapper)}, clear=False), mock.patch.object(
                release_gate.shutil, "which", return_value=None
            ):
                self.assertEqual(release_gate.resolve_openclaw_bin_value(), str(wrapper))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
