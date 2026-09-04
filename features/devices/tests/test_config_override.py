"""Unit tests for config_override (RRO config override feature).

Covers the pure validation/rendering/build functions and the store; mocks adb
via run_local_shell_command for the device-dependent paths.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch
from foundation.command_result import CommandResult

from features.devices import config_explorer as ce
from features.devices import config_override as co
from features.devices.config_override import (
    OverrideEntry,
    OverrideStore,
    _split_array_items,
    build_config_xml,
    build_manifest,
    render_resource_xml,
    validate_override,
)


# validate_override + _split_array_items

class TestValidate(unittest.TestCase):
    def test_string_accepts_anything(self):
        self.assertEqual(validate_override("string", "hello world"), "hello world")
        self.assertEqual(validate_override("string", 'a & b < c > "d"'), 'a & b < c > "d"')

    def test_bool_normalizes_case(self):
        self.assertEqual(validate_override("bool", "TRUE"), "true")
        self.assertEqual(validate_override("bool", "False"), "false")
        with self.assertRaises(ValueError):
            validate_override("bool", "yes")

    def test_integer_decimal_only(self):
        self.assertEqual(validate_override("integer", "42"), "42")
        self.assertEqual(validate_override("integer", "-7"), "-7")
        with self.assertRaises(ValueError):
            validate_override("integer", "0x10")
        with self.assertRaises(ValueError):
            validate_override("integer", "3.14")

    def test_dimen_units(self):
        for ok in ("16dp", "0.5in", "-2px", "100sp", "12dip", "1mm", "2pt"):
            self.assertEqual(validate_override("dimen", ok), ok)
        with self.assertRaises(ValueError):
            validate_override("dimen", "16")          # missing unit
        with self.assertRaises(ValueError):
            validate_override("dimen", "16em")        # bad unit

    def test_fraction(self):
        self.assertEqual(validate_override("fraction", "50%"), "50%")
        self.assertEqual(validate_override("fraction", "100%p"), "100%p")
        with self.assertRaises(ValueError):
            validate_override("fraction", "50")

    def test_integer_array_items(self):
        self.assertEqual(validate_override("integer-array", "1\n2\n3"), "1\n2\n3")
        with self.assertRaises(ValueError):
            validate_override("integer-array", "1\nx\n3")

    def test_string_array_preserves_commas(self):
        # comma must survive inside an item (newline-separated)
        v = validate_override("string-array", "a,b\nc")
        self.assertEqual(v, "a,b\nc")

    def test_unknown_type_rejected(self):
        with self.assertRaises(ValueError):
            validate_override("color", "#fff")


class TestSplitArrayItems(unittest.TestCase):
    def test_basic_newline(self):
        self.assertEqual(_split_array_items("a\nb\nc"), ["a", "b", "c"])

    def test_trailing_newline_dropped(self):
        self.assertEqual(_split_array_items("a\nb\n"), ["a", "b"])

    def test_crlf(self):
        self.assertEqual(_split_array_items("a\r\nb"), ["a", "b"])

    def test_empty_kept(self):
        # deliberate empty item between two values
        self.assertEqual(_split_array_items("a\n\nb"), ["a", "", "b"])

    def test_empty_input(self):
        self.assertEqual(_split_array_items(""), [])


# render_resource_xml + escaping

class TestRenderXml(unittest.TestCase):
    def test_scalar_forms(self):
        self.assertEqual(render_resource_xml("string", "x", "v"), '<string name="x">v</string>')
        self.assertEqual(render_resource_xml("bool", "x", "true"), '<bool name="x">true</bool>')
        self.assertEqual(render_resource_xml("integer", "x", "42"), '<integer name="x">42</integer>')
        self.assertEqual(render_resource_xml("dimen", "x", "16dp"), '<dimen name="x">16dp</dimen>')
        self.assertEqual(render_resource_xml("fraction", "x", "50%"), '<fraction name="x">50%</fraction>')

    def test_xml_escaping(self):
        # & < > " ' must be escaped
        out = render_resource_xml("string", "x", 'a & b < c > "d" \'e\'')
        self.assertIn("a &amp; b &lt; c &gt; &quot;d&quot;", out)
        self.assertNotIn("a & b", out)

    def test_array_forms(self):
        self.assertEqual(
            render_resource_xml("string-array", "x", "a\nb"),
            '<string-array name="x"><item>a</item><item>b</item></string-array>',
        )
        self.assertEqual(
            render_resource_xml("integer-array", "x", "1\n2"),
            '<integer-array name="x"><item>1</item><item>2</item></integer-array>',
        )

    def test_array_item_escaping(self):
        out = render_resource_xml("string-array", "x", "a&b")
        self.assertIn("<item>a&amp;b</item>", out)


# build_config_xml + build_manifest

class TestBuilders(unittest.TestCase):
    def test_config_xml_structure(self):
        xml = build_config_xml([
            OverrideEntry("config_foo", "bool", "true"),
            OverrideEntry("config_bar", "string", "hello"),
        ])
        self.assertTrue(xml.startswith('<?xml version="1.0" encoding="utf-8"?>'))
        self.assertIn("<resources>", xml)
        self.assertIn('<bool name="config_foo">true</bool>', xml)
        self.assertIn('<string name="config_bar">hello</string>', xml)
        self.assertTrue(xml.rstrip().endswith("</resources>"))

    def test_config_xml_dup_rejected(self):
        with self.assertRaises(ValueError):
            build_config_xml([
                OverrideEntry("config_foo", "bool", "true"),
                OverrideEntry("config_foo", "string", "x"),
            ])

    def test_manifest_has_no_targetname(self):
        m = build_manifest()
        self.assertIn(f'package="{co.OVERLAY_PACKAGE}"', m)
        self.assertIn('android:targetPackage="android"', m)
        self.assertIn('android:isStatic="true"', m)
        self.assertIn('android:priority="9999"', m)
        self.assertNotIn("targetName", m)   # intentionally omitted


# OverrideStore

class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "overrides.json")
        self.store = OverrideStore(self.path)

    def test_roundtrip(self):
        self.store.upsert("dev1", OverrideEntry("config_foo", "bool", "true"))
        entries = self.store.list_entries("dev1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].resource_name, "config_foo")
        self.assertEqual(entries[0].value, "true")

    def test_upsert_overwrites_by_name(self):
        self.store.upsert("d", OverrideEntry("config_foo", "bool", "true"))
        self.store.upsert("d", OverrideEntry("config_foo", "bool", "false"))
        entries = self.store.list_entries("d")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].value, "false")

    def test_remove(self):
        self.store.upsert("d", OverrideEntry("config_foo", "bool", "true"))
        self.assertTrue(self.store.remove("d", "config_foo"))
        self.assertFalse(self.store.remove("d", "config_foo"))   # already gone
        self.assertEqual(self.store.list_entries("d"), [])

    def test_clear_returns_count(self):
        self.store.upsert("d", OverrideEntry("a", "bool", "true"))
        self.store.upsert("d", OverrideEntry("b", "bool", "false"))
        self.assertEqual(self.store.clear("d"), 2)
        self.assertEqual(self.store.list_entries("d"), [])

    def test_per_device_isolation(self):
        self.store.upsert("d1", OverrideEntry("a", "bool", "true"))
        self.store.upsert("d2", OverrideEntry("a", "bool", "false"))
        self.assertEqual(len(self.store.list_entries("d1")), 1)
        self.assertEqual(len(self.store.list_entries("d2")), 1)
        self.assertNotEqual(
            self.store.list_entries("d1")[0].value,
            self.store.list_entries("d2")[0].value,
        )

    def test_owner_namespaces_are_isolated(self):
        alice = OverrideStore(self.path, owner_id="alice")
        bob = OverrideStore(self.path, owner_id="bob")

        alice.upsert("d", OverrideEntry("config_foo", "bool", "true"))
        bob.upsert("d", OverrideEntry("config_foo", "bool", "false"))

        self.assertEqual(alice.list_entries("d")[0].value, "true")
        self.assertEqual(bob.list_entries("d")[0].value, "false")
        self.assertEqual(self.store.list_entries("d"), [])

    def test_empty_device_id_uses_default_key(self):
        self.store.upsert(None, OverrideEntry("a", "bool", "true"))
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("_default", data["device_overrides"])

    def test_atomic_write(self):
        """A tmp file should not linger after a successful write."""
        self.store.upsert("d", OverrideEntry("a", "bool", "true"))
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_corrupt_json_reset(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        store = OverrideStore(self.path)
        self.assertEqual(store.list_entries("d"), [])   # tolerates corruption

    def test_upsert_rejects_bad_name(self):
        with self.assertRaises(ValueError):
            self.store.upsert("d", OverrideEntry("1bad", "bool", "true"))
        with self.assertRaises(ValueError):
            self.store.upsert("d", OverrideEntry("config/foo", "bool", "true"))

    def test_concurrent_upserts_no_lost_update(self):
        """Two concurrent writers must both survive (flock serialization)."""
        import threading

        for i in range(8):
            self.store.upsert("d", OverrideEntry(f"config_item{i}", "bool", "true"))
        writers = []

        def write_many(prefix: str) -> None:
            store = OverrideStore(self.path)
            for i in range(10):
                store.upsert("d", OverrideEntry(f"{prefix}_item{i}", "bool", "true"))

        for prefix in ("a", "b", "c", "d"):
            t = threading.Thread(target=write_many, args=(prefix,))
            writers.append(t)
            t.start()
        for t in writers:
            t.join()

        names = {e.resource_name for e in self.store.list_entries("d")}
        for prefix in ("a", "b", "c", "d"):
            for i in range(10):
                self.assertIn(f"{prefix}_item{i}", names)
        # 8 原有 + 4×10 并发写入，一个都不能丢。
        self.assertEqual(len(names), 8 + 40)


# build_overlay_apk (real aapt2 if available, else skipped)

def _shutil_which(name):
    import shutil
    return shutil.which(name)


def _subprocess_run(cmd):
    import subprocess
    return subprocess.run(cmd, capture_output=True, text=True).stdout


@unittest.skipUnless(_shutil_which("aapt2"), "aapt2 not on PATH")
class TestBuildApk(unittest.TestCase):
    def _framework_apk(self):
        cached = os.path.join(ce._APK_CACHE_DIR, "android.apk")
        return cached if os.path.exists(cached) else None

    def test_build_real_apk(self):
        fw = self._framework_apk()
        if not fw:
            self.skipTest("no cached framework-res.apk")
        work = tempfile.mkdtemp(prefix="rro_test_")
        apk = co.build_overlay_apk(
            [OverrideEntry("config_defaultBrowser", "string", "com.test.verify")],
            work, fw,
        )
        self.assertTrue(os.path.exists(apk))
        self.assertGreater(os.path.getsize(apk), 0)
        # Verify the value is baked in via aapt2 dump.
        out = _subprocess_run([_shutil_which("aapt2"), "dump", "resources", apk])
        self.assertIn("com.test.verify", out)


# probe_status / apply / revert (mocked adb)

class TestProbeStatus(unittest.TestCase):
    def test_unreachable_device(self):
        store = OverrideStore(tempfile.mktemp())
        store.upsert("none", OverrideEntry("config_foo", "bool", "true"))
        with patch.object(co, "run_local_shell_command") as m:
            m.return_value = CommandResult(stdout="", stderr="", code=1)  # getprop fails
            status = co.probe_status("none", store)
        self.assertFalse(status.reachable)
        self.assertIsNone(status.build_type)
        self.assertEqual(status.configured_entry_count, 1)
        self.assertEqual(status.applied_entry_count, 1)

    def test_userdebug_with_overlay(self):
        seen_cmds = []

        def matcher(cmd, timeout=15):
            seen_cmds.append(cmd)
            if "ro.build.type" in cmd:
                return CommandResult(stdout="userdebug\n", stderr="", code=0)
            if "veritymode" in cmd:
                return CommandResult(stdout="disabled\n", stderr="", code=0)
            if "shell id" in cmd:
                return CommandResult(stdout="uid=0(root) gid=0(root)\n", stderr="", code=0)
            if "shell mount" in cmd:
                return CommandResult(stdout="/dev/block/dm-1 on /product type ext4 (rw,seclabel,relatime)\n", stderr="", code=0)
            if "ls " in cmd and OVERLAY_APK in cmd:
                return CommandResult(stdout="/product/overlay/GmsConfigOverrides.apk\n", stderr="", code=0)
            return CommandResult(stdout="", stderr="", code=0)
        with patch.object(co, "run_local_shell_command") as m:
            m.side_effect = matcher
            status = co.probe_status("dev")
        self.assertTrue(status.reachable)
        self.assertTrue(status.is_userdebug)
        self.assertTrue(status.verity_disabled)
        self.assertTrue(status.rooted)
        self.assertTrue(status.product_remountable)
        self.assertTrue(status.overlay_installed)
        self.assertFalse(any(" root" in cmd or cmd.strip().endswith("root") for cmd in seen_cmds))
        self.assertFalse(any("remount" in cmd for cmd in seen_cmds))


OVERLAY_APK = co.OVERLAY_APK_NAME


class TestVerity(unittest.TestCase):
    def _patch(self, output, code=0):
        m = patch.object(
            co, "run_local_shell_command",
            return_value=CommandResult(stdout=output, stderr="", code=code),
        )
        m.start()
        self.addCleanup(m.stop)

    def test_disable_needs_reboot(self):
        self._patch("Successfully disabled verity\nenabling overlayfs\n"
                    "Reboot the device for new settings to take effect\n")
        r = co.disable_verity("dev")
        self.assertTrue(r.success)
        self.assertTrue(r.needs_reboot)
        self.assertEqual(r.action, "disable")

    def test_disable_already_disabled(self):
        self._patch("Verity is already disabled\n")
        r = co.disable_verity("dev")
        self.assertTrue(r.success)
        self.assertFalse(r.needs_reboot)

    def test_enable_needs_reboot(self):
        self._patch("Successfully enabled verity\n"
                    "Reboot the device for new settings to take effect\n")
        r = co.enable_verity("dev")
        self.assertTrue(r.success)
        self.assertTrue(r.needs_reboot)
        self.assertEqual(r.action, "enable")

    def test_failure(self):
        self._patch("error: device offline\n", code=1)
        r = co.disable_verity("dev")
        self.assertFalse(r.success)

    def test_reboot(self):
        self._patch("", code=0)
        r = co.reboot_device("dev")
        self.assertTrue(r.success)
        self.assertTrue(r.rebooting)


class TestApply(unittest.TestCase):
    def test_empty_entries_no_op(self):
        store = OverrideStore(tempfile.mktemp())
        with patch.object(co, "run_local_shell_command") as m:
            result = co.apply_overrides("dev", store)
            self.assertEqual(m.call_count, 0)  # nothing ran
        self.assertTrue(result.success)
        self.assertEqual(result.stage, "validated")

    def test_apply_symbol_resolution_failure_is_reported(self):
        """APK resolution failures must return a structured result, not HTTP 500."""
        store = OverrideStore(tempfile.mktemp())
        store.upsert("dev", OverrideEntry("config_foo", "bool", "true"))
        with patch.object(co, "resolve_symbol_apk", side_effect=RuntimeError("adb pull failed")):
            result = co.apply_overrides("dev", store)
        self.assertFalse(result.success)
        self.assertEqual(result.stage, "error")
        self.assertIn("adb pull failed", result.message)

    def test_apply_reboots_on_success(self):
        store = OverrideStore(tempfile.mktemp())
        store.upsert("dev", OverrideEntry("config_defaultBrowser", "string", "com.test.verify"))
        fw = os.path.join(ce._APK_CACHE_DIR, "android.apk")
        if not (os.path.exists(fw) and _shutil_which("aapt2")):
            self.skipTest("needs cached framework-res.apk + aapt2")
        workdir_used = {}
        orig_build = co.build_overlay_apk
        def fake_build(entries, work_dir, symbol_apk, *a, **k):
            apk = orig_build(entries, work_dir, symbol_apk, *a, **k)
            workdir_used["apk"] = apk
            return apk
        def matcher(cmd, timeout=15):
            if "root" in cmd:
                return CommandResult(stdout="restarting adbd as root\n", stderr="", code=0)
            if "remount /product" in cmd:
                return CommandResult(stdout="Remount succeeded\n", stderr="", code=0)
            if "mkdir" in cmd:
                return CommandResult(stdout="", stderr="", code=0)
            if "push" in cmd:
                return CommandResult(stdout="pushed\n", stderr="", code=0)
            if "chcon" in cmd:
                return CommandResult(stdout="", stderr="", code=0)
            if "ls -lZ" in cmd:
                # SELinux context verification must see the expected label.
                return CommandResult(
                    stdout=(
                        "-rw-r--r-- root root u:object_r:system_file:s0 "
                        "/product/overlay/GmsConfigOverrides.apk\n"
                    ),
                    stderr="",
                    code=0,
                )
            if "reboot" in cmd:
                return CommandResult(stdout="", stderr="", code=0)
            return CommandResult(stdout="", stderr="", code=0)
        with patch.object(co, "resolve_symbol_apk", return_value=fw), \
             patch.object(co, "build_overlay_apk", side_effect=fake_build), \
             patch.object(co, "run_local_shell_command", side_effect=matcher), \
             patch("features.devices.adb_ops.time.sleep", return_value=None):
            result = co.apply_overrides("dev", store)
        self.assertTrue(result.success)
        self.assertTrue(result.rebooting)
        self.assertEqual(result.stage, "rebooting")
        # TemporaryDirectory cleans up; the staged APK must be gone afterwards.
        self.assertFalse(os.path.exists(workdir_used["apk"]))

    def test_apply_chcon_failure_reported(self):
        """chcon failing must NOT report success (no more fake-success)."""
        store = OverrideStore(tempfile.mktemp())
        store.upsert("dev", OverrideEntry("config_foo", "bool", "true"))
        fake_apk = tempfile.NamedTemporaryFile(suffix=".apk", delete=False)  # noqa: SIM115
        fake_apk.write(b"placeholder")
        fake_apk.close()
        self.addCleanup(os.unlink, fake_apk.name)
        def matcher(cmd, timeout=15):
            if "root" in cmd:
                return CommandResult(stdout="restarting adbd as root\n", stderr="", code=0)
            if "remount /product" in cmd:
                return CommandResult(stdout="Remount succeeded\n", stderr="", code=0)
            if "chcon" in cmd:
                return CommandResult(stdout="chcon: failed\n", stderr="", code=1)
            if "ls -lZ" in cmd:
                return CommandResult(stdout="-rw-r--r-- root root u:object_r:system_data_file:s0 apk\n", stderr="", code=0)
            return CommandResult(stdout="", stderr="", code=0)
        with patch.object(co, "resolve_symbol_apk", return_value=fake_apk.name), \
             patch.object(co, "build_overlay_apk", return_value=fake_apk.name), \
             patch.object(co, "run_local_shell_command", side_effect=matcher), \
             patch("features.devices.adb_ops.time.sleep", return_value=None):
            result = co.apply_overrides("dev", store)
        self.assertFalse(result.success)
        self.assertEqual(result.stage, "chcon")


if __name__ == "__main__":
    unittest.main()
