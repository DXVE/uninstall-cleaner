import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from uninstall_cleaner import paths, registry
from uninstall_cleaner.models import (
    ActionKind,
    Category,
    Finding,
    Hive,
    InstalledApp,
    Risk,
    ScanOptions,
)
from uninstall_cleaner.remover import Remover
from uninstall_cleaner.scanner import Scanner, _AppIndex


class PathGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows only")
        self.program_files = paths.env_path("ProgramFiles")
        if not self.program_files:
            self.skipTest("ProgramFiles is not set")

    def test_child_of_program_files_is_allowed(self) -> None:
        self.assertEqual(paths.protected_reason(os.path.join(self.program_files, "Foo")), "")
        self.assertEqual(paths.protected_reason(os.path.join(self.program_files, "FooBar")), "")

    def test_program_files_root_is_blocked(self) -> None:
        self.assertIn("根目录", paths.protected_reason(self.program_files))

    def test_windows_directory_is_blocked(self) -> None:
        windows = paths.env_path("SystemRoot")
        self.assertIn("Windows", paths.protected_reason(os.path.join(windows, "System32")))

    def test_recycle_bin_name_is_blocked(self) -> None:
        self.assertIn("保留目录", paths.protected_reason(r"C:\$Recycle.Bin"))


class CommandSplitTests(unittest.TestCase):
    def test_quoted_path(self) -> None:
        self.assertEqual(
            registry.split_command('"C:\\Program Files\\Demo\\app.exe" -x'),
            ("C:\\Program Files\\Demo\\app.exe", "-x"),
        )

    def test_unquoted_path_with_spaces(self) -> None:
        self.assertEqual(
            registry.split_command(r"C:\Program Files\Demo\uninstall.exe /S"),
            (r"C:\Program Files\Demo\uninstall.exe", "/S"),
        )

    def test_plain_command(self) -> None:
        self.assertEqual(registry.split_command("setup.exe"), ("setup.exe", ""))


