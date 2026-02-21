"""Main entry point for the Camera Transfer & Checksum app.

Provides both a CLI interface (via Click) and voice-controlled interactive mode.
"""

import sys
import threading
import time
from pathlib import Path
from typing import Optional

import click
from rich.console import Console

from .volumes import (
    Volume,
    VolumeType,
    get_mounted_volumes,
    get_camera_volumes,
    get_destination_volumes,
    get_sound_volumes,
    get_media_volumes,
    VolumeWatcher,
    scan_projects,
    create_project,
)
from .camera_cards import parse_camera_card, parse_sound_card, CameraCard, SoundCard
from .checksum import HashAlgorithm
from .transfer import (
    TransferJob,
    TransferStatus,
    prepare_transfer_job,
    prepare_sound_transfer_job,
    execute_transfer,
    save_transfer_report,
)
from .voice import VoiceInterface, VoiceCommand, SPEECH_AVAILABLE
from .ui import (
    console,
    display_volumes,
    display_camera_card,
    display_sound_card,
    display_transfer_summary,
    display_verification_result,
    display_help,
    display_banner,
    prompt_select_volume,
    prompt_select_project,
    prompt_project_name,
    prompt_volume_title,
    prompt_confirm,
    create_transfer_progress,
    display_project_created,
)


@click.group(invoke_without_command=True)
@click.option("--voice/--no-voice", default=False, help="Enable voice control mode.")
@click.option(
    "--algorithm",
    type=click.Choice(["xxhash", "md5"]),
    default="xxhash",
    help="Checksum algorithm.",
)
@click.pass_context
def cli(ctx, voice: bool, algorithm: str):
    """Camera Transfer — transfer camera footage with checksum verification."""
    ctx.ensure_object(dict)
    ctx.obj["algorithm"] = HashAlgorithm.XXHASH if algorithm == "xxhash" else HashAlgorithm.MD5
    ctx.obj["voice"] = voice

    if ctx.invoked_subcommand is None:
        interactive_mode(voice=voice, algorithm=ctx.obj["algorithm"])


@cli.command()
def volumes():
    """List all mounted volumes."""
    vols = get_mounted_volumes()
    if not vols:
        console.print("[yellow]No volumes found under /Volumes[/yellow]")
        return
    display_volumes(vols)


@cli.command()
def cameras():
    """List detected camera cards."""
    cam_vols = get_camera_volumes()
    if not cam_vols:
        console.print("[yellow]No camera cards detected[/yellow]")
        return

    for vol in cam_vols:
        card = parse_camera_card(vol.mount_point, vol.volume_type)
        display_camera_card(card)


