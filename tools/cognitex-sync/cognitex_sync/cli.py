"""CLI interface for cognitex-sync."""

import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config, save_config, SyncConfig, get_config_path, get_state_path, load_state
from .sync import SyncClient

app = typer.Typer(
    name="cognitex-sync",
    help="Sync coding CLI sessions to Cognitex knowledge graph",
    add_completion=False,
)
console = Console()


@app.command()
def configure(
    server: str = typer.Option(None, "--server", "-s", help="Cognitex server URL"),
    api_key: str = typer.Option(None, "--api-key", "-k", help="API key for authentication"),
    machine_id: str = typer.Option(None, "--machine-id", "-m", help="Unique ID for this machine"),
) -> None:
    """Configure cognitex-sync with server details."""
    config = load_config()

    if server:
        # Normalize URL
        server = server.rstrip("/")
        if not server.startswith("http"):
            server = f"https://{server}"
        config.server_url = server
        console.print(f"[green]✓[/green] Server URL set to: {server}")

    if api_key:
        config.api_key = api_key
        console.print("[green]✓[/green] API key configured")

    if machine_id:
        config.machine_id = machine_id
        console.print(f"[green]✓[/green] Machine ID set to: {machine_id}")

    if server or api_key or machine_id:
        save_config(config)
        console.print(f"\n[dim]Config saved to: {get_config_path()}[/dim]")
    else:
        # Show current config
        console.print("\n[bold]Current Configuration[/bold]\n")
        table = Table()
        table.add_column("Setting", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Server URL", config.server_url or "[dim]not set[/dim]")
        table.add_row("API Key", "****" + config.api_key[-4:] if config.api_key else "[dim]not set[/dim]")
        table.add_row("Machine ID", config.machine_id or "[dim]auto-detected[/dim]")
        table.add_row("Config File", str(get_config_path()))

        console.print(table)

        console.print("\n[dim]Set values with: cognitex-sync configure --server URL --api-key KEY[/dim]")


@app.command()
def status() -> None:
    """Check connection status and show sync info."""
    config = load_config()
    client = SyncClient(config)

    console.print("\n[bold]Cognitex Sync Status[/bold]\n")

    # Check configuration
    if not config.server_url:
        console.print("[red]✗[/red] Server URL not configured")
        console.print("[dim]Run: cognitex-sync configure --server URL[/dim]")
        return

    if not config.api_key:
        console.print("[red]✗[/red] API key not configured")
        console.print("[dim]Run: cognitex-sync configure --api-key KEY[/dim]")
        return

    console.print(f"[green]✓[/green] Server: {config.server_url}")
    console.print(f"[green]✓[/green] Machine ID: {config.machine_id}")

    # Check connection
    console.print("\n[dim]Checking connection...[/dim]")
    result = client.check_connection()

    if result["status"] == "ok":
        console.print(f"[green]✓[/green] Connected to server (v{result.get('version', 'unknown')})")
        console.print(f"[green]✓[/green] Total sessions on server: {result.get('total_sessions', 0)}")
    else:
        console.print(f"[red]✗[/red] Connection failed: {result['message']}")
        return

    # Show local state
    state = load_state()
    synced_count = len(state.get("synced_sessions", {}))
    last_sync = state.get("last_sync", "never")

    console.print(f"\n[bold]Local State[/bold]")
    console.print(f"  Sessions synced: {synced_count}")
    console.print(f"  Last sync: {last_sync}")

    # Discover sessions
    sessions = client.discovery.discover_sessions("claude")
    console.print(f"\n[bold]Discovered Sessions[/bold]")
    console.print(f"  Claude Code sessions: {len(sessions)}")


@app.command()
def push(
    cli: str = typer.Option("claude", "--cli", "-c", help="CLI type to sync"),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-sync all sessions"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show what would be synced"),
) -> None:
    """Push coding sessions to Cognitex server."""
    config = load_config()

    if not config.server_url or not config.api_key:
        console.print("[red]Error:[/red] Not configured. Run: cognitex-sync configure")
        raise typer.Exit(1)

    client = SyncClient(config)

    console.print(f"\n[bold]Syncing {cli} sessions...[/bold]\n")

    result = client.sync_sessions(cli_type=cli, force=force, dry_run=dry_run)

    if result["status"] == "ok":
        if dry_run:
            console.print(f"[dim]Dry run - would sync {result.get('would_sync', 0)} sessions[/dim]")
            if result.get("sessions"):
                for s in result["sessions"][:10]:
                    console.print(f"  • {s}")
                if len(result["sessions"]) > 10:
                    console.print(f"  ... and {len(result['sessions']) - 10} more")
        else:
            console.print(f"[green]✓[/green] Discovered: {result.get('discovered', 0)} sessions")
            console.print(f"[green]✓[/green] Synced: {result.get('synced', 0)} sessions")
            if result.get("batches", 0) > 1:
                console.print(f"[dim]  (sent in {result['batches']} batches)[/dim]")

            if result.get("errors"):
                console.print(f"\n[yellow]Warnings:[/yellow]")
                for err in result["errors"][:5]:
                    console.print(f"  • {err}")
    else:
        console.print(f"[red]Error:[/red] {result.get('message', 'Unknown error')}")
        if result.get("detail"):
            console.print(f"[dim]{result['detail']}[/dim]")
        if result.get("synced_before_error", 0) > 0:
            console.print(f"[yellow]Note:[/yellow] {result['synced_before_error']} sessions were synced before the error")
        raise typer.Exit(1)


