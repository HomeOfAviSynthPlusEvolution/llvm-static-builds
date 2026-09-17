"""Build a relocatable static LLVM SDK on a dedicated CI runner."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_config():
    config = json.loads((ROOT / "build-config.json").read_text(encoding="utf-8"))
    if not re.fullmatch(r"\d+\.\d+\.\d+", config["version"]):
        raise ValueError("Invalid LLVM version")
    if not isinstance(config["revision"], int) or config["revision"] < 1:
        raise ValueError("SDK revision must be a positive integer")
    if not re.fullmatch(r"[0-9a-f]{64}", config["source_sha256"]):
        raise ValueError("Invalid source SHA-256")
    return config


def package_name(config, system, arch):
    if system not in ("windows", "macos") or arch not in ("x86", "x64", "arm64") or (system == "macos" and arch == "x86"):
        raise ValueError("Unsupported SDK platform")
    toolchain = (f"msvc-{config['windows_toolset']}-md" if system == "windows" else
                 f"xcode{config['xcode_version']}-macos{config['macos_deployment_target']}")
    return f"llvm-{config['version']}-r{config['revision']}-{system}-{arch}-{toolchain}"


def verify_source(path, expected):
    if sha256(path) != expected:
        raise ValueError("LLVM source SHA-256 mismatch")


def extract_source(archive, destination):
    """Extract build inputs only; do not unpack the large unused test suite."""
    with tarfile.open(archive, "r:xz") as source:
        for member in source:
            parts = Path(member.name).parts
            if len(parts) < 2:
                continue
            if parts[1] not in ("llvm", "libc", "cmake", "third-party", "LICENSE.TXT"):
                continue
            if len(parts) > 2 and parts[1] == "llvm" and parts[2] in (
                    "test", "unittests", "benchmarks", "docs", "examples"):
                continue
            source.extract(member, destination, filter="data")


def audit_sdk(sdk, forbidden_paths):
    if not (sdk / "lib/cmake/llvm/LLVMConfig.cmake").is_file():
        raise ValueError("Missing installed LLVM CMake configuration")
    for path in sdk.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in (".dll", ".dylib", ".so") or ".so." in path.name:
            raise ValueError(f"Shared library found in static SDK: {path}")
        if path.suffix == ".cmake":
            text = path.read_text(encoding="utf-8").replace("\\", "/").lower()
            for old in forbidden_paths:
                if str(old).replace("\\", "/").lower() in text:
                    raise ValueError(f"Nonrelocatable path in {path}: {old}")


def check_dependencies(output, system):
    if system == "windows":
        dependencies = sorted(set(re.findall(r"^\s+([A-Za-z0-9_.-]+\.dll)\s*$", output, re.M)), key=str.lower)
        system_dlls = {"kernel32.dll", "ntdll.dll", "advapi32.dll", "ole32.dll", "oleaut32.dll",
                       "shell32.dll", "user32.dll", "ws2_32.dll", "psapi.dll", "bcrypt.dll",
                       "version.dll", "secur32.dll", "ucrtbase.dll", "dbghelp.dll", "imagehlp.dll"}
        for dep in dependencies:
            lower = dep.lower()
            if lower not in system_dlls and not re.fullmatch(
                    r"(?:api-ms-win-[\w-]+|ext-ms-win-[\w-]+|msvcp140(?:_[\w]+)?|vcruntime140(?:_1)?)\.dll", lower):
                raise ValueError(f"Unexpected DLL dependency: {dep}")
    else:
        dependencies = re.findall(r"^\s+(.+?)\s+\(compatibility version", output, re.M)
        # The first entry is the shared library's own install name.
        dependencies = [dep for dep in dependencies if not dep.endswith("/libsdk_smoke.dylib")]
        for dep in dependencies:
            if not dep.startswith(("/usr/lib/", "/System/Library/")):
                raise ValueError(f"Unexpected dylib dependency: {dep}")
    if not dependencies:
        raise ValueError("No dependencies parsed; cannot validate the shared-library audit")
    return dependencies


def run(command, logfile, env=None, cwd=None):
    print("+ " + subprocess.list2cmdline([str(arg) for arg in command]), flush=True)
    with logfile.open("w", encoding="utf-8") as log:
        result = subprocess.run([str(arg) for arg in command], stdout=log, stderr=subprocess.STDOUT,
                                env=env, cwd=cwd)
    if result.returncode:
        print(logfile.read_text(encoding="utf-8", errors="replace")[-16000:], flush=True)
        raise RuntimeError(f"Command failed ({result.returncode}); see {logfile}")


def cache_values(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith(("#", "//")) and "=" in line:
            key, value = line.split("=", 1)
            values[key.split(":", 1)[0]] = value
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("windows", "macos"), required=True)
    parser.add_argument("--arch", choices=("x86", "x64", "arm64"), required=True)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--allow-local-build", action="store_true",
                        help="explicitly permit the expensive LLVM source build outside CI")
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true" and not args.allow_local_build:
        parser.error("Heavy builds run in CI. Local builds require --allow-local-build.")
    if args.platform == "macos" and args.arch == "x86":
        parser.error("macOS x86 is not supported")
    expected_system = "Windows" if args.platform == "windows" else "Darwin"
    expected_arch = "arm64" if args.arch == "arm64" else "x64"
    actual_arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if platform.system() != expected_system or actual_arch != expected_arch:
        parser.error("Use a matching native runner, or an x64 Windows runner for x86")
    if args.platform == "windows" and os.environ.get("VSCMD_ARG_TGT_ARCH") != args.arch:
        parser.error("MSVC target architecture does not match --arch")
    if args.jobs < 1:
        parser.error("jobs must be positive")
    config = load_config()
    work, dist = ROOT / "work", ROOT / "dist"
    if work.exists():
        parser.error("Use a fresh work directory for each SDK build; existing work was not modified")
    logs = work / "logs"
    logs.mkdir(parents=True)
    dist.mkdir(exist_ok=True)
    name = package_name(config, args.platform, args.arch)
    version = config["version"]
    archive = work / f"llvm-project-{version}.src.tar.xz"
    url = f"https://github.com/llvm/llvm-project/releases/download/llvmorg-{version}/{archive.name}"
    run(["curl", "--fail", "--location", "--show-error", "--connect-timeout", "30", "--max-time", "600",
         "--retry", "2", "--output", archive, url], logs / "download.log")
    verify_source(archive, config["source_sha256"])
    extract_source(archive, work)
    archive.unlink()
    source_root = work / f"llvm-project-{version}.src"
    source = source_root / "llvm"
    build = work / "llvm-build"
    stage = work / "stage" / name
    cmake = os.environ.get("SDK_CMAKE", "cmake")
    platform_options = []
    if args.platform == "windows":
        platform_options = ["-DCMAKE_C_COMPILER=cl", "-DCMAKE_CXX_COMPILER=cl",
                            "-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL",
                            "-DCMAKE_C_FLAGS=/utf-8", "-DCMAKE_CXX_FLAGS=/utf-8"]
    else:
        platform_options = ["-DCMAKE_C_COMPILER=/usr/bin/clang", "-DCMAKE_CXX_COMPILER=/usr/bin/clang++",
                            f"-DCMAKE_OSX_DEPLOYMENT_TARGET={config['macos_deployment_target']}",
                            f"-DCMAKE_OSX_ARCHITECTURES={'arm64' if args.arch == 'arm64' else 'x86_64'}"]
    options = ["-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release", *platform_options,
               "-DCMAKE_POSITION_INDEPENDENT_CODE=ON", f"-DCMAKE_INSTALL_PREFIX={stage.as_posix()}",
               f"-DLLVM_TARGETS_TO_BUILD={'AArch64' if args.arch == 'arm64' else 'X86'}",
               "-DLLVM_ENABLE_PROJECTS=", "-DLLVM_ENABLE_RUNTIMES=", "-DBUILD_SHARED_LIBS=OFF",
               "-DLLVM_BUILD_LLVM_DYLIB=OFF", "-DLLVM_BUILD_LLVM_C_DYLIB=OFF", "-DLLVM_LINK_LLVM_DYLIB=OFF",
               "-DLLVM_INCLUDE_TESTS=OFF", "-DLLVM_INCLUDE_BENCHMARKS=OFF", "-DLLVM_INCLUDE_EXAMPLES=OFF",
               "-DLLVM_INCLUDE_DOCS=OFF", "-DLLVM_BUILD_TOOLS=OFF", "-DLLVM_INCLUDE_UTILS=OFF",
               "-DLLVM_ENABLE_ASSERTIONS=OFF", "-DLLVM_ENABLE_ZLIB=OFF", "-DLLVM_ENABLE_ZSTD=OFF",
               "-DLLVM_ENABLE_LIBXML2=OFF", "-DLLVM_ENABLE_CURL=OFF", "-DLLVM_ENABLE_HTTPLIB=OFF",
               "-DLLVM_ENABLE_LIBEDIT=OFF", "-DLLVM_ENABLE_FFI=OFF", "-DLLVM_ENABLE_DIA_SDK=OFF",
               "-DLLVM_ENABLE_Z3_SOLVER=OFF", f"-DPython3_EXECUTABLE={sys.executable}"]
    # Discover the complete static dependency closure from LLVM's own CMake targets.
    run([cmake, "-S", source, "-B", build, *options], logs / "configure.log")
    probe = work / "components"
    run([cmake, "-S", ROOT / "cmake/components", "-B", probe, "-G", "Ninja",
         "-DCMAKE_BUILD_TYPE=Release", *platform_options,
         f"-DLLVM_DIR={(build / 'lib/cmake/llvm').as_posix()}", f"-DSDK_LLVM_VERSION={version}",
         "-DSDK_COMPONENTS=" + ";".join(config["components"])], logs / "components.log")
    components = (probe / "components.txt").read_text(encoding="utf-8").split(";")
    run([cmake, "-S", source, "-B", build, *options,
         "-DLLVM_DISTRIBUTION_COMPONENTS=" + ";".join(components)], logs / "distribution-configure.log")
    run([cmake, "--build", build, "--target", "distribution", "--parallel", args.jobs], logs / "build.log")
    run([cmake, "--build", build, "--target", "install-distribution", "--parallel", args.jobs], logs / "install.log")
    licenses = stage / "licenses"
    licenses.mkdir()
    shutil.copy2(source / "LICENSE.TXT", licenses / "LLVM-LICENSE.TXT")
    for notice in (source / "NOTICE", source_root / "NOTICE"):
        if notice.exists():
            shutil.copy2(notice, licenses / (notice.parent.name + "-NOTICE"))
    relocated = work / "relocated" / name
    relocated.parent.mkdir()
    shutil.move(stage, relocated)
    audit_sdk(relocated, (source_root, build, stage))
    smoke = work / "smoke-build"
    run([cmake, "-S", ROOT / "tests/smoke", "-B", smoke, "-G", "Ninja",
         "-DCMAKE_BUILD_TYPE=Release", *platform_options,
         f"-DLLVM_DIR={(relocated / 'lib/cmake/llvm').as_posix()}", f"-DSDK_LLVM_VERSION={version}",
         f"-DSDK_POINTER_SIZE={4 if args.arch == 'x86' else 8}"], logs / "smoke-configure.log")
    run([cmake, "--build", smoke, "--parallel", args.jobs], logs / "smoke-build.log")
    clean = work / "clean-test"
    clean.mkdir()
    shared_name = "sdk_smoke.dll" if args.platform == "windows" else "libsdk_smoke.dylib"
    exe_name = "sdk_smoke_test.exe" if args.platform == "windows" else "sdk_smoke_test"
    for filename in (shared_name, exe_name):
        shutil.copy2(smoke / filename, clean / filename)
    clean_env = os.environ.copy()
    for key in ("LLVM_DIR", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES"):
        clean_env.pop(key, None)
    clean_env["PATH"] = (os.pathsep.join([str(Path(os.environ["SystemRoot"]) / "System32"), os.environ["SystemRoot"]])
                         if args.platform == "windows" else "/usr/bin:/bin")
    # Run with no LLVM SDK or Homebrew directories on the library search path.
    run([clean / exe_name], logs / "smoke-test.log", env=clean_env, cwd=clean)
    dependency_command = (["dumpbin", "/DEPENDENTS", clean / shared_name] if args.platform == "windows" else
                          ["otool", "-L", clean / shared_name])
    run(dependency_command, logs / "dependencies.log")
    dependencies = check_dependencies((logs / "dependencies.log").read_text(encoding="utf-8", errors="replace"), args.platform)
    if args.platform == "macos":
        run(["otool", "-l", clean / shared_name], logs / "load-commands.log")
        load_commands = (logs / "load-commands.log").read_text()
        if "/opt/homebrew" in load_commands or "/usr/local/" in load_commands:
            raise ValueError("Homebrew path leaked into the smoke shared library")
    cache = cache_values(build / "CMakeCache.txt")
    compiler_info = next((build / "CMakeFiles").glob("*/CMakeCXXCompiler.cmake")).read_text(encoding="utf-8")
    pointer_size = re.search(r'set\(CMAKE_CXX_SIZEOF_DATA_PTR "?(\d+)"?\)', compiler_info)
    expected_pointer_size = 4 if args.arch == "x86" else 8
    if pointer_size is None or int(pointer_size.group(1)) != expected_pointer_size:
        raise ValueError("Compiler pointer size does not match SDK architecture")
    compiler_version = re.search(r'set\(CMAKE_CXX_COMPILER_VERSION "([^"]+)"\)', compiler_info).group(1)
    compiler_id = re.search(r'set\(CMAKE_CXX_COMPILER_ID "([^"]+)"\)', compiler_info).group(1)
    manifest = {"name": name, "llvm_version": version, "sdk_revision": config["revision"],
                "source_url": url, "source_sha256": config["source_sha256"],
                "platform": args.platform, "architecture": args.arch, "configuration": "Release",
                "runtime": "MD" if args.platform == "windows" else "system libc++",
                "compiler": {"id": compiler_id, "version": compiler_version,
                             "msvc_tools_version": os.environ.get("VCToolsVersion"),
                             "xcode": config["xcode_version"] if args.platform == "macos" else None},
                "macos_deployment_target": config["macos_deployment_target"] if args.platform == "macos" else None,
                "components": components, "cmake_options": {key: value for key, value in cache.items()
                  if key.startswith(("LLVM_ENABLE_", "LLVM_BUILD_", "LLVM_LINK_")) and ":" not in value and "/" not in value},
                "builder_commit": os.environ.get("GITHUB_SHA", "local"),
                "builder_repository": os.environ.get("GITHUB_REPOSITORY", "local"),
                "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
                "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "local"),
                "runner_image": os.environ.get("ImageVersion"),
                "verification": {"relocated_jit": "passed", "shared_library_dependencies": dependencies}}
    manifest_text = json.dumps(manifest, indent=2) + "\n"
    (relocated / "manifest.json").write_text(manifest_text, encoding="utf-8")
    (dist / f"{name}.manifest.json").write_text(manifest_text, encoding="utf-8")
    package = dist / (name + (".zip" if args.platform == "windows" else ".tar.xz"))
    if args.platform == "windows":
        with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
            for path in sorted(relocated.rglob("*")):
                if path.is_file():
                    output.write(path, path.relative_to(relocated.parent).as_posix())
    else:
        with tarfile.open(package, "w:xz", preset=3) as output:
            output.add(relocated, arcname=name)
    (dist / f"{package.name}.sha256").write_text(f"{sha256(package)}  {package.name}\n", encoding="utf-8")
    print(f"Verified SDK: {package} ({package.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