@cli.command()
@click.argument("source", type=click.Path(exists=True))
@click.argument("destination", type=click.Path(exists=True))
@click.option("--title", "-t", default=None, help="Folder name for the footage within the project.")
@click.option("--project", "-p", default=None, help="Project name on the destination volume.")
@click.option("--new-project", is_flag=True, default=False, help="Create a new project folder structure.")
@click.option(
    "--algorithm",
    type=click.Choice(["xxhash", "md5"]),
    default="xxhash",
    help="Checksum algorithm.",
)
def transfer(source: str, destination: str, title: str, project: str, new_project: bool, algorithm: str):
    """Transfer footage or sound from SOURCE card to DESTINATION volume."""
    algo = HashAlgorithm.XXHASH if algorithm == "xxhash" else HashAlgorithm.MD5
    source_path = Path(source)
    dest_path = Path(destination)

    # Detect source type (camera or sound)
    from .volumes import _classify_volume, _classify_sound_device
    vol_type = _classify_volume(source_path)

    is_sound = vol_type in (
        VolumeType.SOUND_DEVICES, VolumeType.ZOOM_RECORDER,
        VolumeType.TASCAM_RECORDER, VolumeType.SOUND_RECORDER,
    )

    if not is_sound and vol_type in (VolumeType.GENERIC_STORAGE, VolumeType.UNKNOWN):
        console.print(f"[red]Could not identify {source_path.name} as a camera card or sound device.[/red]")
        console.print("Supported: RED, ARRI cameras and Sound Devices, Zoom, Tascam recorders")
        return

    if is_sound:
        scard = parse_sound_card(source_path, vol_type)
        display_sound_card(scard)
        total_files = scard.total_files
        total_gb = scard.total_gb
        target_subfolder = "SOUND"
    else:
        card = parse_camera_card(source_path, vol_type)
        display_camera_card(card)
        total_files = card.total_files
        total_gb = card.total_gb
        target_subfolder = "FOOTAGE"

    # Project selection
    if project is None:
        projects = scan_projects(dest_path)
        selection = prompt_select_project(projects, dest_path.name)
        if selection is None:
            console.print("[yellow]Transfer cancelled.[/yellow]")
            return
        if selection == "__NEW__":
            new_project = True
            project = prompt_project_name()
            if not project:
                console.print("[yellow]Transfer cancelled — no project name.[/yellow]")
                return
        else:
            project = selection

    if new_project:
        proj = create_project(dest_path, project)
        display_project_created(proj)

    # Title defaults to source volume name
    if title is None:
        suggested = source_path.name
        console.print(f"Files go to: {project}/{target_subfolder}/{suggested}")
        title = prompt_volume_title(suggested=suggested)
        if not title:
            console.print("[yellow]Transfer cancelled — no title.[/yellow]")
            return

    dest_root = dest_path / project / target_subfolder
    dest_display = f"{dest_path.name}/{project}/{target_subfolder}/{title}"

    media_label = "sound files" if is_sound else "files"
    if not prompt_confirm(f"Transfer {total_files} {media_label} ({total_gb:.2f} GB) to {dest_display}?"):
        console.print("[yellow]Transfer cancelled.[/yellow]")
        return

    if is_sound:
        _run_sound_transfer(scard, dest_root, title, algo)
    else:
        _run_transfer(card, dest_root, title, algo)


@cli.command()
@click.argument("report_path", type=click.Path(exists=True))
def verify(report_path: str):
    """Re-verify a previous transfer using its report file."""
    import json

    report_file = Path(report_path)
    with open(report_file) as f:
        report = json.load(f)

    console.print(f"[bold]Re-verifying transfer: {report['volume_title']}[/bold]")
    console.print(f"Files: {report['total_files']}  |  Algorithm: {report['algorithm']}")

    algo = HashAlgorithm.XXHASH if report["algorithm"] == "xxh3_128" else HashAlgorithm.MD5
    from .checksum import verify_transfer as verify_pair

    passed = 0
    failed = 0
    total = len(report["files"])

    for i, entry in enumerate(report["files"], 1):
        src = Path(entry["source"])
        dst = Path(entry["destination"])

        if not dst.exists():
            console.print(f"  [red]✗[/red] [{i}/{total}] {entry['relative_path']} — destination missing")
            failed += 1
            continue

        src_result, dst_result, match = verify_pair(src, dst, algo)
        if match:
            console.print(
                f"  [green]✓[/green] [{i}/{total}] {entry['relative_path']} "
                f"[dim]({src_result.hex_digest[:16]}...)[/dim]"
            )
            passed += 1
        else:
            console.print(f"  [red]✗[/red] [{i}/{total}] {entry['relative_path']} — MISMATCH")
            failed += 1

    console.print()
    if failed == 0:
        console.print(f"[bold green]All {passed} files verified successfully.[/bold green]")
    else:
        console.print(f"[bold red]{failed} file(s) failed verification out of {total}.[/bold red]")


