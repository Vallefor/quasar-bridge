#!/usr/bin/env python3
"""Package and BusyBox installation regression checks; no speaker required."""
import hashlib
import os
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackageTest(unittest.TestCase):
    def test_archives_contain_only_public_payload_and_matching_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for arch, elf in (("arm64", b"\x02\xb7\x00"), ("armv7", b"\x01\x28\x00")):
                binary = root / "binary"
                binary.write_bytes(b"\x7fELF" + elf[:1] + bytes(13) + elf[1:] + bytes(44))
                output = root / f"{arch}.tar.gz"
                result = subprocess.run([
                    "python3", str(ROOT / "scripts/build-speaker-package.py"),
                    "--arch", arch, "--binary", str(binary), "--output", str(output),
                    "--tag", "v1.2.3",
                ], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                with tarfile.open(output) as archive:
                    self.assertEqual(set(archive.getnames()), {
                        "bin/quasar-bridge", "start.sh", "control.sh", "install.sh",
                        "uninstall.sh", "S98quasar-bridge", "firmware.sh",
                        "config.device.example.json", "ARCH", "VERSION",
                        "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES",
                    })
                    self.assertEqual(archive.extractfile("ARCH").read(), (arch + "\n").encode())
                    self.assertEqual(archive.extractfile("VERSION").read(), b"1.2.3\n")
                    self.assertEqual(archive.extractfile("bin/quasar-bridge").read(), binary.read_bytes())
                    self.assertEqual(archive.getmember("install.sh").mode, 0o755)
                self.assertEqual(output.with_suffix(".gz.sha256").read_text(),
                                 f"{hashlib.sha256(output.read_bytes()).hexdigest()}  {output.name}\n")

    def test_wrong_architecture_does_not_produce_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "binary"
            binary.write_bytes(b"\x7fELF\x02" + bytes(13) + b"\x3e\x00" + bytes(44))
            output = root / "bad.tar.gz"
            result = subprocess.run([
                "python3", str(ROOT / "scripts/build-speaker-package.py"),
                "--arch", "arm64", "--binary", str(binary), "--output", str(output),
            ], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0,
                     "installation tests require Linux root (run in a disposable container)")
class InstallationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.init = self.root / "init.d"
        self.init.mkdir()
        self.package = self.root / "package"
        shutil.copytree(ROOT / "packaging/speaker", self.package)
        (self.package / "bin").mkdir()
        shutil.copy2(os.environ["QUASAR_TEST_BINARY"], self.package / "bin/quasar-bridge")
        (self.package / "bin/quasar-bridge").chmod(0o755)
        machine = os.uname().machine
        (self.package / "ARCH").write_text("armv7\n" if machine in ("armv7l", "armv8l") else "arm64\n")
        (self.package / "VERSION").write_text("fixture\n")
        for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES"):
            shutil.copy2(ROOT / name, self.package / name)
        self.env = dict(os.environ, QUASAR_DATA_DIR=str(self.state), QUASAR_INIT_DIR=str(self.init))
        if machine not in ("aarch64", "arm64", "armv7l", "armv8l"):
            # The lifecycle fixture uses a native binary; simulate only the speaker's uname.
            shim = self.root / "shim"
            shim.mkdir()
            (shim / "uname").write_text('#!/bin/sh\necho aarch64\n')
            (shim / "uname").chmod(0o755)
            self.env["PATH"] = str(shim) + os.pathsep + self.env["PATH"]
        self.addCleanup(self.stop)

    def run_script(self, path, *args, ok=True):
        result = subprocess.run(["sh", str(path), *args], env=self.env,
                                capture_output=True, text=True, timeout=45)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def control(self, action, **kwargs):
        return self.run_script(self.state / "app/control.sh", action, **kwargs)

    def install(self, **kwargs):
        return self.run_script(self.package / "install.sh", **kwargs)

    def stop(self):
        if (self.state / "app/control.sh").exists():
            self.control("stop")

    def test_install_update_and_uninstall_keep_state_and_boot_independently(self):
        self.install()
        self.control("status")
        self.control("restart")
        config = self.state / "config.json"
        data = json.loads(config.read_text())
        data["dialogue_context"]["max_turns"] = 9
        config.write_text(json.dumps(data))
        runtime = self.state / "runtime.env"
        runtime.write_text("GOMAXPROCS=2\n")
        backup = self.state / "endpoint-backup.json"
        backup.write_text("saved endpoint fixture")
        self.install()
        self.assertEqual(json.loads(config.read_text())["dialogue_context"]["max_turns"], 9)
        self.assertEqual(runtime.read_text(), "GOMAXPROCS=2\n")
        self.assertEqual(backup.read_text(), "saved endpoint fixture")
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o600)
        self.control("stop")
        # Persistent state can contain a service lock left by an older boot.
        (self.state / ".service-lock").mkdir()
        self.run_script(self.init / "S98quasar-bridge", "start")
        self.control("status")
        self.run_script(self.state / "app/uninstall.sh", ok=False)
        self.control("status")
        backup.unlink()  # Simulate a completed endpoint rollback/reboot.
        self.run_script(self.state / "app/uninstall.sh")
        self.assertFalse((self.state / "app").exists())
        self.assertFalse((self.init / "S98quasar-bridge").exists())
        self.assertTrue(config.exists())
        self.assertTrue(runtime.exists())

    def test_bad_config_rejected_before_working_process_is_stopped(self):
        self.install()
        old_pid = (self.state / "bridge.pid").read_text()
        config = self.state / "config.json"
        original = config.read_text()
        config.write_text('{"unknown_option":true}')
        self.install(ok=False)
        self.assertEqual((self.state / "bridge.pid").read_text(), old_pid)
        self.assertFalse((self.state / ".install-lock").exists())
        config.write_text(original)
        self.control("status")

    def test_failed_update_restores_previous_binary_and_service(self):
        self.install()
        original = (self.state / "app/bin/quasar-bridge").read_bytes()
        # A new launcher that validates but cannot run reproduces a broken update.
        (self.package / "start.sh").write_text(
            '#!/bin/sh\ncase "${1:-}" in -check-config) exit 0 ;; *) exit 1 ;; esac\n')
        self.install(ok=False)
        self.control("status")
        self.assertEqual((self.state / "app/bin/quasar-bridge").read_bytes(), original)
        self.assertFalse((self.state / ".install-lock").exists())

    def test_hook_write_failure_restores_read_only_mount_and_previous_service(self):
        self.install()
        original_hook = (self.init / "S98quasar-bridge").read_text()
        shim = self.root / "shim"
        shim.mkdir(exist_ok=True)
        (shim / "awk").write_text('#!/bin/sh\nexit 0\n')  # Simulate a read-only root.
        (shim / "mount").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$QUASAR_MOUNT_TRACE"\n')
        (shim / "awk").chmod(0o755)
        (shim / "mount").chmod(0o755)
        self.env["PATH"] = str(shim) + os.pathsep + self.env["PATH"]
        trace = self.root / "mount-trace"
        self.env["QUASAR_MOUNT_TRACE"] = str(trace)
        (self.init / "S98quasar-bridge.new").mkdir()  # Force publication to fail after start.
        self.install(ok=False)
        self.control("status")
        self.assertEqual((self.init / "S98quasar-bridge").read_text(), original_hook)
        self.assertEqual(trace.read_text().splitlines(), ["-o remount,rw /", "-o remount,ro /"])
        self.assertFalse((self.state / ".install-lock").exists())

    def test_stale_pid_does_not_signal_unrelated_process(self):
        self.install()
        self.control("stop")
        process = subprocess.Popen(["sleep", "60"])
        try:
            (self.state / "bridge.pid").write_text(str(process.pid))
            self.control("stop")
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            process.wait()

    def test_apply_and_rollback_preserve_other_firmware_fields(self):
        self.install()
        firmware = self.root / "firmware.json"
        original = {"unrelated": {"keep": True}, "aliced": {
            "uniProxyUrl": "wss://uniproxy.alice.yandex.net/uni.ws",
            "uniProxyQuicUrl": "quic://example.invalid", "keep": 42,
        }}
        firmware.write_text(json.dumps(original))
        self.env["QUASAR_DEVICE_CONFIG"] = str(firmware)
        self.control("apply")
        changed = json.loads(firmware.read_text())
        self.assertEqual(changed["aliced"]["uniProxyUrl"], "ws://127.0.0.1:8443/uni.ws")
        self.assertEqual(changed["aliced"]["uniProxyQuicUrl"], "")
        self.control("apply")  # Must not overwrite the original backup.
        self.control("stop")
        self.control("rollback")  # Recovery also works when the bridge is down.
        self.assertEqual(json.loads(firmware.read_text()), original)
        self.assertFalse((self.state / "endpoint-backup.json").exists())


if __name__ == "__main__":
    unittest.main()
