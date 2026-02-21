"""Rich-based terminal UI for the camera transfer app.

Provides formatted output for volume listings, transfer progress,
checksum verification results, and interactive prompts.
"""

from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    FileSizeColumn,
    TransferSpeedColumn,
    TimeRemainingColumn,
    TaskID,
)
from rich.text import Text
from rich.columns import Columns
from rich import box

from .volumes import Volume, VolumeType, Project, PROJECT_SUBFOLDERS
from .camera_cards import CameraCard, SoundCard
from .transfer import TransferJob, TransferStatus, FileTransferRecord

console = Console()


# ---------- Volume display ----------

CAMERA_LABELS = {
    VolumeType.RED_KOMODO: "RED Komodo",
    VolumeType.RED_DSMC2: "RED DSMC2",
    VolumeType.ARRI_ALEXA_MINI: "ARRI ALEXA Mini/LF",
    VolumeType.ARRI_ALEXA35: "ARRI ALEXA 35",
    VolumeType.ARRI_AMIRA: "ARRI AMIRA",
    VolumeType.SOUND_DEVICES: "Sound Devices",
    VolumeType.ZOOM_RECORDER: "Zoom Recorder",
    VolumeType.TASCAM_RECORDER: "Tascam Recorder",
    VolumeType.SOUND_RECORDER: "Sound Recorder",
    VolumeType.GENERIC_STORAGE: "Storage",
    VolumeType.UNKNOWN: "Unknown",
}