def _run_transfer(card: CameraCard, dest_root: Path, title: str, algorithm: HashAlgorithm):
    """Execute a transfer with progress display."""
    job = prepare_transfer_job(card, dest_root, title, algorithm)

    console.print(f"\n[bold]Starting transfer...[/bold]")
    console.print(f"  Source: {card.volume_name} ({card.camera_type.value})")
    console.print(f"  Destination: {dest_root / title}")
    console.print(f"  Files: {job.total_files}  |  Size: {job.total_bytes / (1024**3):.2f} GB")
    console.print(f"  Checksum: {algorithm.value}")
    console.print()

    progress = create_transfer_progress()

    with progress:
        # Phase 1: Copy
        copy_task = progress.add_task("Copying...", total=job.total_files, filename="")

        def on_file_start(record, idx, total):
            progress.update(copy_task, filename=record.relative_path)

        def on_file_complete(record, idx, total):
            progress.advance(copy_task)

        # Phase 2: Verify
        verify_task = progress.add_task("Verifying...", total=job.total_files, filename="", visible=False)

        def on_verify_start(record, idx, total):
            if not progress.tasks[verify_task].visible:
                progress.update(verify_task, visible=True)
            progress.update(verify_task, filename=record.relative_path)

        def on_verify_complete(record, idx, total, match):
            progress.advance(verify_task)

        execute_transfer(
            job,
            on_file_start=on_file_start,
            on_file_complete=on_file_complete,
            on_verify_start=on_verify_start,
            on_verify_complete=on_verify_complete,
        )

    display_transfer_summary(job)

    # Save report
    report_path = save_transfer_report(job)
    console.print(f"\n[dim]Report saved: {report_path}[/dim]")

    return job


def _run_sound_transfer(card: SoundCard, dest_root: Path, title: str, algorithm: HashAlgorithm):
    """Execute a sound transfer with progress display."""
    job = prepare_sound_transfer_job(card, dest_root, title, algorithm)

    console.print(f"\n[bold]Starting sound transfer...[/bold]")
    console.print(f"  Source: {card.volume_name} ({card.recorder_type.value})")
    console.print(f"  Destination: {dest_root / title}")
    console.print(f"  Files: {job.total_files}  |  Size: {job.total_bytes / (1024**3):.2f} GB")
    console.print(f"  Checksum: {algorithm.value}")
    console.print()

    progress = create_transfer_progress()

    with progress:
        copy_task = progress.add_task("Copying...", total=job.total_files, filename="")

        def on_file_start(record, idx, total):
            progress.update(copy_task, filename=record.relative_path)

        def on_file_complete(record, idx, total):
            progress.advance(copy_task)

        verify_task = progress.add_task("Verifying...", total=job.total_files, filename="", visible=False)

        def on_verify_start(record, idx, total):
            if not progress.tasks[verify_task].visible:
                progress.update(verify_task, visible=True)
            progress.update(verify_task, filename=record.relative_path)

        def on_verify_complete(record, idx, total, match):
            progress.advance(verify_task)

        execute_transfer(
            job,
            on_file_start=on_file_start,
            on_file_complete=on_file_complete,
            on_verify_start=on_verify_start,
            on_verify_complete=on_verify_complete,
        )

    display_transfer_summary(job)

    report_path = save_transfer_report(job)
    console.print(f"\n[dim]Report saved: {report_path}[/dim]")

    return job


# ---------- Interactive / Voice mode ----------

