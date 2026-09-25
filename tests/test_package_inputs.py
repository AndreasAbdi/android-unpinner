import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from android_unpinner import __main__ as app


class PackageInputTests(unittest.TestCase):
    def test_apkm_zip_extracts_only_apks_and_ignores_old_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "example apkm.zip"
            with zipfile.ZipFile(archive, "w") as out:
                out.writestr("info.json", "{}")
                out.writestr("base.apk", b"base")
                out.writestr("splits/config.apk", b"split")
            extracted = app.process_package_inputs([archive])
            self.assertEqual([p.read_bytes() for p in extracted], [b"base", b"split"])
            (root / "example apkm_extracted" / "stale.apk").write_bytes(b"old")
            self.assertEqual(app.process_package_inputs([archive]), extracted)

    def test_mixed_packages_are_installed_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            apks = [Path(temporary) / name for name in
                    ("first.apk", "first_split.apk", "second.apk")]
            apks[0].touch()
            names = {apks[0]: "first", apks[1]: "first", apks[2]: "second"}
            with patch.object(app, "process_package_inputs", return_value=apks), \
                 patch.object(app.build_tools, "package_name", side_effect=names.__getitem__), \
                 patch.object(app, "install_apk") as install:
                result = CliRunner().invoke(app.cli, ["install", str(apks[0])])
        self.assertIsNone(result.exception, result.output)
        self.assertEqual([call.args[0] for call in install.call_args_list],
                         [apks[:2], apks[2:]])

    def test_device_selects_one_abi_and_density_even_when_patched(self):
        apks = [Path(name) for name in (
            "base.unpinned.apk", "split_config.arm64_v8a.unpinned.apk",
            "split_config.armeabi_v7a.unpinned.apk", "split_config.mdpi.unpinned.apk",
            "split_config.xhdpi.unpinned.apk")]

        def adb(command):
            output = "arm64-v8a,armeabi-v7a" if "abilist" in command else "Physical density: 320"
            return type("Result", (), {"stdout": output})()

        with patch.object(app, "adb", side_effect=adb):
            self.assertEqual(app.select_apks_for_device(apks), [apks[0], apks[1], apks[4]])

    def test_all_starts_each_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            input_apk = Path(temporary) / "first.apk"
            input_apk.touch()
            apks = [input_apk, Path(temporary) / "second.apk"]
            with patch.object(app, "process_package_inputs", return_value=apks), \
                 patch.object(app.build_tools, "package_name", side_effect=["first", "second"]), \
                 patch.object(app, "patch_apk_files", side_effect=lambda files: files), \
                 patch.object(app, "copy_files") as copy, \
                 patch.object(app, "install_apk") as install, \
                 patch.object(app, "start_app_on_device") as start:
                result = CliRunner().invoke(app.cli, ["all", str(input_apk)])
        self.assertIsNone(result.exception, result.output)
        copy.assert_called_once()
        self.assertEqual(install.call_count, 2)
        self.assertEqual([call.args[0] for call in start.call_args_list], ["first", "second"])

    def test_archive_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "bad.apkm"
            with zipfile.ZipFile(archive, "w") as out:
                out.writestr("../escape.apk", b"bad")
            with self.assertRaisesRegex(ValueError, "Unsafe APK path"):
                app.process_package_inputs([archive])

    def test_missing_proxy_ca_fails_before_copying_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.pem"
            with patch.object(app, "ca_cert_file", missing), \
                 patch.object(app, "gadget_config_file", app.gadget_config_file_script_directory), \
                 patch.object(app, "adb") as adb:
                with self.assertRaisesRegex(RuntimeError, "--ca-cert"):
                    app.copy_files()
            adb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