def display_volumes(volumes: list[Volume], title: str = "Mounted Volumes"):
    """Display a table of mounted volumes."""
    table = Table(title=title, box=box.ROUNDED, show_lines=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("Name", style="bold cyan")
    table.add_column("Type", style="magenta")
    table.add_column("Used", justify="right")
    table.add_column("Free", justify="right", style="green")
    table.add_column("Total", justify="right")
    table.add_column("Mount Point", style="dim")

    for i, vol in enumerate(volumes, 1):
        vol_label = CAMERA_LABELS.get(vol.volume_type, vol.volume_type.value)
        if vol.is_camera_card:
            style = "bold yellow"
        elif vol.is_sound_device:
            style = "bold green"
        else:
            style = ""

        table.add_row(
            str(i),
            vol.name,
            vol_label,
            f"{vol.used_gb:.1f} GB",
            f"{vol.free_gb:.1f} GB",
            f"{vol.total_gb:.1f} GB",
            str(vol.mount_point),
            style=style,
        )

    console.print(table)


def display_camera_card(card: CameraCard):
    """Display detailed info about a camera card."""
    vol_label = CAMERA_LABELS.get(card.camera_type, card.camera_type.value)

    console.print(Panel(
        f"[bold]{card.volume_name}[/bold] — {vol_label}\n"
        f"Rolls: {len(card.rolls)}  |  "
        f"Files: {card.total_files}  |  "
        f"Size: {card.total_gb:.2f} GB",
        title="Camera Card",
        border_style="yellow",
    ))

    if card.rolls:
        table = Table(box=box.SIMPLE)
        table.add_column("Roll", style="bold")
        table.add_column("Files", justify="right")
        table.add_column("Size", justify="right")

        for roll in card.rolls:
            table.add_row(
                roll.name,
                str(roll.file_count),
                f"{roll.total_gb:.2f} GB",
            )

        console.print(table)


def display_sound_card(card: SoundCard):
    """Display detailed info about a sound recorder card."""
    vol_label = CAMERA_LABELS.get(card.recorder_type, card.recorder_type.value)

    console.print(Panel(
        f"[bold]{card.volume_name}[/bold] — {vol_label}\n"
        f"Sessions: {len(card.rolls)}  |  "
        f"Files: {card.total_files}  |  "
        f"Size: {card.total_gb:.2f} GB",
        title="Sound Card",
        border_style="green",
    ))

    if card.rolls:
        table = Table(box=box.SIMPLE)
        table.add_column("Session/Folder", style="bold")
        table.add_column("Files", justify="right")
        table.add_column("Size", justify="right")
        table.add_column("Formats", style="dim")

        for roll in card.rolls:
            formats = ", ".join(sorted(set(
                f.format.value.upper() for f in roll.files
            )))
            table.add_row(
                roll.name,
                str(roll.file_count),
                f"{roll.total_gb:.2f} GB",
                formats,
            )

        console.print(table)


# ---------- Transfer progress ----------

def create_transfer_progress() -> Progress:
    """Create a Rich progress bar for file transfers."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.fields[filename]}"),
        BarColumn(bar_width=40),
        FileSizeColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    )


def display_transfer_summary(job: TransferJob):
    """Display a summary after transfer completes."""
    duration = job.end_time - job.start_time if job.end_time and job.start_time else 0

    if job.status == TransferStatus.VERIFIED:
        status_text = "[bold green]ALL FILES VERIFIED[/bold green]"
    elif job.status == TransferStatus.FAILED:
        status_text = f"[bold red]FAILED — {job.failed_count} file(s) failed[/bold red]"
    else:
        status_text = f"[yellow]{job.status.value}[/yellow]"

    total_mb = job.total_bytes / (1024 * 1024)
    avg_speed = total_mb / duration if duration > 0 else 0

    console.print()
    console.print(Panel(
        f"Source: [cyan]{job.card.volume_name}[/cyan]\n"
        f"Destination: [cyan]{job.destination_root / job.volume_title}[/cyan]\n"
        f"Status: {status_text}\n"
        f"\n"
        f"Files: {job.verified_count}/{job.total_files} verified\n"
        f"Data: {job.total_bytes / (1024**3):.2f} GB\n"
        f"Duration: {duration:.1f}s\n"
        f"Avg Speed: {avg_speed:.1f} MB/s",
        title="Transfer Complete",
        border_style="green" if job.status == TransferStatus.VERIFIED else "red",
    ))

    # Show failed files if any
    if job.failed_count > 0:
        console.print()
        console.print("[bold red]Failed files:[/bold red]")
        for record in job.records:
            if record.status == TransferStatus.FAILED:
                console.print(f"  [red]✗[/red] {record.relative_path}: {record.error}")


def display_verification_result(record: FileTransferRecord, index: int, total: int):
    """Display a single file verification result."""
    if record.checksums_match:
        console.print(
            f"  [green]✓[/green] [{index}/{total}] {record.relative_path} "
            f"[dim]({record.source_checksum[:16]}...)[/dim]"
        )
    else:
        console.print(
            f"  [red]✗[/red] [{index}/{total}] {record.relative_path} "
            f"[red]MISMATCH[/red]"
        )


# ---------- Prompts ----------

def prompt_select_volume(volumes: list[Volume], prompt_text: str = "Select volume") -> Optional[Volume]:
    """Prompt user to select a volume by number."""
    display_volumes(volumes, title=prompt_text)

    while True:
        try:
            choice = input(f"\nEnter number (1-{len(volumes)}), or 'q' to cancel: ").strip()
            if choice.lower() == 'q':
                return None
            idx = int(choice) - 1
            if 0 <= idx < len(volumes):
                return volumes[idx]
            console.print("[red]Invalid selection[/red]")
        except (ValueError, EOFError, KeyboardInterrupt):
            return None


def prompt_volume_title(suggested: str = "") -> str:
    """Prompt user for a volume/project title for the destination folder."""
    default_msg = f" [{suggested}]" if suggested else ""
    try:
        title = input(f"Enter volume/project title{default_msg}: ").strip()
        return title or suggested
    except (EOFError, KeyboardInterrupt):
        return suggested


def prompt_select_project(projects: list[Project], volume_name: str) -> Optional[str]:
    """Prompt user to select an existing project or create a new one.

    Returns:
        Project name (existing or new), or None if cancelled.
        Returns the string "__NEW__" sentinel if user chose to create new.
    """
    console.print()
    console.print(f"[bold]Projects on {volume_name}:[/bold]")

    if projects:
        table = Table(box=box.ROUNDED, show_lines=True)
        table.add_column("#", style="dim", width=3)
        table.add_column("Project", style="bold cyan")
        table.add_column("Folders", style="green")
        table.add_column("Status", style="dim")

        for i, proj in enumerate(projects, 1):
            folders = ", ".join(proj.existing_subfolders)
            status = "[green]Complete[/green]" if proj.is_complete else "[yellow]Partial[/yellow]"
            table.add_row(str(i), proj.name, folders, status)

        # Add "Create New" option
        table.add_row(
            str(len(projects) + 1),
            "[bold magenta]+ Create New Project[/bold magenta]",
            "",
            "",
        )
        console.print(table)
    else:
        console.print("  [dim]No existing projects found.[/dim]")
        console.print("  [bold magenta]1. + Create New Project[/bold magenta]")

    total_options = len(projects) + 1

    while True:
        try:
            choice = input(f"\nSelect project (1-{total_options}), or 'q' to cancel: ").strip()
            if choice.lower() == 'q':
                return None
            idx = int(choice) - 1
            if idx == len(projects):
                # "Create New" selected
                return "__NEW__"
            if 0 <= idx < len(projects):
                return projects[idx].name
            console.print("[red]Invalid selection[/red]")
        except (ValueError, EOFError, KeyboardInterrupt):
            return None


def prompt_project_name() -> str:
    """Prompt user for a new project/brand name."""
    try:
        name = input("Enter project/brand name: ").strip()
        return name
    except (EOFError, KeyboardInterrupt):
        return ""


def display_project_created(project: Project):
    """Display confirmation of a newly created project folder structure."""
    folder_tree = "\n".join(f"    {sub}/" for sub in PROJECT_SUBFOLDERS)
    console.print(Panel(
        f"[bold]{project.name}/[/bold]\n{folder_tree}",
        title="Project Created",
        border_style="green",
    ))


def prompt_confirm(message: str) -> bool:
    """Prompt for yes/no confirmation."""
    try:
        response = input(f"{message} [y/N]: ").strip().lower()
        return response in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


# ---------- Help ----------

def display_help():
    """Display available commands."""
    help_table = Table(title="Available Commands", box=box.ROUNDED)
    help_table.add_column("Command", style="bold cyan")
    help_table.add_column("Description")

    commands = [
        ("transfer / copy", "Transfer footage from a camera card to a destination volume"),
        ("list volumes / show drives", "Show all mounted volumes"),
        ("list cameras / show cards", "Show detected camera cards"),
        ("verify / check", "Verify a previous transfer with checksums"),
        ("status / progress", "Show current transfer status"),
        ("stop / cancel", "Cancel the current transfer"),
        ("help", "Show this help message"),
        ("quit / exit", "Exit the application"),
    ]

    for cmd, desc in commands:
        help_table.add_row(cmd, desc)

    console.print(help_table)
    console.print("\n[dim]You can type these commands or speak them if voice mode is active.[/dim]")


def display_banner():
    """Display the app banner on startup."""
    console.print(Panel(
        "[bold]Camera Transfer & Checksum Verification[/bold]\n"
        "[dim]Transfer footage from RED and ARRI camera cards\n"
        "with checksum verification and voice control[/dim]",
        border_style="blue",
        padding=(1, 2),
    ))