@app.command()
def push_file(
    session_file: str = typer.Argument(..., help="Path to session file to sync"),
    cli: str = typer.Option("claude", "--cli", "-c", help="CLI type"),
) -> None:
    """Push a single session file to Cognitex server (for hook usage)."""
    config = load_config()

    if not config.server_url or not config.api_key:
        console.print("[red]Error:[/red] Not configured. Run: cognitex-sync configure")
        raise typer.Exit(1)

    client = SyncClient(config)
    result = client.sync_single_session(session_file, cli_type=cli)

    if result.get("status") == "ok":
        console.print(f"[green]✓[/green] Synced session to Cognitex")
    else:
        console.print(f"[red]Error:[/red] {result.get('message', 'Unknown error')}")
        raise typer.Exit(1)


@app.command()
def daemon(
    cli: str = typer.Option("claude", "--cli", "-c", help="CLI type to watch"),
    interval: int = typer.Option(30, "--interval", "-i", help="Sync interval in minutes"),
) -> None:
    """Run as a background daemon, syncing sessions periodically."""
    config = load_config()

    if not config.server_url or not config.api_key:
        console.print("[red]Error:[/red] Not configured. Run: cognitex-sync configure")
        raise typer.Exit(1)

    client = SyncClient(config)

    console.print(f"\n[bold]Cognitex Sync Daemon[/bold]")
    console.print(f"  Server: {config.server_url}")
    console.print(f"  Machine: {config.machine_id}")
    console.print(f"  Interval: {interval} minutes")
    console.print(f"\n[dim]Press Ctrl+C to stop[/dim]\n")

    try:
        while True:
            # Sync sessions
            result = client.sync_sessions(cli_type=cli)

            timestamp = time.strftime("%H:%M:%S")
            if result["status"] == "ok":
                synced = result.get("synced", 0)
                if synced > 0:
                    console.print(f"[{timestamp}] Synced {synced} sessions")
                else:
                    console.print(f"[{timestamp}] [dim]No new sessions[/dim]")
            else:
                console.print(f"[{timestamp}] [red]Error:[/red] {result.get('message')}")

            # Wait for next interval
            time.sleep(interval * 60)

    except KeyboardInterrupt:
        console.print("\n[dim]Daemon stopped[/dim]")


@app.command()
def discover(
    cli: str = typer.Option("claude", "--cli", "-c", help="CLI type to scan"),
) -> None:
    """Discover local coding sessions."""
    config = load_config()
    client = SyncClient(config)

    sessions = client.discovery.discover_sessions(cli)

    console.print(f"\n[bold]Discovered {len(sessions)} {cli} sessions[/bold]\n")

    if not sessions:
        console.print("[dim]No sessions found[/dim]")
        return

    table = Table()
    table.add_column("Session ID", style="cyan")
    table.add_column("Project", style="green")
    table.add_column("Modified", style="dim")
    table.add_column("Size", justify="right")

    for s in sessions[:20]:
        table.add_row(
            s["session_id"][:12],
            s["project_path"].split("/")[-1],
            s["modified_at"][:16],
            f"{s['size_bytes'] // 1024}KB",
        )

    console.print(table)

    if len(sessions) > 20:
        console.print(f"\n[dim]... and {len(sessions) - 20} more[/dim]")


@app.command()
def hook_install() -> None:
    """Install Claude Code hook for automatic syncing."""
    hooks_dir = Path.home() / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    hook_file = hooks_dir / "post-session.sh"

    hook_content = '''#!/bin/bash
# Cognitex Sync Hook - Syncs session to Cognitex after each conversation
# Installed by: cognitex-sync hook-install

# Get the session file from environment (if available)
SESSION_FILE="${CLAUDE_SESSION_FILE:-}"

if [ -n "$SESSION_FILE" ] && [ -f "$SESSION_FILE" ]; then
    # Sync in background to not block Claude
    cognitex-sync push-file "$SESSION_FILE" &>/dev/null &
fi
'''

    if hook_file.exists():
        console.print(f"[yellow]Warning:[/yellow] Hook already exists at {hook_file}")
        overwrite = typer.confirm("Overwrite existing hook?")
        if not overwrite:
            return

    with open(hook_file, "w") as f:
        f.write(hook_content)

    # Make executable
    hook_file.chmod(0o755)

    console.print(f"[green]✓[/green] Hook installed at: {hook_file}")
    console.print("\n[dim]Note: Claude Code hook support depends on Claude Code version.[/dim]")
    console.print("[dim]Check Claude Code documentation for hook configuration.[/dim]")


if __name__ == "__main__":
    app()