def interactive_mode(voice: bool = False, algorithm: HashAlgorithm = HashAlgorithm.XXHASH):
    """Run the app in interactive mode with optional voice control."""
    display_banner()

    voice_interface = None
    if voice and SPEECH_AVAILABLE:
        voice_interface = VoiceInterface()
        console.print("[bold]Voice mode enabled.[/bold] Requesting microphone access...")

        auth_event = threading.Event()
        auth_result = [False]

        def on_auth(authorized):
            auth_result[0] = authorized
            auth_event.set()

        voice_interface.request_authorization(on_auth)
        auth_event.wait(timeout=10)

        if auth_result[0]:
            voice_interface.start_listening()
            console.print("[green]Listening for voice commands...[/green]")
            voice_interface.speak("Camera transfer ready. Say a command or type one below.")
        else:
            console.print("[yellow]Speech recognition not authorized. Using keyboard input.[/yellow]")
            voice_interface = None
    elif voice and not SPEECH_AVAILABLE:
        console.print("[yellow]Speech framework not available (requires macOS + PyObjC). Using keyboard.[/yellow]")

    display_help()
    console.print()

    current_job: Optional[TransferJob] = None

    while True:
        # Get command from voice or keyboard
        cmd = None
        if voice_interface and voice_interface.is_listening:
            cmd = voice_interface.get_command(timeout=0.3)

        if cmd is None:
            # Fall back to keyboard
            try:
                cmd = VoiceCommand(input("\n[command]> "))
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Goodbye.[/dim]")
                break

        if cmd.command == "quit":
            if voice_interface:
                voice_interface.speak("Goodbye.")
                voice_interface.stop_listening()
            console.print("[dim]Goodbye.[/dim]")
            break

        elif cmd.command == "help":
            display_help()

        elif cmd.command == "list_volumes":
            vols = get_mounted_volumes()
            if vols:
                display_volumes(vols)
            else:
                _say(voice_interface, "No volumes found.")

        elif cmd.command == "list_cameras":
            cam_vols = get_camera_volumes()
            if cam_vols:
                for vol in cam_vols:
                    card = parse_camera_card(vol.mount_point, vol.volume_type)
                    display_camera_card(card)
                _say(voice_interface, f"Found {len(cam_vols)} camera card(s).")
            else:
                _say(voice_interface, "No camera cards detected. Plug in a camera card.")

        elif cmd.command == "transfer":
            _handle_transfer_command(voice_interface, algorithm)

        elif cmd.command == "verify":
            _handle_verify_command(voice_interface)

        elif cmd.command == "status":
            if current_job:
                pct = current_job.progress * 100
                _say(voice_interface, f"Transfer is {pct:.0f}% complete.")
            else:
                _say(voice_interface, "No transfer in progress.")

        elif cmd.command == "stop":
            _say(voice_interface, "Cancel not yet implemented for running transfers.")

        elif cmd.command == "unknown":
            _say(voice_interface, f"I didn't understand: '{cmd.raw_text}'. Say 'help' for commands.")

        else:
            _say(voice_interface, f"Command '{cmd.command}' noted. Context: {cmd.raw_text}")


