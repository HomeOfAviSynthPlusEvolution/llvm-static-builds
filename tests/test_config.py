import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build import audit_sdk, check_dependencies, load_config, package_name, sha256, verify_source
from publish import PLATFORMS, verify_artifacts


class ConfigurationTests(unittest.TestCase):
    def test_source_hash_rejects_wrong_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "source.tar.xz"
            archive.write_bytes(b"changed source")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                verify_source(archive, "0" * 64)
            verify_source(archive, hashlib.sha256(b"changed source").hexdigest())

    def test_sdk_rejects_shared_libraries_and_absolute_build_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            sdk = Path(temporary)
            config = sdk / "lib/cmake/llvm/LLVMConfig.cmake"
            config.parent.mkdir(parents=True)
            config.write_text('set(prefix "${CMAKE_CURRENT_LIST_DIR}/../../..")')
            audit_sdk(sdk, ("/old/build",))
            bad = sdk / "lib/LLVM-C.dll"
            bad.write_bytes(b"not static")
            with self.assertRaisesRegex(ValueError, "Shared library"):
                audit_sdk(sdk, ())
            bad.unlink()
            config.write_text('set(prefix "/old/build/lib")')
            with self.assertRaisesRegex(ValueError, "Nonrelocatable"):
                audit_sdk(sdk, ("/old/build",))

    def test_dependency_audits_reject_llvm_and_homebrew(self):
        windows = "    KERNEL32.dll\n    MSVCP140.dll\n    VCRUNTIME140.dll\n"
        self.assertEqual(len(check_dependencies(windows, "windows")), 3)
        with self.assertRaisesRegex(ValueError, "Unexpected DLL"):
            check_dependencies(windows + "    LLVM-C.dll\n", "windows")
        macos = ("libsample.dylib:\n"
                 "\t@rpath/libsdk_smoke.dylib (compatibility version 0.0.0, current version 0.0.0)\n"
                 "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)\n")
        self.assertEqual(check_dependencies(macos, "macos"), ["/usr/lib/libSystem.B.dylib"])
        with self.assertRaisesRegex(ValueError, "Unexpected dylib"):
            check_dependencies(macos + "\t/opt/homebrew/lib/libLLVM.dylib (compatibility version 1.0.0)\n", "macos")
        with self.assertRaisesRegex(ValueError, "No dependencies"):
            check_dependencies("unrecognized tool output", "windows")

    def make_artifacts(self, destination):
        config = load_config()
        for system, arch in PLATFORMS:
            name = package_name(config, system, arch)
            manifest = {"name": name, "llvm_version": config["version"], "sdk_revision": config["revision"],
                        "platform": system, "architecture": arch, "source_sha256": config["source_sha256"],
                        "builder_commit": "a" * 40, "run_id": "1234", "builder_repository": "owner/repo",
                        "verification": {"relocated_jit": "passed"}}
            text = json.dumps(manifest).encode()
            external = destination / f"{name}.manifest.json"
            external.write_bytes(text)
            archive = destination / (name + (".zip" if system == "windows" else ".tar.xz"))
            if system == "windows":
                with zipfile.ZipFile(archive, "w") as output:
                    output.writestr(name + "/manifest.json", text)
            else:
                with tarfile.open(archive, "w:xz") as output:
                    output.add(external, arcname=name + "/manifest.json")
            Path(str(archive) + ".sha256").write_text(f"{sha256(archive)}  {archive.name}\n")
        return config

    def test_publish_requires_complete_matching_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            config = self.make_artifacts(destination)
            packages = verify_artifacts(destination, config, "a" * 40, "1234", "owner/repo")
            self.assertEqual(len(packages), 5)
            with self.assertRaisesRegex(ValueError, "run_id"):
                verify_artifacts(destination, config, "a" * 40, "5678", "owner/repo")
            with self.assertRaisesRegex(ValueError, "builder_commit"):
                verify_artifacts(destination, config, "b" * 40, "1234", "owner/repo")
            packages[-1].unlink()
            with self.assertRaisesRegex(ValueError, "Missing artifact"):
                verify_artifacts(destination, config, "a" * 40, "1234", "owner/repo")

    def test_publish_rejects_tampered_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            config = self.make_artifacts(destination)
            archive = next(destination.glob("*.zip"))
            with archive.open("ab") as stream:
                stream.write(b"tampered")
            with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
                verify_artifacts(destination, config, "a" * 40, "1234", "owner/repo")

    def test_publish_rejects_manifest_disagreement(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            config = self.make_artifacts(destination)
            path = next(destination.glob("*.manifest.json"))
            manifest = json.loads(path.read_text())
            manifest["extra"] = "not in archive"
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "Embedded manifest"):
                verify_artifacts(destination, config, "a" * 40, "1234", "owner/repo")


if __name__ == "__main__":
    unittest.main()
