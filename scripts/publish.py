"""Publish existing verified CI artifacts without rebuilding LLVM."""
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import zipfile

from build import ROOT, load_config, package_name, sha256


PLATFORMS = (("windows", "x86"), ("windows", "x64"), ("windows", "arm64"), ("macos", "x64"), ("macos", "arm64"))


def verify_artifacts(dist, config, commit, run_id, repository):
    packages = []
    for system, arch in PLATFORMS:
        name = package_name(config, system, arch)
        archive = dist / (name + (".zip" if system == "windows" else ".tar.xz"))
        checksum = Path(str(archive) + ".sha256")
        manifest_path = dist / f"{name}.manifest.json"
        for path in (archive, checksum, manifest_path):
            if not path.is_file():
                raise ValueError(f"Missing artifact: {path.name}")
        expected_line = f"{sha256(archive)}  {archive.name}"
        if checksum.read_text().strip() != expected_line:
            raise ValueError(f"Checksum mismatch: {archive.name}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = {"name": name, "llvm_version": config["version"], "sdk_revision": config["revision"],
                    "platform": system, "architecture": arch, "source_sha256": config["source_sha256"],
                    "builder_commit": commit, "run_id": run_id, "builder_repository": repository}
        for key, value in expected.items():
            if manifest.get(key) != value:
                raise ValueError(f"Invalid {key} for {name}")
        if manifest.get("verification", {}).get("relocated_jit") != "passed":
            raise ValueError(f"SDK has not passed relocation/JIT verification: {name}")
        # Match the external manifest against the one actually shipped inside the package.
        member = f"{name}/manifest.json"
        if system == "windows":
            with zipfile.ZipFile(archive) as package:
                embedded = json.loads(package.read(member))
        else:
            with tarfile.open(archive, "r:xz") as package:
                stream = package.extractfile(member)
                if stream is None:
                    raise ValueError(f"Missing embedded manifest: {name}")
                embedded = json.load(stream)
        if embedded != manifest:
            raise ValueError(f"Embedded manifest mismatch: {name}")
        packages.append(archive)
    return packages


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Publishing is only supported by the manual CI workflow")
    config = load_config()
    commit = os.environ["SDK_BUILD_COMMIT"]
    run_id = os.environ["SDK_RUN_ID"]
    repo = os.environ["GH_REPO"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not run_id.isdecimal():
        raise ValueError("Invalid build identity")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if head != commit:
        raise ValueError("Publish checkout must match the verified build commit")
    tag = f"llvm-{config['version']}-r{config['revision']}"
    # Never move tags, overwrite release assets, or silently reuse an old draft.
    remote_tag = subprocess.check_output(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"], text=True)
    if remote_tag.strip():
        raise ValueError(f"Tag {tag} already exists; increment the SDK revision")
    releases = json.loads(subprocess.check_output(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100"], text=True))
    if any(release["tag_name"] == tag for page in releases for release in page):
        raise ValueError(f"Release {tag} already exists; inspect it before proceeding")
    dist = ROOT / "dist"
    packages = verify_artifacts(dist, config, commit, run_id, repo)
    manifests = [dist / f"{package_name(config, system, arch)}.manifest.json" for system, arch in PLATFORMS]
    sums = dist / "SHA256SUMS"
    sums.write_text("".join(f"{sha256(path)}  {path.name}\n" for path in [*packages, *manifests]), encoding="utf-8")
    notes = dist / "release-notes.md"
    notes.write_text(
        f"LLVM {config['version']} static SDK, packaging revision {config['revision']}.\n\n"
        f"- Windows x86, x64, and ARM64: clang-cl {config['windows_clang_version']}, MSVC v145 ABI, Release dynamic CRT (`/MD`).\n"
        f"- macOS x64 and ARM64: Xcode {config['xcode_version']}, deployment target {config['macos_deployment_target']}.\n"
        "- Static core, ORC JIT, optimization passes, native code generation, and their dependencies.\n"
        "- Relocated SDKs were used to build and execute a JIT inside a shared library.\n"
        "- Compiler details, source hash, and provenance are recorded in each manifest.\n\n"
        f"Build: https://github.com/{repo}/actions/runs/{run_id}\n\n"
        f"Builder commit: `{commit}`. Verify downloads with `SHA256SUMS`.\n",
        encoding="utf-8")
    assets = [str(path) for path in [*packages, *manifests, sums]]
    subprocess.run(["gh", "release", "create", tag, *assets, "--draft", "--target", commit,
                    "--title", tag, "--notes-file", str(notes)], check=True)
    subprocess.run(["gh", "release", "edit", tag, "--draft=false", "--latest=false"], check=True)
    print(f"Published {tag} from {commit}")


if __name__ == "__main__":
    main()
