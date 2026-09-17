# llvm-static-builds

Precompiled LLVM static SDKs for embedding LLVM in shared libraries. These packages are build dependencies, not additional runtime files to deploy with an application.

## Build matrix

| Platform | Native runner | Toolchain | Runtime / minimum OS |
|---|---|---|---|
| Windows x86 | `windows-2025-vs2026` | clang-cl 23.1.1, x64 host tools | Release `/MD` |
| Windows x64 | `windows-2025-vs2026` | clang-cl 23.1.1 | Release `/MD` |
| Windows ARM64 | `windows-11-vs2026-arm` | clang-cl 23.1.1 | Release `/MD` |
| macOS x64 | `macos-15-intel` | Xcode 16.4 | macOS 15.0 |
| macOS ARM64 | `macos-15` | Xcode 16.4 | macOS 15.0 |

`build-config.json` pins LLVM 23.1.1, the official source SHA-256, and SDK revision 1. Exact compiler and runner image versions are recorded in each package. Hosted runner updates can change the compiler patch version; increment the SDK revision when publishing a rebuilt package. The macOS deployment target is an explicit build baseline, not a claim of compatibility with older systems.

Linux is not included: downstream distribution packages can declare their LLVM runtime dependency through the system package manager.

Windows builds use the SHA-256-pinned official Clang 23.1.1 compiler with MSVC v145 headers/libraries and the Windows SDK. The compiler runs natively on x64 or ARM64; x86 uses an explicit i686 target.

Windows x86 packages target modern Windows, not Windows XP. Their shared-library JIT smoke test runs as a 32-bit process on the x64 runner.

## Build and verify

Pushes and pull requests only run lightweight configuration tests. The expensive **Build static LLVM SDKs** workflow is manually dispatched; it runs on GitHub-hosted runners. The script refuses local builds unless explicitly given `--allow-local-build`.

The workflow verifies the official source archive, configures only the native LLVM target, and discovers the complete static dependency closure for `core`, `orcjit`, `passes`, and `nativecodegen` from LLVM's CMake targets. LLVM's `distribution` and `install-distribution` targets build and install those libraries, headers, and CMake exports. Clang and optional compression/XML dependencies are not included.

Each SDK is moved away from its installation prefix before configuring a fresh consumer. That consumer embeds LLVM in a shared library and executes an optimized ORC JIT function. Tests run from a separate directory without LLVM or Homebrew search paths; shared-library dependencies are inspected. Windows retains the host's normal MSVC runtime requirement; it does not require an LLVM DLL.

Successful jobs upload:

- A `.zip` (Windows) or `.tar.xz` (macOS), containing `include/`, `lib/`, `lib/cmake/llvm/`, `licenses/`, and `manifest.json` under one versioned root directory.
- A checksum file and an external copy of the manifest.
- Separate diagnostic logs, including on failure.

Actions artifacts expire after 14 days. Publish a successful build before expiry; expired artifacts require another build.

## Release procedure

1. Update and commit `build-config.json` and any build scripts on `master`.
2. Manually dispatch **Build static LLVM SDKs** on that commit. Wait for all five jobs to succeed and review their diagnostics and manifests.
3. Dispatch **Publish verified LLVM SDKs** from `master`, supplying the successful build's numeric run ID. This reuses its artifacts and does **not** rebuild LLVM.
4. The publisher checks that the run is a successful manual build from this repository's `master`, checks out its exact commit, and validates all five packages, checksums, and embedded manifests.
5. It creates a draft Release named `llvm-23.1.1-r1`, uploads the packages, manifests, and `SHA256SUMS`, then publishes it. The release tag points to the builder commit, not to an LLVM upstream commit. GitHub creates the tag through the release API; do not pre-create it.
6. Downstream CI pins the release asset URL and SHA-256. It may cache the downloaded SDK; a cache miss only causes another download, never an LLVM rebuild.

Published versions are never overwritten or retagged by the workflow. Update `revision` to publish changed build options, packaging, or toolchains (for example `llvm-23.1.1-r2`). An interrupted publication leaves a draft for inspection; the workflow refuses to overwrite it automatically. Repository administrators should enable immutable releases and tag protection where available.

Creating these workflows does not publish anything. Publishing requires an explicit manual dispatch of the publication workflow with repository write permission.

## Consume an SDK

Extract the archive and set `LLVM_DIR` to `<sdk>/lib/cmake/llvm`. Link static components rather than the `LLVM` or `LLVM-C` shared target:

```cmake
find_package(LLVM REQUIRED CONFIG)
llvm_map_components_to_libnames(llvm_libs core orcjit passes nativecodegen)
target_include_directories(my_library SYSTEM PRIVATE ${LLVM_INCLUDE_DIRS})
target_compile_definitions(my_library PRIVATE LLVM_BUILD_STATIC)
target_link_libraries(my_library PRIVATE ${llvm_libs})
```

Windows consumers must use a compatible MSVC toolchain and the same Release `/MD` runtime; these packages are not Debug `/MDd` SDKs. macOS consumers must target macOS 15.0 or newer and use a compatible Apple toolchain. See `tests/smoke` for a complete example.

LLVM is distributed under Apache-2.0 with LLVM exceptions and applicable third-party notices; the SDK includes LLVM's license text. This repository downloads upstream sources without maintaining a source fork.
