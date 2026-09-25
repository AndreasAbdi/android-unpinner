import os
import subprocess
import sys
from functools import cache
from pathlib import Path

here = Path(__file__).absolute().parent

if sys.platform == "win32":
    aapt2_binary = here / "win32" / "aapt2.exe"
    apksigner_binary = here / "win32" / "apksigner.bat"
    zipalign_binary = here / "win32" / "zipalign.exe"
elif sys.platform == "darwin":
    aapt2_binary = here / "darwin" / "aapt2"
    apksigner_binary = here / "darwin" / "apksigner"
    zipalign_binary = here / "darwin" / "zipalign"
else:
    aapt2_binary = here / "linux" / "aapt2"
    apksigner_binary = here / "linux" / "apksigner"
    zipalign_binary = here / "linux" / "zipalign"


def apksigner_command() -> list[str | Path]:
    if sys.platform == "win32":
        # The batch wrapper splits filenames containing parentheses.
        java_home = os.environ.get("JAVA_HOME")
        java = Path(java_home) / "bin" / "java.exe" if java_home else "java"
        return [java, "-jar", here / "win32" / "lib" / "apksigner.jar"]
    return [apksigner_binary]


def zipalign(apk_file: Path) -> None:
    apk_aligned = apk_file.with_suffix(".aligned.apk")
    subprocess.run([
        zipalign_binary,
        "-p", "4",
        apk_file,
        apk_aligned
    ], check=True)
    apk_file.unlink()
    apk_aligned.rename(apk_file)


def sign(apk_file: Path) -> None:
    # android-unpinner.jks was generated as follows:
    # keytool -genkey -v -keystore android-unpinner.jks -alias android-unpinner -keyalg RSA -keysize 2048 -validity 3650
    subprocess.run([
        *apksigner_command(),
        "sign",
        "--v4-signing-enabled",
        "false",
        '--ks',
        here / "android-unpinner.jks",
        '--ks-pass',
        'pass:correcthorsebatterystaple',
        '--ks-key-alias',
        'android-unpinner',
        apk_file
    ], check=True)


def verify_signature(apk_file: Path) -> bool:
    """Return whether apksigner accepts an existing patched APK."""
    return subprocess.run(
        [*apksigner_command(), "verify", apk_file],
        capture_output=True,
    ).returncode == 0


@cache
def package_name(apk_file: Path) -> str:
    return subprocess.run([
        aapt2_binary,
        "dump",
        "packagename",
        apk_file,
    ], check=True, capture_output=True, text=True).stdout.strip()
