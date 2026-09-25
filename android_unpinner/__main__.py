from __future__ import annotations

import os
import zipfile
import asyncio
import logging
import shutil
import subprocess
import re
import json
import ssl
import tempfile
from pathlib import Path
from time import sleep

import rich.traceback
import rich_click as click
from rich.logging import RichHandler

from . import jdwplib
from .vendor import build_tools
from .vendor import frida_tools
from .vendor import gadget_config_file_listen, gadget_config_file_script_directory
from .vendor import gadget_files
from .vendor.platform_tools import adb, set_device

here = Path(__file__).absolute().parent
LIBGADGET = "libgadget.so"
LIBGADGET_CONF = "libgadget.config.so"

force = False
gadget_config_file = gadget_config_file_script_directory
ca_cert_file: Path | None = None


def patch_apk_file(infile: Path, outfile: Path) -> None:
    """
    Patch the APK to be debuggable.
    """
    if outfile.exists():
        if not force:
            click.confirm(
                f"Overwrite existing file: {outfile.absolute()}?", abort=True
            )

    # Publish only a fully signed APK. A failed signing step must not leave
    # an unsigned .unpinned.apk that a later run could mistake for a result.
    with tempfile.TemporaryDirectory(dir=outfile.parent) as temporary:
        # apksigner.bat parses parentheses in filenames as batch syntax on Windows.
        candidate = Path(temporary) / "patched.apk"
        logging.info("Make APK debuggable...")
        frida_tools.apk.make_debuggable(str(infile), str(candidate))

        logging.info("Zipalign & re-sign APK...")
        build_tools.zipalign(candidate)
        build_tools.sign(candidate)
        candidate.replace(outfile)

    logging.info(f"Created patched APK: {outfile}")


def patch_apk_files(apks: list[Path]) -> list[Path]:
    """
    Patch multiple APK files and return the list of patched filenames.
    """
    patched: list[Path] = []
    for apk in apks:
        if apk.stem.endswith(".unpinned"):
            logging.warning(
                f"Skipping {apk} (filename indicates it is already patched)."
            )
            continue

        outfile = apk.with_suffix(".unpinned" + apk.suffix)
        if outfile.exists() and not force and build_tools.verify_signature(outfile):
            logging.warning(f"Reusing existing file: {outfile}")
        else:
            if outfile.exists() and not force:
                logging.warning(
                    f"Existing patched APK has no valid signature; rebuilding: {outfile}"
                )
                outfile.unlink()
            logging.info(f"Patching {apk}...")
            patch_apk_file(apk, outfile)
        patched.append(outfile)
    return patched


def ensure_device_connected() -> None:
    try:
        adb("get-state")
    except subprocess.CalledProcessError:
        raise RuntimeError("No device connected via ADB.")


def install_apk(apk_files: list[Path]) -> None:
    """
    Install the APK on the device, replacing any existing installation.
    """
    ensure_device_connected()

    package_names = {build_tools.package_name(apk) for apk in apk_files}
    if len(package_names) != 1:
        raise ValueError("install_apk requires APKs for exactly one package")
    package_name = next(iter(package_names))
    apk_files = select_apks_for_device(apk_files)

    if package_name in get_packages():
        if not force:
            click.confirm(
                "About to install patched APK. This removes the existing app with all its data. Continue?",
                abort=True,
            )

        logging.info("Uninstall existing app...")
        adb(f"uninstall {package_name}")

    logging.info(f"Installing {package_name}...")
    if len(apk_files) > 1:
        adb(f"install-multiple --no-incremental {' '.join(quote_arg(x) for x in apk_files)}")
    else:
        adb(f"install --no-incremental {quote_arg(apk_files[0])}")


def quote_arg(value: Path) -> str:
    """Quote a local path for the shell used by the ADB helper."""
    if os.name == "nt":
        return f'"{value}"'
    import shlex
    return shlex.quote(str(value))