def _handle_transfer_command(voice_interface: Optional[VoiceInterface], algorithm: HashAlgorithm):
    """Handle the interactive transfer flow for both camera and sound sources."""
    # Step 1: Select source — camera cards AND sound devices
    media_vols = get_media_volumes()
    if not media_vols:
        _say(voice_interface, "No camera cards or sound devices found. Please insert media.")
        return

    _say(voice_interface, f"Found {len(media_vols)} media source(s). Select the source.")
    source_vol = prompt_select_volume(media_vols, "Select source (camera or sound)")
    if not source_vol:
        _say(voice_interface, "Transfer cancelled.")
        return

    # Determine if this is a sound device or camera card
    is_sound = source_vol.is_sound_device

    if is_sound:
        sound_card = parse_sound_card(source_vol.mount_point, source_vol.volume_type)
        display_sound_card(sound_card)
        total_files = sound_card.total_files
        total_gb = sound_card.total_gb
        source_name = sound_card.volume_name
        target_subfolder = "SOUND"
    else:
        card = parse_camera_card(source_vol.mount_point, source_vol.volume_type)
        display_camera_card(card)
        total_files = card.total_files
        total_gb = card.total_gb
        source_name = card.volume_name
        target_subfolder = "FOOTAGE"

    # Step 2: Select destination volume
    dest_vols = get_destination_volumes()
    if not dest_vols:
        _say(voice_interface, "No destination volumes found. Please connect a storage drive.")
        return

    _say(voice_interface, "Select the destination volume.")
    dest_vol = prompt_select_volume(dest_vols, "Select destination volume")
    if not dest_vol:
        _say(voice_interface, "Transfer cancelled.")
        return

    # Step 3: Select or create a project on the destination volume
    projects = scan_projects(dest_vol.mount_point)

    if projects:
        _say(voice_interface, f"Found {len(projects)} project(s) on {dest_vol.name}. Select one or create new.")
    else:
        _say(voice_interface, f"No projects found on {dest_vol.name}. Let's create one.")

    selection = prompt_select_project(projects, dest_vol.name)

    if selection is None:
        _say(voice_interface, "Transfer cancelled.")
        return

    if selection == "__NEW__":
        project_name = prompt_project_name()
        if not project_name:
            _say(voice_interface, "Transfer cancelled — no project name provided.")
            return
        project = create_project(dest_vol.mount_point, project_name)
        display_project_created(project)
        _say(voice_interface, f"Created project '{project_name}' with standard folder structure.")
    else:
        project_name = selection
        _say(voice_interface, f"Using existing project: {project_name}")

    # Step 4: Get a title for the subfolder
    suggested_title = source_vol.name
    _say(voice_interface, f"Files will be placed in {project_name}/{target_subfolder}/{suggested_title}")
    title = prompt_volume_title(suggested=suggested_title)

    if not title:
        _say(voice_interface, "Transfer cancelled — no title provided.")
        return

    # The destination root is the project's FOOTAGE or SOUND folder
    dest_root = dest_vol.mount_point / project_name / target_subfolder

    # Step 5: Confirm
    dest_display = f"{dest_vol.name}/{project_name}/{target_subfolder}/{title}"
    media_label = "sound files" if is_sound else "files"
    msg = f"Transfer {total_files} {media_label} ({total_gb:.2f} GB) from {source_name} to {dest_display}?"
    _say(voice_interface, msg)

    if not prompt_confirm("Proceed?"):
        _say(voice_interface, "Transfer cancelled.")
        return

    # Step 6: Execute
    _say(voice_interface, "Starting transfer.")

    if is_sound:
        job = _run_sound_transfer(sound_card, dest_root, title, algorithm)
    else:
        job = _run_transfer(card, dest_root, title, algorithm)

    if job.status == TransferStatus.VERIFIED:
        _say(voice_interface, f"Transfer complete. All {job.verified_count} files verified.")
    else:
        _say(voice_interface, f"Transfer finished with {job.failed_count} failures. Check the report.")


def _handle_verify_command(voice_interface: Optional[VoiceInterface]):
    """Handle re-verification of a previous transfer."""
    _say(voice_interface, "Enter the path to a transfer report JSON file.")
    try:
        path_str = input("Report file path: ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    report_path = Path(path_str)
    if not report_path.exists():
        _say(voice_interface, f"File not found: {report_path}")
        return

    # Reuse the verify CLI command logic
    import json
    from .checksum import verify_transfer as verify_pair

    with open(report_path) as f:
        report = json.load(f)

    algo = HashAlgorithm.XXHASH if report["algorithm"] == "xxh3_128" else HashAlgorithm.MD5

    console.print(f"[bold]Re-verifying: {report['volume_title']}[/bold]")
    _say(voice_interface, f"Verifying {report['total_files']} files.")

    passed = 0
    failed = 0
    total = len(report["files"])

    for i, entry in enumerate(report["files"], 1):
        dst = Path(entry["destination"])
        src = Path(entry["source"])

        if not dst.exists():
            console.print(f"  [red]✗[/red] [{i}/{total}] {entry['relative_path']} — missing")
            failed += 1
            continue

        src_result, dst_result, match = verify_pair(src, dst, algo)
        if match:
            console.print(f"  [green]✓[/green] [{i}/{total}] {entry['relative_path']}")
            passed += 1
        else:
            console.print(f"  [red]✗[/red] [{i}/{total}] {entry['relative_path']} — MISMATCH")
            failed += 1

    if failed == 0:
        _say(voice_interface, f"All {passed} files verified successfully.")
    else:
        _say(voice_interface, f"{failed} of {total} files failed verification.")


def _say(voice_interface: Optional[VoiceInterface], text: str):
    """Speak and print a message."""
    if voice_interface:
        voice_interface.speak(text)
    else:
        console.print(f"  {text}")


if __name__ == "__main__":
    cli()
