# Android Unpinner

This tool removes certificate pinning from APKs.

 - Does not require root.
 - Uses [`frida-apk`](https://github.com/frida/frida-tools/blob/main/frida_tools/apk.py) to mark app as debuggable.
   This is much less invasive than other approaches, only `AndroidManifest.xml` is touched within the APK.
 - Includes a custom Java Debug Wire Protocol implementation to inject the Frida Gadget via ADB.
 - Uses [HTTPToolkit's excellent unpinning script](https://github.com/httptoolkit/frida-android-unpinning) to defeat certificate pinning.
 - Already includes all native dependencies for Windows/Linux/macOS (`adb`, `apksigner`, `zipalign`, `aapt2`).
 - Handles XAPK and APKM archives (including APKM files saved as `.zip`) and extracted APK directories.
 - Accepts multiple packages in one command and installs each package's split APKs together.

The goal was not to build yet another unpinning tool, but to explore some newer avenues for non-rooted devices.
Please shamelessly copy whatever idea you like into other tools. :-)

## Installation

From this checkout, install Python 3.10+ and Java, then install the tool:

```console
cd android-unpinner
python -m pip install -e .
```

Java must be on `PATH` or available through `JAVA_HOME`. The Android tools used
for patching and ADB are bundled with this project. Have `adb` on `PATH` for the
manual device setup commands below.
If you use Android Studio's bundled Java on Windows, set it in PowerShell with
`$env:JAVA_HOME = 'C:\Program Files\Android\Android Studio\jbr'` before running the tool.

## Quick start

### 1. Connect the device

Enable USB debugging and check that ADB sees the device:

```console
adb devices
```

### 2. Start the proxy

For an Android emulator, start mitmproxy in one terminal, then point the
emulator at it from another terminal:

```console
mitmweb --listen-port 8083
adb shell settings put global http_proxy 10.0.2.2:8083
```

For a physical device, use your computer's network address instead of
`10.0.2.2`.
To return to a direct connection, clear the device proxy with
`adb shell settings put global http_proxy :0`.

### 3. Patch, install, and start

From this repository, the example Ring APKM is saved as a ZIP in the parent
directory:

```console
android-unpinner all --ca-cert "$HOME/.mitmproxy/mitmproxy-ca-cert.pem" "../Ring_3.113.0.zip"
```

Replace the CA path with the **public PEM certificate used by your proxy**.
For mitmproxy's default certificate, you may omit `--ca-cert`; the tool reads
`~/.mitmproxy/mitmproxy-ca-cert.pem`. `all` extracts the archive, patches and
signs the APKs, installs the compatible splits, pushes the unpinning scripts,
and launches Ring with Frida Gadget. If Ring is already installed, the tool
asks before uninstalling it; uninstalling removes its app data.

### 4. Launch it again

Every time you want to launch Ring again, start it through the tool:

```console
android-unpinner start-app com.ringapp
```

Injection applies to the running process. Starting Ring from its icon after
it exits will start it without the hooks. `start-app` stops any existing Ring
process and starts a new one with the hooks.

If your proxy CA changes, run `android-unpinner push-resources --ca-cert PATH`
and then `android-unpinner start-app com.ringapp`. The tool also accepts `.apk`,
`.apkm`, `.xapk`, APKM `.zip`, and extracted directories. You can pass files
from multiple packages to `all` or `install`.

If JDWP closes during startup, close Android Studio or another debugger
connected to the device and run `start-app` again. Mapbox's Cronet requests may
still reject the proxy CA because they use a separate certificate validation
path.

![screenshot](https://uploads.hi.ls/2022-03/2022-03-08_09-09-36.png)

See `android-unpinner --help` for usage details.

You can pull APKs from your device using `android-unpinner list-packages` and `android-unpinner get-apks`.
Alternatively, you can download APKs from the internet, for example manually from [apkpure.com](https://apkpure.com/) or automatically
using [apkeep](https://github.com/EFForg/apkeep).

## Comparison

**Compared to using a rooted device, android-unpinner...**

🟥 requires APK patching.
🟩 does not need to hide from root detection.

**Compared to [`apk-mitm`](https://github.com/shroudedcode/apk-mitm), android-unpinner...**

🟥 requires active instrumentation from a desktop machine when launching the app.
🟩 allows more dynamic patching at runtime (thanks to Frida).
🟩 does less invasive APK patching, e.g. `classes.dex` stays as-is.

**Compared to [`objection`](https://github.com/sensepost/objection), android-unpinner...**

🟥 supports only one feature (disable pinning) and no interactive analysis shell.
🟩 is easier to get started with, does not require additional dependencies.
🟩 does less invasive APK patching, e.g. `classes.dex` stays as-is.

**Compared to [`frida`](https://frida.re/) + [`LIEF`](https://lief-project.github.io/doc/latest/tutorials/09_frida_lief.html),
android-unpinner...**

🟥 modifies `AndroidManifest.xml`
🟩 is easier to get started with, does not require additional dependencies.
🟩 Does not require that the application includes a native library.

## Licensing

This tool stands on the shoulders of giants.

- `httptoolkit-pinning-demo.apk` is a copy of HTTP Toolkit's neat demo app available
  at https://github.com/httptoolkit/android-ssl-pinning-demo
  (Apache-2.0 License).
- `scripts/httptoolkit-unpinner.js` is a copy of HTTP Toolkit's excellent unpinning script available at
  https://github.com/httptoolkit/frida-android-unpinning/
  (AGPL License, Version 3.0 or later).
- `android_unpinner/vendor/frida/` contains the fantastic Frida gadgets available at https://frida.re/
  (wxWindows Library Licence, Version 3.1).
- `android_unpinner/vendor/frida-tools/` is adapted from https://github.com/frida/frida-tools
  (wxWindows Library Licence, Version 3.1).
- `android_unpinner/vendor/build_tools/` is a copy of some of Android's build tools
  (see `NOTICE.txt` therein for license).
- `android_unpinner/vendor/platform_tools/` is a copy of some of Android's platform tools
  (see `NOTICE.txt` therein for license).
- Code written here is licensed under the MIT license
  (https://github.com/mitmproxy/mitmproxy/blob/main/LICENSE).