def select_apks_for_device(apks: list[Path]) -> list[Path]:
    """Choose one ABI and density configuration split supported by the device."""
    abi_names = {"arm64_v8a", "armeabi_v7a", "x86", "x86_64"}
    densities = {"ldpi": 120, "mdpi": 160, "tvdpi": 213, "hdpi": 240,
                 "xhdpi": 320, "xxhdpi": 480, "xxxhdpi": 640}
    def split_key(apk: Path) -> str:
        return apk.stem.removesuffix(".unpinned").removeprefix("split_config.")

    abi_splits = {split_key(apk): apk for apk in apks
                  if apk.stem.startswith("split_config.") and split_key(apk) in abi_names}
    density_splits = {split_key(apk): apk for apk in apks
                      if apk.stem.startswith("split_config.") and split_key(apk) in densities}
    selected = set(apks)
    if len(abi_splits) > 1:
        supported = adb("shell getprop ro.product.cpu.abilist").stdout.strip().split(",")
        match = next((abi_splits[abi.replace("-", "_")] for abi in supported
                      if abi.replace("-", "_") in abi_splits), None)
        if match is None:
            raise ValueError(f"No compatible ABI split for device: {list(abi_splits)}")
        selected.difference_update(abi_splits.values())
        selected.add(match)
    if len(density_splits) > 1:
        output = adb("shell wm density").stdout
        matches = re.findall(r"(?:Override|Physical) density: (\d+)", output)
        if not matches:
            raise ValueError(f"Could not determine device density: {output!r}")
        density = int(matches[-1])
        match_name = min(density_splits, key=lambda name: abs(densities[name] - density))
        selected.difference_update(density_splits.values())
        selected.add(density_splits[match_name])
    return [apk for apk in apks if apk in selected]