class ContainerScanTests(unittest.TestCase):
    def _vendor_findings(self, layout: dict[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory(prefix="uc_test_") as base:
            vendor = Path(base) / "Vendor"
            for rel, content in layout.items():
                target = vendor / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            scanner = Scanner(ScanOptions(detect_unmatched=True, keywords=[]))
            scanner._index = _AppIndex([], [], [], [])
            scanner._consider_dir(str(vendor), Category.INSTALL_DIR, 1, True)
            return [Path(finding.location).name for finding in scanner._findings.values()]

    def test_vendor_with_stray_file_expands_to_child(self) -> None:
        names = self._vendor_findings({"App/app.exe": "x", "readme.txt": "x"})
        self.assertEqual(names, ["App"])

    def test_app_with_generic_bin_reports_parent(self) -> None:
        names = self._vendor_findings({"bin/app.exe": "x", "readme.txt": "x"})
        self.assertEqual(names, ["Vendor"])

    def test_vendor_with_multiple_apps_expands(self) -> None:
        names = self._vendor_findings({"AppOne/app.exe": "x", "AppTwo/app.exe": "x"})
        self.assertEqual(sorted(names), ["AppOne", "AppTwo"])

    def test_vendor_with_top_level_exe_and_multiple_apps_expands(self) -> None:
        names = self._vendor_findings(
            {"vendor.exe": "x", "AppOne/app.exe": "x", "AppTwo/app.exe": "x"}
        )
        self.assertEqual(sorted(names), ["AppOne", "AppTwo"])

    def test_vendor_with_installed_child_is_not_reported_as_whole(self) -> None:
        with tempfile.TemporaryDirectory(prefix="uc_test_") as base:
            vendor = Path(base) / "Vendor"
            installed = vendor / "AppOne"
            installed.mkdir(parents=True)
            (installed / "app.exe").write_text("x", encoding="utf-8")
            (vendor / "vendor.exe").write_text("x", encoding="utf-8")
            (vendor / "readme.txt").write_text("x", encoding="utf-8")
            scanner = Scanner(ScanOptions(detect_unmatched=True, keywords=[]))
            scanner._index = _AppIndex(
                [], [], [], [(paths.norm(str(installed)), InstalledApp("AppOne"))]
            )
            scanner._consider_dir(str(vendor), Category.INSTALL_DIR, 1, True)
            self.assertEqual(list(scanner._findings.values()), [])


class InstallLocationTests(unittest.TestCase):
    def test_broad_install_location_is_ignored(self) -> None:
        program_files = paths.env_path("ProgramFiles")
        if not program_files:
            self.skipTest("ProgramFiles is not set")
        app = InstalledApp(display_name="Demo", install_location=program_files)
        scanner = Scanner(ScanOptions())
        index = scanner._build_index([app])
        self.assertEqual(index.locations, [])

    def test_ancestor_of_user_profile_is_ignored(self) -> None:
        user_profile = paths.env_path("USERPROFILE")
        if not user_profile:
            self.skipTest("USERPROFILE is not set")
        app = InstalledApp(display_name="Demo", install_location=os.path.dirname(user_profile))
        self.assertEqual(Scanner(ScanOptions())._build_index([app]).locations, [])

    def test_system_component_is_ignored(self) -> None:
        program_files = paths.env_path("ProgramFiles")
        if not program_files:
            self.skipTest("ProgramFiles is not set")
        app = InstalledApp(
            display_name="Demo", install_location=os.path.join(program_files, "Common Files")
        )
        self.assertEqual(Scanner(ScanOptions())._build_index([app]).locations, [])

    def test_system_root_subdirectory_is_ignored(self) -> None:
        system_root = paths.env_path("SystemRoot")
        if not system_root:
            self.skipTest("SystemRoot is not set")
        app = InstalledApp(
            display_name="Demo", install_location=os.path.join(system_root, "System32")
        )
        self.assertEqual(Scanner(ScanOptions())._build_index([app]).locations, [])

    def test_app_subdirectory_is_kept(self) -> None:
        program_files = paths.env_path("ProgramFiles")
        if not program_files:
            self.skipTest("ProgramFiles is not set")
        location = os.path.join(program_files, "Vendor", "App")
        app = InstalledApp(display_name="Demo", install_location=location)
        index = Scanner(ScanOptions())._build_index([app])
        self.assertEqual(len(index.locations), 1)


class DirectorySizeTests(unittest.TestCase):
    def test_cancel_stops_walk(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with tempfile.TemporaryDirectory(prefix="uc_size_") as base:
            Path(base, "file.bin").write_bytes(b"x" * 32)
            size, count, truncated = paths.dir_size(base, cancel=cancel)
        self.assertEqual(size, 0)
        self.assertEqual(count, 0)
        self.assertTrue(truncated)


class RemoverTests(unittest.TestCase):
    def test_validate_returns_finding_and_reason(self) -> None:
        finding = Finding(
            category=Category.SHORTCUT,
            name="gone",
            location=str(Path(tempfile.gettempdir()) / "uc_missing_test.lnk"),
            risk=Risk.LOW,
            action=ActionKind.RECYCLE_FILE,
        )
        problems = Remover().validate([finding])
        self.assertEqual(len(problems), 1)
        self.assertIs(problems[0][0], finding)
        self.assertIn("路径已不存在", problems[0][1])

    def test_recycle_temp_file_marks_status(self) -> None:
        handle, path = tempfile.mkstemp(prefix="uc_remove_test_", suffix=".txt")
        os.close(handle)
        if not Remover.recycle_supported(path):
            os.remove(path)
            self.skipTest("temp volume has no recycle bin")
        finding = Finding(
            category=Category.SHORTCUT,
            name="temp",
            location=path,
            risk=Risk.LOW,
            action=ActionKind.RECYCLE_FILE,
        )
        summary = Remover().remove([finding])
        self.assertEqual(summary.succeeded, 1)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(finding.status, "已删除")
        self.assertFalse(os.path.exists(path))

    def test_partial_delete_marks_partial_status(self) -> None:
        finding = Finding(
            category=Category.INSTALL_DIR,
            name="partial",
            location=str(Path(tempfile.gettempdir()) / "uc_partial_test"),
            risk=Risk.LOW,
            action=ActionKind.RECYCLE_FILE,
        )
        with patch(
            "uninstall_cleaner.remover.recycle_path",
            return_value=(False, "部分删除：原路径已不存在，请检查回收站"),
        ):
            summary = Remover().remove([finding])
        self.assertEqual(summary.failed, 1)
        self.assertEqual(finding.status, "部分删除")
        self.assertTrue(finding.deletable)

    def test_pending_service_is_not_deletable(self) -> None:
        finding = Finding(
            category=Category.SERVICE,
            name="demo",
            location=r"SYSTEM\CurrentControlSet\Services\demo",
            risk=Risk.HIGH,
            action=ActionKind.DELETE_SERVICE,
            service_name="demo",
        )
        with patch(
            "uninstall_cleaner.remover.registry.delete_service",
            return_value=(True, "已标记删除，重启后生效"),
        ):
            ok, _ = Remover()._delete(finding)
        self.assertTrue(ok)
        self.assertEqual(finding.status, "待重启生效")
        self.assertFalse(finding.deletable)


class ServiceDeleteTests(unittest.TestCase):
    def test_pending_delete_returns_pending(self) -> None:
        proc = subprocess.CompletedProcess(
            args=["sc.exe"],
            returncode=1,
            stdout="",
            stderr="[SC] DeleteService FAILED 1072:",
        )
        with patch(
            "uninstall_cleaner.registry.stop_service", return_value=(True, "")
        ), patch("uninstall_cleaner.registry._run_sc", return_value=proc):
            ok, message = registry.delete_service("uc_demo")
        self.assertTrue(ok)
        self.assertEqual(message, registry.SERVICE_PENDING)

    def test_missing_service_is_treated_as_success(self) -> None:
        proc = subprocess.CompletedProcess(
            args=["sc.exe"],
            returncode=1,
            stdout="",
            stderr="[SC] OpenService FAILED 1060:",
        )
        with patch(
            "uninstall_cleaner.registry.stop_service", return_value=(True, "")
        ), patch("uninstall_cleaner.registry._run_sc", return_value=proc):
            ok, message = registry.delete_service("uc_demo")
        self.assertTrue(ok)
        self.assertEqual(message, "")


class ServiceScanTests(unittest.TestCase):
    def test_driver_service_is_protected(self) -> None:
        entry = registry.ServiceEntry(
            name="uc_probe",
            display_name="uc_probe",
            image_path=r"C:\uc_missing_driver.sys",
            service_type=0x01,
        )
        scanner = Scanner(ScanOptions())
        with patch("uninstall_cleaner.scanner.registry.services", return_value=[entry]):
            scanner._scan_services()
        findings = list(scanner._findings.values())
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0].protected)
        self.assertIn("驱动服务", findings[0].protected_reason)


class RemoveBatchTests(unittest.TestCase):
    @staticmethod
    def _findings() -> list[Finding]:
        return [
            Finding(
                Category.REGISTRY,
                "uc_a",
                r"HKCU\Software\uc_a",
                Risk.LOW,
                ActionKind.DELETE_REG_KEY,
                hive=Hive.HKCU,
                reg_path=r"Software\uc_a",
            ),
            Finding(
                Category.REGISTRY,
                "uc_b",
                r"HKCU\Software\uc_b",
                Risk.LOW,
                ActionKind.DELETE_REG_KEY,
                hive=Hive.HKCU,
                reg_path=r"Software\uc_b",
            ),
        ]

    def test_one_exception_does_not_abort_batch_and_manifest_is_written(self) -> None:
        findings = self._findings()
        first = findings[0]

        def fake_backup(remover, finding, target, index):
            if finding is first:
                raise RuntimeError("boom")
            return "", True, ""

        with tempfile.TemporaryDirectory(prefix="uc_batch_") as base:
            backup = Path(base) / "backup"
            with patch.object(Remover, "_backup", new=fake_backup), patch(
                "uninstall_cleaner.remover.registry.delete_key", return_value=(True, "")
            ):
                summary = Remover().remove(findings, str(backup))
            manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(summary.failed, 1)
            self.assertEqual(summary.succeeded, 1)
            self.assertEqual(len(manifest["items"]), 2)
            self.assertFalse((backup / "manifest.json.tmp").exists())


class PermanentDeleteTests(unittest.TestCase):
    @staticmethod
    def _finding() -> Finding:
        return Finding(
            Category.INSTALL_DIR,
            "remote",
            r"Z:\uc_probe",
            Risk.LOW,
            ActionKind.RECYCLE_FILE,
        )

    def test_non_fixed_without_confirmation_is_rejected(self) -> None:
        with patch("uninstall_cleaner.remover.recycle_supported", return_value=False):
            ok, message = Remover()._delete(self._finding())
        self.assertFalse(ok)
        self.assertIn("不支持回收站", message)

    def test_permanent_delete_is_counted(self) -> None:
        finding = self._finding()
        with patch(
            "uninstall_cleaner.remover.recycle_supported", return_value=False
        ), patch("uninstall_cleaner.remover.recycle_path", return_value=(True, "")) as recycle:
            summary = Remover().remove([finding], allow_permanent=True)
        self.assertEqual(summary.succeeded, 1)
        self.assertEqual(summary.permanent, 1)
        recycle.assert_called_once_with(finding.location, permanent=True)


class RegistryValueBackupTests(unittest.TestCase):
    def test_only_target_value_is_kept(self) -> None:
        sample = (
            "Windows Registry Editor Version 5.00\r\n\r\n"
            "[HKEY_CURRENT_USER\\Software\\UC]\r\n"
            '"Keep"=hex:01,02,\\\r\n'
            "  03,04\r\n"
            '"Other"="x"\r\n\r\n'
            "[HKEY_CURRENT_USER\\Software\\UC\\Sub]\r\n"
            '"Keep"="fake"\r\n'
        )
        with tempfile.TemporaryDirectory(prefix="uc_value_") as base:
            dest = Path(base) / "value.reg"

            def fake_export(hive, path, target):
                Path(target).write_bytes(sample.encode("utf-16"))
                return True, target

            with patch("uninstall_cleaner.registry.export_key", side_effect=fake_export):
                ok, message = registry.export_value(
                    Hive.HKCU, r"Software\UC", "Keep", str(dest)
                )
            text = dest.read_text(encoding="utf-16")
        self.assertTrue(ok, message)
        self.assertIn('"Keep"=hex:01,02,', text)
        self.assertIn("  03,04", text)
        self.assertNotIn('"Other"', text)
        self.assertNotIn(r"Software\UC\Sub", text)


class RecyclePolicyTests(unittest.TestCase):
    def tearDown(self) -> None:
        registry.recycle_disabled.cache_clear()

    def test_policy_disables_recycle(self) -> None:
        class FakeKey:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        registry.recycle_disabled.cache_clear()
        with patch(
            "uninstall_cleaner.registry.winreg.OpenKey", return_value=FakeKey()
        ), patch(
            "uninstall_cleaner.registry.winreg.QueryValueEx", return_value=(1, 4)
        ):
            self.assertTrue(registry.recycle_disabled())
            self.assertFalse(Remover.recycle_supported(r"C:\Windows"))

    def test_policy_absent(self) -> None:
        registry.recycle_disabled.cache_clear()
        with patch("uninstall_cleaner.registry.winreg.OpenKey", side_effect=OSError):
            self.assertFalse(registry.recycle_disabled())


class DeleteSelectionTests(unittest.TestCase):
    def test_hidden_checked_rows_are_not_selected(self) -> None:
        from uninstall_cleaner.ui import App

        visible = Finding(
            Category.INSTALL_DIR,
            "visible",
            r"C:\uc_visible",
            Risk.LOW,
            ActionKind.RECYCLE_FILE,
        )
        hidden = Finding(
            Category.INSTALL_DIR,
            "hidden",
            r"C:\uc_hidden",
            Risk.HIGH,
            ActionKind.RECYCLE_FILE,
        )

        class Widget:
            def configure(self, **kwargs):
                pass

        class Variable:
            def set(self, value):
                pass

        class RemoverFake:
            def validate(self, findings):
                return []

            def recycle_supported(self, path):
                return True

        captured = {}

        class ThreadFake:
            def __init__(self, target=None, args=(), daemon=False, **kwargs):
                captured["args"] = args

            def start(self):
                pass

        fake = type("Fake", (), {})()
        fake.scanner = None
        fake.removing = False
        fake.findings = [visible, hidden]
        fake.checked = {visible.key, hidden.key}
        fake.remover = RemoverFake()
        fake._visible_findings = lambda: [visible]
        fake._ask_backup_parent = lambda selected: ""
        fake._remove_worker = lambda *args: None
        fake.delete_btn = Widget()
        fake.scan_btn = Widget()
        fake.back_btn = Widget()
        fake.status_var = Variable()
        fake._refresh = lambda: None
        fake.backup_parent = ""

        with patch("uninstall_cleaner.ui.messagebox.askyesno", return_value=True), patch(
            "uninstall_cleaner.ui.threading.Thread", ThreadFake
        ):
            App._delete_checked(fake)
        self.assertEqual(App._checked_count(fake), 1)
        self.assertEqual(list(captured["args"][0]), [visible])


if __name__ == "__main__":
    unittest.main()
