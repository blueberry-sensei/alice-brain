# ALICE Desktop

> **Lưu ý:** quy trình phát hành dưới đây chưa được kích hoạt. Coi như tài liệu tham khảo.

ALICE Desktop wraps the existing Next.js workbench in Electron and manages two bundled services locally:

- the Next.js standalone local web runtime;
- the FastAPI/Python sidecar in PyInstaller `onedir` form.

The desktop build opens the full main panel by default, and the product UI and routes still come from `apps/web`. The first release does not split out a pet window and does not maintain a second frontend.

## Local development

Requirements:

- Node.js 20+；
- Python 3.11；
- the dependencies of `apps/web`, `apps/api` and `apps/desktop` are installed.

First-time setup:

```bash
cd apps/web
npm install

cd ../api
uv sync --extra dev --extra desktop

cd ../desktop
npm install
```

Start Web, API and Electron:

```bash
cd apps/desktop
npm run dev
```

When port 3000 or 8000 already runs the matching service, the dev script reuses it. Quitting Electron also stops the child processes the script created.

## Downloads and updates

Official installers are published on [`blueberry-sensei/alice-brain` Releases](https://github.com/blueberry-sensei/alice-brain/releases/latest):

- macOS Apple Silicon: the DMG installs, while the ZIP and `latest-mac.yml` drive auto-update;
- Windows x64: an as-yet unsigned NSIS EXE installs, while `latest.yml` and the blockmap drive auto-update; Windows may show an "unknown publisher" prompt;
- `SHA256SUMS.txt` verifies download integrity.

An installed client follows the `latest` stable channel of GitHub Releases by default. A release must be a non-draft official version; drafts and failed pipelines are never discovered by the client.

## Publishing to public with one command

An official release may only run from the root of a separate public clone of `blueberry-sensei/alice-brain`, on a clean, fully merged `main`. That clone must not add an internal repository remote and must not contain internal Git history:

```bash
make release-dry-run VERSION=1.4.0
make release VERSION=1.4.0
```

`scripts/release-public.mjs` will:

1. verify the current branch, a clean working tree, a strictly increasing stable SemVer, and that both the fetch and push remotes point at `blueberry-sensei/alice-brain`;
2. fetch and confirm that the local `main` contains `origin/main` and that their root commits match exactly, keeping internal or unrelated history out of the public repository;
3. sync the Desktop/Web/API runtime versions and the lockfiles, update the README badge, and archive `Unreleased` into this version;
4. create the `release: vX.Y.Z` commit and an immutable annotated tag;
5. push `main + vX.Y.Z` atomically to the public `origin`. If either ref fails to push, neither takes effect on the remote.

The tag then triggers `.github/workflows/desktop-release.yml`. The pipeline reuses the full CI gate and builds in parallel on the native `macos-15` ARM64 and `windows-2025` x64 runners; a public GitHub Release is created only when macOS signing and notarisation succeed, Windows explicitly produces an unsigned installer, and both platforms have complete update metadata and checksums.

The release script never builds or uploads a binary locally. If a push fails, the local release commit and tag remain, so it can be retried after investigation; never move or reuse a tag that is already public.

## The GitHub release environment

Open [`Settings -> Environments`](https://github.com/blueberry-sensei/alice-brain/settings/environments) in the public repository and create an environment named exactly `desktop-release`. If the deployment branch and tag restriction is on, allow both `main` (manual acceptance) and `v*.*.*` (official release tags); required reviewers can be added as a manual release gate.

Configure the following under **Environment secrets**:

| Secret | Purpose |
| --- | --- |
| `APPLE_CERTIFICATE_BASE64` | The single-line Base64 of the Developer ID Application `.p12` certificate including its private key; the pipeline maps it to `CSC_LINK` |
| `APPLE_CERTIFICATE_PASSWORD` | The `.p12` export password; the pipeline maps it to `CSC_KEY_PASSWORD` |
| `APPLE_ID` | The Apple Developer account email, used for notarisation |
| `APPLE_APP_SPECIFIC_PASSWORD` | The Apple ID app-specific password, used for notarisation; not the ordinary account password |
| `APPLE_TEAM_ID` | Apple Developer Team ID |

No plain environment variables are needed, and no GitHub PAT has to be created; the release job uses the `GITHUB_TOKEN` GitHub provides automatically and requests `contents: write` only in the final publish job.

- Create a **Developer ID Application** certificate under Certificates, Identifiers & Profiles in Apple Developer, then export it from the local keychain together with its private key as a password-protected `.p12`; that is where `APPLE_CERTIFICATE_BASE64` and `APPLE_CERTIFICATE_PASSWORD` come from.
- Create an app-specific password on the Apple ID account page and store it as `APPLE_APP_SPECIFIC_PASSWORD`; never put the ordinary Apple ID password into GitHub.
- `APPLE_TEAM_ID` is visible under Apple Developer Membership details.

On macOS, convert the certificate into the single-line Base64 you paste into the GitHub secret:

```bash
openssl base64 -A -in DeveloperIDApplication.p12 | pbcopy
```

Store the result as `APPLE_CERTIFICATE_BASE64`; the command only writes to the clipboard, so never paste the result into a terminal, issue, PR or log.

The `APPLE_SIGNING_IDENTITY` you already have is not needed today: after importing the `.p12`, electron-builder finds the Developer ID Application certificate by itself. A full identity usually carries the `Developer ID Application:` prefix, and mapping it straight to `CSC_NAME` is in fact rejected by the current builder; only when the `.p12` holds several certificates of the same kind should the prefix-free qualifier be confirmed and configured explicitly. `APPLE_PASSWORD` is never referenced by the pipeline either and is best removed from GitHub Secrets; notarisation uses only `APPLE_APP_SPECIFIC_PASSWORD`.

If these secrets already live at the repository level under **Settings -> Secrets and variables -> Actions**, the references still work and nothing needs recreating. For stricter isolation, copy the 5 secrets above into the `desktop-release` environment secrets; that environment is open only to the macOS release job.

Windows has no certificate secret for now, so the pipeline disables certificate auto-discovery and verifies the installer stays unsigned. The environment may carry a required reviewer as a manual release gate. The workflow requests only `contents: write` to create the release, and the macOS signing credentials are never passed to ordinary CI, a PR, a fork or the Windows build job.

Once the secrets are in place, run a manual acceptance from **Actions -> Desktop Release -> Run workflow** in the public repository against `main`. It runs the full quality gate, macOS signing and notarisation and the unsigned Windows build, and keeps temporary artifacts for 7 days; a manual run never creates a GitHub Release. Only pushing the annotated tag `vX.Y.Z` reaches the public publish step.

## Local builds and troubleshooting

A release build must run on the target operating system. The PyInstaller sidecar carries native libraries tied to the operating system and CPU architecture, so a releasable Windows sidecar cannot be produced on macOS.

macOS Apple Silicon：

```bash
cd apps/desktop
npm run dist:mac
```

Windows x64：

```powershell
cd apps/desktop
npm run dist:win
```

The build order is fixed:

1. compile the Electron main/preload;
2. rebuild the Next.js standalone with the desktop API address;
3. freeze the Python sidecar;
4. assemble Web, API and the runtime manifest;
5. produce the installer; macOS additionally signs and notarises, while Windows stays unsigned for now.

The artifacts land in `apps/desktop/release/`:

- macOS: the DMG installs, the ZIP drives auto-update;
- Windows: the NSIS installer and its update metadata.

To verify only the app directory without producing an installer, run:

```bash
npm run package:dir
```

## Build and release configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SAG_DESKTOP_APP_ID` | `ai.alice.brain` | The unique application identifier; do not change it casually after the first public release |
| `SAG_DESKTOP_API_PORT` | `8000` | The local API port; written into both the web build and the desktop runtime |
| `SAG_DESKTOP_WEB_PORT` | `32100` | The preferred local web port; when taken, it searches upward for a free one |
| `SAG_UPDATE_GITHUB_REPOSITORY` | unset | The GitHub update source, in `owner/repository` form; the official pipeline passes `blueberry-sensei/alice-brain` |
| `SAG_UPDATE_BASE_URL` | unset | An alternative generic update source root; it cannot be set together with the GitHub update source |
| `SAG_NOTARIZE` | `false` | Set to `true` to run macOS notarisation |
| `SAG_DESKTOP_PYTHON` | the Python in `apps/api/.venv` | The interpreter used to build the sidecar |
| `SAG_PYTHON_DIST_DIR` | the API's default frozen output directory | Reuses a sidecar already built in CI |

The macOS signing credentials are injected only into electron-builder's final signing and notarisation steps; they never reach Next.js, PyInstaller or their build dependencies, and they are never written into the repository. Windows has no signing credentials injected today. The application icon master and the platform artifacts live at `apps/desktop/assets/icon-master.png`, `icon.icns` and `icon.ico`.

`SAG_DESKTOP_API_PORT` is a release build parameter and is best not exposed to end users, because the API base inside Next.js is a build-time value. If it is changed, the build and the runtime must agree.

## Runtime and data directories

The official client listens on loopback only:

- Web: a dynamic port starting at `localhost:32100`;
- API/MCP：`127.0.0.1:8000`。

The database, uploaded files, knowledge engine data and the desktop runtime key are not written into the install directory but into Electron's standard `userData` directory:

- macOS：`~/Library/Application Support/SAG/`
- Windows：`%APPDATA%\SAG\`

An application update never overwrites that directory, and the Windows uninstaller is configured to keep user data by default.

## Update constraints

The desktop build uses whole-package versions and whole-package updates: Electron, Next.js, the Python API and their native dependencies are released under one `apps/desktop/package.json` version. Never update the web or the Python sidecar separately, or interface and data-migration compatibility cannot be guaranteed.

The public official build uses the GitHub provider and publishes the installer, the ZIP/EXE update payload, the blockmap, `latest-mac.yml` and `latest.yml` in one non-draft release. electron-builder generates `app-update.yml` inside the installer, and the client discovers later stable versions from it; never pin the update address to the download directory of one version tag.

A self-hosted fallback can set `SAG_UPDATE_BASE_URL` to use the generic provider, but it must then guarantee that one stable URL always serves the newest metadata and the matching payload. A development or local artifact with no provider configured generates no update configuration and never checks for updates.

## Pre-release checklist

At minimum:

```bash
npm run typecheck
npm run prepare:release
```

And verify on a clean target machine:

- the first install and the first cold start;
- the web login page and `/api/v1/system/ready`;
- document import, search, conversation, exploration mode and MCP;
- both local services stopping once the app quits;
- an in-place upgrade keeping user data;
- macOS signing and notarisation, the Windows "unknown publisher" install flow, and auto-update on both platforms.