def find_apks_in_archive(archive_path: Path) -> list[Path]:
    """Extract APK members of an XAPK or APKM into a persistent work directory."""
    extraction_dir = archive_path.parent / f"{archive_path.stem}_extracted"
    logging.info(f"Processing package archive: {archive_path.name}")
    with zipfile.ZipFile(archive_path) as archive:
        members = [info for info in archive.infolist()
                   if not info.is_dir() and info.filename.lower().endswith(".apk")
                   and not Path(info.filename).stem.endswith(".unpinned")]
        if not members:
            raise ValueError(f"No APK files found in {archive_path}")
        extraction_dir.mkdir(exist_ok=True)
        apks = []
        for member in members:
            # Archive paths must stay inside the extraction directory.
            target = (extraction_dir / member.filename).resolve()
            if not target.is_relative_to(extraction_dir.resolve()):
                raise ValueError(f"Unsafe APK path in {archive_path}: {member.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            apks.append(target)
    return apks


def process_package_inputs(inputs: list[Path]) -> list[Path]:
    """Expand APKs, extracted directories, XAPKs, and APKM archives."""
    apks = []
    for path in inputs:
        if path.is_dir():
            found = sorted(p for p in path.rglob("*.apk")
                           if not p.stem.endswith(".unpinned"))
            if not found:
                raise ValueError(f"No APK files found in {path}")
            apks.extend(found)
        elif path.suffix.lower() in {".xapk", ".apkm", ".zip"}:
            apks.extend(find_apks_in_archive(path))
        elif path.suffix.lower() == ".apk":
            apks.append(path)
        else:
            raise ValueError(f"Unsupported package input: {path}")
    return apks


def group_apks_by_package(apks: list[Path]) -> dict[str, list[Path]]:
    packages: dict[str, list[Path]] = {}
    for apk in apks:
        packages.setdefault(build_tools.package_name(apk), []).append(apk)
    return packages


def copy_files() -> None:
    """
    Copy the Frida Gadget and unpinning scripts.
    """
    # TODO: We could later provide the option to use a custom script dir.
    ca_pem = None
    if gadget_config_file == gadget_config_file_script_directory:
        certificate = ca_cert_file or Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
        if not certificate.is_file():
            raise RuntimeError(
                "Proxy CA certificate not found. Pass --ca-cert with the PEM certificate "
                "used by your proxy."
            )
        ca_pem = certificate.read_text(encoding="ascii")
        try:
            ssl.PEM_cert_to_DER_cert(ca_pem)
        except ValueError as exc:
            raise ValueError(f"Invalid PEM certificate: {certificate}") from exc

    ensure_device_connected()
    logging.info("Detect architecture...")
    abi = adb("shell getprop ro.product.cpu.abi").stdout.strip()
    if abi == "armeabi-v7a":
        abi = "arm"
    gadget_file = gadget_files.get(abi, gadget_files["arm64"])
    logging.info(f"Copying matching gadget: {gadget_file.name}...")
    adb(f"push {gadget_file} /data/local/tmp/{LIBGADGET}")
    adb(f"push {gadget_config_file} /data/local/tmp/{LIBGADGET_CONF}")

    logging.info("Copying builtin Frida scripts to /data/local/tmp/android-unpinner...")
    adb("shell mkdir -p /data/local/tmp/android-unpinner")
    adb(f"push {quote_arg(here / 'scripts' / 'hide-debugger.js')} /data/local/tmp/android-unpinner/")
    if ca_pem is not None:
        # The vendored script expects CERT_PEM in its own JS runtime. A separate
        # config.js in the script directory would not share that runtime.
        source = (here / "scripts" / "httptoolkit-unpinner.js").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "httptoolkit-unpinner.js"
            configured.write_text(
                f"const CERT_PEM = {json.dumps(ca_pem)};\n"
                "const __au_log = new NativeFunction(Module.getExportByName(null, '__android_log_write'), 'int', ['int', 'pointer', 'pointer']);\n"
                "__au_log(4, Memory.allocUtf8String('android-unpinner'), Memory.allocUtf8String('Frida script started'));\n"
                "try {\n" + source +
                "\n} catch (error) { __au_log(6, Memory.allocUtf8String('android-unpinner'), Memory.allocUtf8String(String(error))); throw error; }\n",
                encoding="utf-8",
            )
            adb(f"push {quote_arg(configured)} /data/local/tmp/android-unpinner/httptoolkit-unpinner.js")
    active_scripts = adb("shell ls /data/local/tmp/android-unpinner").stdout.splitlines(
        keepends=False
    )
    logging.info(f"Active frida scripts: {active_scripts}")


def start_app_on_device(package_name: str) -> None:
    ensure_device_connected()
    logging.info("Stop any existing app process...")
    adb(f"shell am force-stop {package_name}")
    logging.info("Start app (suspended)...")
    adb(f"shell am set-debug-app -w {package_name}")
    activity = adb(
        f'shell cmd "package resolve-activity --brief {package_name} | tail -n 1"'
    ).stdout.strip()
    adb(
        "shell am start -a android.intent.action.MAIN "
        "-c android.intent.category.LAUNCHER -f 0x10200000 "
        f"-n {activity}"
    )

    logging.info("Obtain process id...")
    pid = None
    for i in range(5):
        try:
            pid = adb(f"shell pidof {package_name}").stdout.strip()
            break
        except subprocess.CalledProcessError:
            if i:
                logging.info("Timeout...")
            if i == 4:
                raise
            sleep(1)
    logging.debug(f"{pid=}")
    local_port = int(adb(f"forward tcp:0 jdwp:{pid}").stdout)
    logging.debug(f"{local_port=}")

    async def inject_frida():
        logging.info("Establish Java Debug Wire Protocol Connection over ADB...")
        async with jdwplib.JDWPClient("127.0.0.1", local_port) as client:
            logging.info("Advance until android.app.Activity.onCreate...")
            thread_id = await client.advance_to_breakpoint(
                "Landroid/app/Activity;", "onCreate"
            )
            logging.info("Copy Frida gadget into app...")
            await client.exec(
                thread_id,
                f"cp /data/local/tmp/{LIBGADGET} /data/data/{package_name}/{LIBGADGET}",
            )
            await client.exec(
                thread_id,
                f"cp /data/local/tmp/{LIBGADGET_CONF} /data/data/{package_name}/{LIBGADGET_CONF}",
            )
            logging.info("Inject Frida gadget...")
            await client.load(thread_id, f"/data/data/{package_name}/{LIBGADGET}")
            logging.info("Continue app execution...")
            await client.send_command(jdwplib.Commands.RESUME_VM)

    asyncio.run(inject_frida())


def get_packages() -> list[str]:
    packages = adb("shell pm list packages").stdout.strip().splitlines()
    return [p.removeprefix("package:") for p in sorted(packages)]


@click.group()
def cli():
    rich.traceback.install(suppress=[click, click.core])


def _verbosity(ctx, param, verbose):
    logging.basicConfig(
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                show_path=False, show_level=verbose > 0, omit_repeated_times=False
            )
        ],
    )
    if verbose == 0:
        logging.getLogger().setLevel("INFO")
        logging.getLogger("jdwplib").setLevel("WARNING")
    elif verbose == 1:
        logging.getLogger().setLevel("INFO")
    else:
        logging.getLogger().setLevel("DEBUG")


verbosity_option = click.option(
    "-v",
    "--verbose",
    count=True,
    metavar="",
    help="Log verbosity. Can be passed twice.",
    callback=_verbosity,
    expose_value=False,
)


def _force(ctx, param, val):
    global force
    force = val


force_option = click.option(
    "-f",
    "--force",
    help="Affirmatively answer all safety prompts.",
    is_flag=True,
    callback=_force,
    expose_value=False,
)


def _listen(ctx, param, val):
    global gadget_config_file
    if val:
        gadget_config_file = gadget_config_file_listen


listen_option = click.option(
    "-l",
    "--listen",
    help="Configure the Frida gadget to expose a server instead of running unpinning scripts.",
    is_flag=True,
    callback=_listen,
    expose_value=False,
)


def _ca_cert(ctx, param, val):
    global ca_cert_file
    ca_cert_file = val


ca_cert_option = click.option(
    "--ca-cert",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="PEM proxy CA certificate (defaults to ~/.mitmproxy/mitmproxy-ca-cert.pem).",
    callback=_ca_cert,
    expose_value=False,
)


def _device(ctx, param, val):
    if val:
        set_device(val)


device_option = click.option(
    "-d",
    "--device",
    help="Device serial number to use when multiple devices are connected.",
    callback=_device,
    expose_value=False,
)


@cli.command("all")
@verbosity_option
@force_option
@listen_option
@ca_cert_option
@device_option
@click.argument(
    "apk-files",
    type=click.Path(path_type=Path, exists=True),
    nargs=-1,
    required=True,
)
def all_cmd(apk_files: list[Path]) -> None:
    """
    Patch, install, and start APKs from one or more packages.

    Accepts APK files, extracted directories, XAPK/APKM archives, and APKM ZIPs.
    """
    packages = group_apks_by_package(process_package_inputs(apk_files))
    if not packages:
        raise ValueError("No APK files provided")
    patched = {name: patch_apk_files(apks) for name, apks in packages.items()}
    copy_files()
    for package_name, apks in patched.items():
        logging.info(f"Target: {package_name}")
        install_apk(apks)
        start_app_on_device(package_name)
    logging.info("All done.")


@cli.command("install")
@verbosity_option
@force_option
@device_option
@click.argument(
    "apk-files",
    type=click.Path(path_type=Path, exists=True),
    nargs=-1,
    required=True,
)
def install_cmd(apk_files: list[Path]) -> None:
    """
    Install APKs from one or more packages on the device.
    """
    for apks in group_apks_by_package(process_package_inputs(apk_files)).values():
        install_apk(apks)
    logging.info("All done.")


@cli.command()
@verbosity_option
@force_option
@click.argument(
    "apks",
    type=click.Path(path_type=Path, exists=True),
    nargs=-1,
    required=True,
)
def patch_apks(apks: list[Path]) -> None:
    """Patch an APK file to be debuggable."""
    apks = process_package_inputs(apks)
    patch_apk_files(apks)
    logging.info("All done.")


@cli.command()
@verbosity_option
@force_option
@listen_option
@ca_cert_option
@device_option
def push_resources() -> None:
    """Copy Frida gadget and scripts to device."""
    copy_files()
    logging.info("All done.")


@cli.command()
@verbosity_option
@force_option
@device_option
@click.argument("package-name")
def start_app(package_name: str) -> None:
    """Start app on device and inject Frida gadget."""
    start_app_on_device(package_name)
    logging.info("All done.")


@cli.command()
@verbosity_option
@device_option
def list_packages() -> None:
    """List all packages installed on the device."""
    ensure_device_connected()
    logging.info("Enumerating packages...")
    print("\n".join(get_packages()))
    logging.info("All done.")


@cli.command()
@click.argument("apk-file", type=click.Path(path_type=Path, exists=True))
def package_name(apk_file: Path) -> None:
    """Get the package name for a local APK file."""
    print(build_tools.package_name(apk_file))


@cli.command()
@verbosity_option
@force_option
@device_option
@click.argument("package", type=str)
@click.argument("outdir", type=click.Path(path_type=Path, file_okay=False))
def get_apks(package: str, outdir: Path) -> None:
    """Get all APKs for a specific package from the device."""
    ensure_device_connected()

    logging.info("Getting package info...")
    if package not in get_packages():
        raise RuntimeError(f"Could not find package: {package}")

    package_info = adb(f"shell pm path {package}").stdout
    if not package_info.startswith("package:"):
        raise RuntimeError(f"Unxepected output from pm path: {package_info!r}")
    apks = [p.removeprefix("package:") for p in package_info.splitlines()]
    if not outdir.exists():
        outdir.mkdir()
    for apk in apks:
        logging.info(f"Getting {apk}...")
        outfile = outdir / Path(apk).name
        if outfile.exists():
            if force or click.confirm(
                f"Overwrite existing file: {outfile.absolute()}?", abort=True
            ):
                outfile.unlink()
        adb(f"pull {apk} {outfile.absolute()}")

    logging.info("All done.")


if __name__ == "__main__":
    cli()
