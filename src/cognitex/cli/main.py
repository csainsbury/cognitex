"""CLI entry point using Typer."""

import asyncio
import logging
from datetime import datetime, timedelta

import structlog
import typer
from rich.console import Console

logger = structlog.get_logger()
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.prompt import Prompt, Confirm
from rich.table import Table

app = typer.Typer(
    name="cognitex",
    help="Personal agent system for cognitive load management",
    no_args_is_help=True,
)
console = Console()


# =============================================================================
# Interactive Form Helpers
# =============================================================================

def prompt_with_options(prompt_text: str, options: list[tuple[str, str]], allow_empty: bool = True) -> str | None:
    """
    Show numbered options and let user pick one.

    Args:
        prompt_text: The prompt to show
        options: List of (id, display_text) tuples
        allow_empty: If True, allow pressing Enter to skip

    Returns:
        Selected ID or None if skipped
    """
    if not options:
        console.print(f"[dim]  (no options available)[/dim]")
        return None

    for i, (opt_id, display) in enumerate(options, 1):
        console.print(f"  [cyan]{i}[/cyan]. {display} [dim]({opt_id[:12]}...)[/dim]")

    skip_hint = " or Enter to skip" if allow_empty else ""
    choice = Prompt.ask(f"{prompt_text} [dim](1-{len(options)}{skip_hint})[/dim]", default="")

    if not choice:
        return None

    try:
        idx = int(choice) - 1
        if 0 <= idx < len(options):
            return options[idx][0]
    except ValueError:
        # Try matching by partial ID
        for opt_id, _ in options:
            if choice in opt_id:
                return opt_id

    return None


def prompt_with_multi_options(prompt_text: str, options: list[tuple[str, str]]) -> list[str]:
    """
    Show numbered options and let user pick multiple (comma-separated).

    Returns:
        List of selected IDs
    """
    if not options:
        console.print(f"[dim]  (no options available)[/dim]")
        return []

    for i, (opt_id, display) in enumerate(options, 1):
        console.print(f"  [cyan]{i}[/cyan]. {display}")

    choice = Prompt.ask(f"{prompt_text} [dim](e.g., 1,3,4 or Enter to skip)[/dim]", default="")

    if not choice:
        return []

    selected = []
    for part in choice.split(","):
        part = part.strip()
        try:
            idx = int(part) - 1
            if 0 <= idx < len(options):
                selected.append(options[idx][0])
        except ValueError:
            pass

    return selected


@app.callback()
def main_callback(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show debug output"),
) -> None:
    """Configure logging for CLI commands."""
    log_level = logging.DEBUG if verbose else logging.WARNING

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
    )


@app.command("shell")
def shell() -> None:
    """Interactive shell mode - run commands without typing 'cognitex' each time."""
    import shlex
    import subprocess
    import sys

    console.print("\n[bold cyan]Cognitex Interactive Shell[/bold cyan]")
    console.print("[dim]Type commands without 'cognitex' prefix. Use 'help' for commands, 'exit' to quit.[/dim]\n")

    # Quick aliases
    aliases = {
        "t": "tasks",
        "p": "projects",
        "g": "goals",
        "c": "calendar",
        "w": "web",
        "cs": "cheatsheet",
        "td": "task-done",
        "ts": "task-show",
        "tn": "task-new",
        "pn": "project-new",
        "ps": "project-show",
        "?": "cheatsheet",
    }

    while True:
        try:
            user_input = console.input("[bold green]cognitex>[/bold green] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye![/dim]")
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit", "q"):
            console.print("[dim]Goodbye![/dim]")
            break

        if user_input.lower() == "help":
            console.print("\n[bold]Available commands:[/bold]")
            console.print("  tasks, projects, goals, calendar, contacts")
            console.print("  task-new, task-show <#>, task-done <#>, task-update <#>")
            console.print("  project-new, project-show <#>, project-link <#>")
            console.print("  cheatsheet, status, today, briefing")
            console.print("\n[bold]Aliases:[/bold]")
            console.print("  t=tasks  p=projects  g=goals  c=calendar")
            console.print("  tn=task-new  ts=task-show  td=task-done")
            console.print("  pn=project-new  ps=project-show  ?=cheatsheet")
            console.print("\n[bold]Chat with agent:[/bold]")
            console.print("  Start with '>' to chat: > what tasks do I have today?")
            console.print()
            continue

        # Check if it's a chat message (starts with >)
        if user_input.startswith(">"):
            chat_msg = user_input[1:].strip()
            if chat_msg:
                subprocess.run(["cognitex", "agent-chat", chat_msg])
            continue

        # Parse command and args
        try:
            parts = shlex.split(user_input)
        except ValueError:
            parts = user_input.split()

        if not parts:
            continue

        # Apply alias
        cmd = aliases.get(parts[0], parts[0])
        args = ["cognitex", cmd] + parts[1:]

        # Run the command
        subprocess.run(args)


@app.command("cheatsheet")
def cheatsheet() -> None:
    """Show quick reference for common commands."""
    console.print("\n[bold cyan]Cognitex Quick Reference[/bold cyan]\n")

    # Daily workflow
    console.print("[bold]Daily Workflow[/bold]")
    console.print("  cognitex today          Morning briefing")
    console.print("  cognitex tasks          List all tasks (with short IDs)")
    console.print("  cognitex calendar       Today's events")
    console.print()

    # Task management
    console.print("[bold]Task Management[/bold]")
    console.print("  cognitex task-new       [green]Interactive form[/green] to create task")
    console.print("  cognitex task-show 1    Show task #1 with full context")
    console.print("  cognitex task-done 1    Mark task #1 complete")
    console.print("  cognitex task-update 1 --priority high")
    console.print("  cognitex task-add \"Title\" --priority medium")
    console.print("  cognitex task-link 1 --project proj_xxx --person email@example.com")
    console.print()

    # Projects & Goals
    console.print("[bold]Projects & Goals[/bold]")
    console.print("  cognitex project-new    [green]Interactive form[/green] to create project")
    console.print("  cognitex projects       List all projects")
    console.print("  cognitex goals          List all goals")
    console.print("  cognitex project-add \"Name\" --desc \"Description\"")
    console.print("  cognitex goal-add \"Name\" --timeframe \"Q1 2025\"")
    console.print()

    # Data sync
    console.print("[bold]Data Sync[/bold]")
    console.print("  cognitex sync           Sync Gmail (historical)")
    console.print("  cognitex calendar --sync   Sync calendar events")
    console.print("  cognitex drive-sync     Sync Drive metadata")
    console.print("  cognitex watch-setup    Enable real-time notifications")
    console.print()

    # Contacts & Search
    console.print("[bold]Contacts & Search[/bold]")
    console.print("  cognitex contacts       List enriched contacts")
    console.print("  cognitex doc-search \"query\"   Search documents")
    console.print("  cognitex graph \"cypher query\" Run graph query")
    console.print()

    # Agent
    console.print("[bold]Agent[/bold]")
    console.print("  cognitex agent-chat     Interactive agent conversation")
    console.print("  cognitex briefing       Generate morning summary")
    console.print("  cognitex approvals      Review pending agent actions")
    console.print()

    # Web Dashboard
    console.print("[bold]Web Dashboard[/bold]")
    console.print("  cognitex web            Start web UI at http://127.0.0.1:8080")
    console.print("  cognitex web --port 3000   Use different port")
    console.print()

    # System
    console.print("[bold]System[/bold]")
    console.print("  cognitex status         Check service status")
    console.print("  cognitex auth           Authenticate with Google")
    console.print("  cognitex -v <cmd>       Verbose output for debugging")
    console.print()


@app.command()
def dashboard() -> None:
    """Launch the interactive TUI dashboard."""
    console.print("[bold]Launching Cognitex Dashboard...[/bold]")
    # TODO: Launch Textual TUI
    console.print("[yellow]TUI not yet implemented. Coming in Phase 3.[/yellow]")


@app.command()
def status() -> None:
    """Show current system status."""
    console.print("[bold]Cognitex Status[/bold]\n")

    # Check Google credentials
    from cognitex.services.google_auth import check_credentials_status

    creds_status = check_credentials_status()

    table = Table(title="Service Status")
    table.add_column("Service", style="cyan")
    table.add_column("Status", style="green")

    # Google Auth status
    if creds_status["credentials_valid"]:
        google_status = "[green]Authenticated[/green]"
    elif creds_status["credentials_exists"]:
        google_status = "[yellow]Token expired (run 'cognitex auth')[/yellow]"
    elif creds_status["client_secrets_exists"]:
        google_status = "[yellow]Not authenticated (run 'cognitex auth')[/yellow]"
    else:
        google_status = "[red]No client_secret.json[/red]"

    table.add_row("Google API", google_status)
    table.add_row("PostgreSQL", "[dim]Use docker compose ps[/dim]")
    table.add_row("Neo4j", "[dim]Use docker compose ps[/dim]")
    table.add_row("Redis", "[dim]Use docker compose ps[/dim]")

    console.print(table)


@app.command()
def auth(
    force: bool = typer.Option(False, "--force", "-f", help="Force re-authentication"),
    browser: bool = typer.Option(False, "--browser", "-b", help="Use browser-based auth (requires desktop)"),
) -> None:
    """Authenticate with Google (Gmail, Calendar, Drive)."""
    from cognitex.services.google_auth import get_google_credentials, check_credentials_status

    status = check_credentials_status()

    if not status["client_secrets_exists"]:
        console.print("[red]Error: data/client_secret.json not found[/red]")
        console.print("Download it from Google Cloud Console and save it to data/client_secret.json")
        raise typer.Exit(1)

    if status["credentials_valid"] and not force:
        console.print("[green]Already authenticated![/green]")
        console.print(f"Scopes: {', '.join(status['scopes'])}")
        console.print("\nUse --force to re-authenticate")
        return

    console.print("[bold]Starting Google OAuth flow...[/bold]")
    if browser:
        console.print("A browser window will open for authentication.\n")
    else:
        console.print("Follow the instructions below to authenticate.\n")

    try:
        credentials = get_google_credentials(force_reauth=force, headless=not browser)
        console.print("\n[green]Authentication successful![/green]")

        # Show authenticated email
        from cognitex.services.gmail import GmailService
        gmail = GmailService()
        profile = gmail.get_profile()
        console.print(f"Authenticated as: [cyan]{profile['emailAddress']}[/cyan]")

    except Exception as e:
        console.print(f"[red]Authentication failed: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def sync(
    months: int = typer.Option(6, "--months", "-m", help="Months of history to sync"),
    incremental: bool = typer.Option(False, "--incremental", "-i", help="Incremental sync only"),
    all_mail: bool = typer.Option(False, "--all", "-a", help="Include all mail (not just inbox)"),
    clear: bool = typer.Option(False, "--clear", help="Clear existing emails before sync"),
) -> None:
    """Sync emails from Gmail into the graph database."""
    from cognitex.services.google_auth import check_credentials_status

    status = check_credentials_status()
    if not status["credentials_valid"]:
        console.print("[red]Not authenticated. Run 'cognitex auth' first.[/red]")
        raise typer.Exit(1)

    inbox_only = not all_mail
    console.print(f"[bold]Starting {'incremental' if incremental else 'historical'} sync...[/bold]")
    console.print(f"  Filter: {'all mail' if all_mail else 'inbox only'}")

    async def run_sync():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_driver
        from cognitex.db.graph_schema import init_graph_schema
        from cognitex.services.ingestion import run_historical_sync

        # Initialize Neo4j
        await init_neo4j()
        await init_graph_schema()

        try:
            if clear:
                console.print("[yellow]Clearing existing email data...[/yellow]")
                from cognitex.db.neo4j import get_neo4j_session
                async for session in get_neo4j_session():
                    await session.run("MATCH (e:Email) DETACH DELETE e")
                    await session.run("MATCH (p:Person) WHERE NOT (p)--() DELETE p")
                console.print("[green]Cleared.[/green]")

            if incremental:
                console.print("[yellow]Incremental sync not yet implemented. Running historical.[/yellow]")

            result = await run_historical_sync(months=months, inbox_only=inbox_only)

            console.print("\n[bold green]Sync complete![/bold green]")
            console.print(f"  Total emails: {result['total']}")
            console.print(f"  Successfully ingested: {result['success']}")
            if result['failed'] > 0:
                console.print(f"  [yellow]Failed: {result['failed']}[/yellow]")

        finally:
            await close_neo4j()

    asyncio.run(run_sync())


@app.command()
def calendar(
    months: int = typer.Option(1, "--months", "-m", help="Months of history to sync"),
    days: int = typer.Option(30, "--days", "-d", help="Days ahead to sync"),
    clear: bool = typer.Option(False, "--clear", help="Clear existing events before sync"),
) -> None:
    """Sync calendar events into the graph database."""
    from cognitex.services.google_auth import check_credentials_status

    status = check_credentials_status()
    if not status["credentials_valid"]:
        console.print("[red]Not authenticated. Run 'cognitex auth' first.[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Syncing calendar events...[/bold]")
    console.print(f"  History: {months} month(s) back")
    console.print(f"  Upcoming: {days} days ahead")

    async def run_calendar_sync_cli():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_driver
        from cognitex.db.graph_schema import init_graph_schema
        from cognitex.services.ingestion import run_calendar_sync

        await init_neo4j()
        await init_graph_schema()

        try:
            if clear:
                console.print("[yellow]Clearing existing event data...[/yellow]")
                from cognitex.db.neo4j import get_neo4j_session
                async for session in get_neo4j_session():
                    await session.run("MATCH (ev:Event) DETACH DELETE ev")
                console.print("[green]Cleared.[/green]")

            result = await run_calendar_sync(months_back=months, days_ahead=days)

            console.print("\n[bold green]Calendar sync complete![/bold green]")
            console.print(f"  Total events: {result['total']}")
            console.print(f"  Successfully ingested: {result['success']}")
            if result['failed'] > 0:
                console.print(f"  [yellow]Failed: {result['failed']}[/yellow]")

        finally:
            await close_neo4j()

    asyncio.run(run_calendar_sync_cli())


@app.command()
def today() -> None:
    """Show today's schedule and energy forecast."""
    from cognitex.services.google_auth import check_credentials_status

    status = check_credentials_status()
    if not status["credentials_valid"]:
        console.print("[red]Not authenticated. Run 'cognitex auth' first.[/red]")
        raise typer.Exit(1)

    async def show_today():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import get_today_events, get_daily_energy_forecast

        await init_neo4j()

        try:
            async for session in get_neo4j_session():
                events = await get_today_events(session)
                forecast = await get_daily_energy_forecast(session)

                console.print("\n[bold]Today's Schedule[/bold]\n")

                if not events:
                    console.print("  [dim]No events scheduled[/dim]")
                else:
                    for ev in events:
                        start = ev.get("start")
                        if hasattr(start, "strftime"):
                            time_str = start.strftime("%H:%M")
                        else:
                            time_str = str(start)[11:16] if start else "??:??"

                        energy = ev.get("energy_impact", 0)
                        energy_color = "green" if energy <= 2 else "yellow" if energy <= 4 else "red"

                        console.print(
                            f"  {time_str}  [{energy_color}]●[/{energy_color}] "
                            f"{ev.get('title', 'Untitled')} "
                            f"[dim]({ev.get('duration_minutes', 0)}m, {ev.get('event_type', 'unknown')})[/dim]"
                        )

                console.print(f"\n[bold]Energy Forecast[/bold]")
                console.print(f"  Events: {forecast['event_count']}")
                console.print(f"  Total meeting time: {forecast['total_minutes']} minutes")
                console.print(f"  Estimated energy cost: {forecast['total_energy_cost']} spoons")

        finally:
            await close_neo4j()

    asyncio.run(show_today())


@app.command()
def classify(
    limit: int = typer.Option(10, "--limit", "-l", help="Number of emails to classify"),
) -> None:
    """Classify unprocessed emails using LLM."""
    from cognitex.services.google_auth import check_credentials_status
    from cognitex.config import get_settings

    status = check_credentials_status()
    if not status["credentials_valid"]:
        console.print("[red]Not authenticated. Run 'cognitex auth' first.[/red]")
        raise typer.Exit(1)

    settings = get_settings()
    if not settings.together_api_key.get_secret_value():
        console.print("[red]TOGETHER_API_KEY not configured in .env[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Classifying up to {limit} unprocessed emails...[/bold]")

    async def run_classify():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import get_unprocessed_emails, mark_email_processed
        from cognitex.services.llm import get_llm_service

        await init_neo4j()
        llm = get_llm_service()

        try:
            async for session in get_neo4j_session():
                emails = await get_unprocessed_emails(session, limit=limit)

                if not emails:
                    console.print("[yellow]No unprocessed emails found.[/yellow]")
                    return

                console.print(f"Found {len(emails)} unprocessed emails\n")

                for i, email in enumerate(emails, 1):
                    console.print(f"[{i}/{len(emails)}] {email.get('subject', 'No subject')[:60]}...")

                    # Classify
                    result = await llm.classify_email({
                        "sender_email": "",  # Not stored in node currently
                        "sender_name": "",
                        "subject": email.get("subject", ""),
                        "snippet": email.get("snippet", ""),
                    })

                    # Update in graph
                    await mark_email_processed(
                        session,
                        gmail_id=email["gmail_id"],
                        classification=result["classification"],
                        action_required=result["action_required"],
                        urgency=result["urgency"],
                        sentiment=result["sentiment"],
                        inferred_tasks=result.get("suggested_tasks", []),
                    )

                    status_color = "green" if result["classification"] == "actionable" else "dim"
                    console.print(f"  → [{status_color}]{result['classification']}[/{status_color}] "
                                f"(urgency: {result['urgency']}, action: {result['action_required']})")

                console.print(f"\n[green]Classified {len(emails)} emails[/green]")

        finally:
            await close_neo4j()

    asyncio.run(run_classify())


@app.command()
def energy(level: int = typer.Argument(..., min=1, max=10)) -> None:
    """Set your current energy level (1-10)."""
    console.print(f"[bold]Energy level set to {level}/10[/bold]")
    # TODO: Store energy level
    console.print("[yellow]Energy tracking not yet implemented. Coming in Phase 4.[/yellow]")


@app.command()
def tasks(
    status: str = typer.Option(None, "--status", "-s", help="Filter by status (pending, in_progress, done)"),
    limit: int = typer.Option(20, "--limit", "-l", help="Number of tasks to show"),
    project: str = typer.Option(None, "--project", "-p", help="Filter by project ID or short ID"),
) -> None:
    """List tasks from the graph. Use short IDs (e.g., 1, 2, 3) with other task commands."""
    from cognitex.services.google_auth import check_credentials_status

    creds_status = check_credentials_status()
    if not creds_status["credentials_valid"]:
        console.print("[red]Not authenticated. Run 'cognitex auth' first.[/red]")
        raise typer.Exit(1)

    async def show_tasks():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import get_tasks
        from cognitex.db.redis import init_redis, get_redis, close_redis
        from cognitex.cli.task_ids import store_task_ids

        await init_neo4j()
        await init_redis()
        redis = get_redis()

        try:
            async for session in get_neo4j_session():
                task_list = await get_tasks(session, status=status, limit=limit)

                if not task_list:
                    console.print("[yellow]No tasks found.[/yellow]")
                    return

                # Store short ID mapping
                task_ids = [t.get("id") for t in task_list if t.get("id")]
                await store_task_ids(redis, task_ids)

                table = Table(title=f"Tasks ({len(task_list)})")
                table.add_column("#", style="bold cyan", width=4)
                table.add_column("", width=2)  # Status icon
                table.add_column("Pri", style="yellow", width=4)
                table.add_column("Title", style="white", width=35)
                table.add_column("Project", style="dim", width=20)
                table.add_column("Due", style="magenta", width=12)

                for idx, task in enumerate(task_list, 1):
                    status_icon = {
                        "pending": "[yellow]○[/yellow]",
                        "in_progress": "[blue]◐[/blue]",
                        "done": "[green]●[/green]",
                    }.get(task.get("status", "pending"), "○")

                    priority = task.get("priority", "medium")
                    pri_display = {
                        "critical": "[red bold]!![/red bold]",
                        "high": "[red]![/red]",
                        "medium": "[yellow]-[/yellow]",
                        "low": "[dim]·[/dim]",
                    }.get(priority, "-")

                    due = task.get("due")
                    due_str = str(due)[:10] if due else "-"

                    project_name = task.get("project_name") or task.get("project") or "-"

                    table.add_row(
                        str(idx),
                        status_icon,
                        pri_display,
                        task.get("title", "Untitled")[:35],
                        project_name[:20],
                        due_str,
                    )

                console.print(table)
                console.print("\n[dim]Use short IDs with commands: task-show 1, task-done 2, task-link 3 --project ...[/dim]")

        finally:
            await close_redis()
            await close_neo4j()

    asyncio.run(show_tasks())


@app.command()
def infer_tasks(
    limit: int = typer.Option(10, "--limit", "-l", help="Number of emails to process"),
) -> None:
    """Infer tasks from actionable emails using LLM."""
    from cognitex.services.google_auth import check_credentials_status
    from cognitex.config import get_settings
    import uuid

    creds_status = check_credentials_status()
    if not creds_status["credentials_valid"]:
        console.print("[red]Not authenticated. Run 'cognitex auth' first.[/red]")
        raise typer.Exit(1)

    settings = get_settings()
    if not settings.together_api_key.get_secret_value():
        console.print("[red]TOGETHER_API_KEY not configured in .env[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Inferring tasks from up to {limit} actionable emails...[/bold]")

    async def run_inference():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import (
            get_actionable_emails,
            create_task,
            link_task_to_email,
            link_task_to_person,
        )
        from cognitex.services.llm import get_llm_service

        await init_neo4j()
        llm = get_llm_service()

        tasks_created = 0

        try:
            async for session in get_neo4j_session():
                emails = await get_actionable_emails(session, limit=limit)

                if not emails:
                    console.print("[yellow]No actionable emails without tasks found.[/yellow]")
                    return

                console.print(f"Found {len(emails)} actionable emails to process\n")

                for i, email in enumerate(emails, 1):
                    subject = email.get("subject", "No subject")[:60]
                    console.print(f"[{i}/{len(emails)}] {subject}...")

                    # Build email data for LLM
                    email_data = {
                        "sender_email": email.get("sender_email", ""),
                        "sender_name": email.get("sender_name", ""),
                        "subject": email.get("subject", ""),
                        "snippet": email.get("snippet", ""),
                    }

                    # Infer tasks using LLM
                    inferred = await llm.infer_tasks_from_email(email_data)

                    if not inferred:
                        console.print("  → [dim]No tasks inferred[/dim]")
                        continue

                    for task_data in inferred:
                        task_id = f"task_{uuid.uuid4().hex[:12]}"

                        await create_task(
                            session,
                            task_id=task_id,
                            title=task_data["title"],
                            description=task_data.get("description"),
                            energy_cost=task_data.get("energy_cost", 3),
                            due_date=task_data.get("due_date"),
                            source_type="email",
                            source_id=email.get("gmail_id"),
                        )

                        await link_task_to_email(session, task_id, email.get("gmail_id"))

                        # Link to sender if we have their email
                        if email.get("sender_email"):
                            await link_task_to_person(
                                session,
                                task_id,
                                email["sender_email"],
                                relationship_type="REQUESTED_BY",
                            )

                        tasks_created += 1
                        console.print(f"  → [green]Created:[/green] {task_data['title'][:50]}")

                console.print(f"\n[bold green]Created {tasks_created} tasks[/bold green]")

        finally:
            await close_neo4j()

    asyncio.run(run_inference())


@app.command()
def graph() -> None:
    """Show graph database statistics."""
    async def show_stats():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import get_graph_stats

        await init_neo4j()

        try:
            async for session in get_neo4j_session():
                stats = await get_graph_stats(session)

                console.print("\n[bold]Graph Statistics[/bold]\n")

                # Nodes table
                nodes_table = Table(title="Nodes")
                nodes_table.add_column("Type", style="cyan")
                nodes_table.add_column("Count", style="green", justify="right")

                for label, count in sorted(stats["nodes"].items()):
                    nodes_table.add_row(label, str(count))

                console.print(nodes_table)

                # Relationships table
                rels_table = Table(title="Relationships")
                rels_table.add_column("Type", style="cyan")
                rels_table.add_column("Count", style="green", justify="right")

                for rel_type, count in sorted(stats["relationships"].items()):
                    rels_table.add_row(rel_type, str(count))

                console.print(rels_table)

                # Connection info
                console.print("\n[dim]View in browser: http://localhost:7474[/dim]")
                console.print("[dim]For remote: ssh -L 7474:localhost:7474 user@server[/dim]")

        finally:
            await close_neo4j()

    asyncio.run(show_stats())


@app.command()
def enrich(
    limit: int = typer.Option(20, "--limit", "-l", help="Number of contacts to enrich"),
) -> None:
    """Enrich contacts with org, role, and communication style using LLM."""
    from cognitex.config import get_settings

    settings = get_settings()
    if not settings.together_api_key.get_secret_value():
        console.print("[red]TOGETHER_API_KEY not configured in .env[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Enriching up to {limit} contacts...[/bold]")

    async def run_enrichment():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import get_contacts_for_enrichment, update_person_enrichment
        from cognitex.services.llm import get_llm_service

        await init_neo4j()
        llm = get_llm_service()

        enriched_count = 0

        try:
            async for session in get_neo4j_session():
                contacts = await get_contacts_for_enrichment(session, limit=limit)

                if not contacts:
                    console.print("[yellow]No contacts to enrich.[/yellow]")
                    return

                console.print(f"Found {len(contacts)} contacts to enrich\n")

                for i, contact in enumerate(contacts, 1):
                    email = contact["email"]
                    name = contact.get("name")
                    snippets = contact.get("sample_snippets") or []

                    interaction = f"{contact['emails_sent']} emails sent, {contact['events_attended']} events"

                    console.print(f"[{i}/{len(contacts)}] {email}...")

                    # Enrich using LLM
                    result = await llm.enrich_contact(
                        email_address=email,
                        name=name,
                        sample_snippets=snippets,
                        interaction_summary=interaction,
                    )

                    # Update in graph
                    await update_person_enrichment(
                        session,
                        email=email,
                        org=result.get("organization"),
                        role=result.get("role"),
                        communication_style=result.get("communication_style"),
                        urgency_tendency=result.get("urgency_tendency"),
                    )

                    enriched_count += 1

                    # Display result
                    org = result.get("organization") or "-"
                    role = result.get("role") or "-"
                    style = result.get("communication_style") or "-"
                    console.print(f"  → [green]{org}[/green] | {role} | {style}")

                console.print(f"\n[bold green]Enriched {enriched_count} contacts[/bold green]")

        finally:
            await close_neo4j()

    asyncio.run(run_enrichment())


@app.command()
def contacts(
    limit: int = typer.Option(30, "--limit", "-l", help="Number of contacts to show"),
    enriched_only: bool = typer.Option(False, "--enriched", "-e", help="Show only enriched contacts"),
) -> None:
    """List contacts from the graph."""
    async def show_contacts():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session

        await init_neo4j()

        try:
            async for session in get_neo4j_session():
                enriched_filter = "WHERE p.enriched = true" if enriched_only else ""
                query = f"""
                MATCH (p:Person)
                {enriched_filter}
                OPTIONAL MATCH (p)<-[:SENT_BY]-(e:Email)
                OPTIONAL MATCH (p)<-[:ATTENDED_BY]-(ev:Event)
                WITH p, count(DISTINCT e) as emails, count(DISTINCT ev) as events
                RETURN p, emails, events
                ORDER BY emails + events DESC
                LIMIT $limit
                """
                result = await session.run(query, limit=limit)
                records = await result.data()

                if not records:
                    console.print("[yellow]No contacts found.[/yellow]")
                    return

                table = Table(title=f"Contacts ({len(records)})")
                table.add_column("Email", style="cyan", width=30)
                table.add_column("Name", style="white", width=20)
                table.add_column("Org", style="green", width=15)
                table.add_column("Role", style="dim", width=15)
                table.add_column("Style", style="yellow", width=8)
                table.add_column("Activity", style="magenta", width=12)

                for record in records:
                    p = dict(record["p"])
                    emails = record["emails"]
                    events = record["events"]

                    table.add_row(
                        p.get("email", "-")[:30],
                        (p.get("name") or "-")[:20],
                        (p.get("org") or "-")[:15],
                        (p.get("role") or "-")[:15],
                        (p.get("communication_style") or "-")[:8],
                        f"{emails}e/{events}ev",
                    )

                console.print(table)

        finally:
            await close_neo4j()

    asyncio.run(show_contacts())


@app.command("drive-sync")
def drive_sync(
    folder: str = typer.Option(None, "--folder", "-f", help="Sync specific folder only"),
    index_priority: bool = typer.Option(False, "--index-priority", "-i", help="Index content from priority folders"),
    skip_metadata: bool = typer.Option(False, "--skip-metadata", help="Skip metadata sync, only run indexing"),
    limit: int = typer.Option(100, "--limit", "-l", help="Max documents to index"),
) -> None:
    """Sync Google Drive files into the graph database."""
    from cognitex.services.google_auth import check_credentials_status

    creds_status = check_credentials_status()
    if not creds_status["credentials_valid"]:
        console.print("[red]Not authenticated. Run 'cognitex auth' first.[/red]")
        raise typer.Exit(1)

    async def run_drive_sync():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.db.graph_schema import init_graph_schema
        from cognitex.services.ingestion import (
            run_drive_metadata_sync,
            run_drive_folder_sync,
            run_deep_document_indexing,
        )

        await init_neo4j()
        await init_graph_schema()

        try:
            # Always ensure metadata is up to date first (unless explicitly skipped)
            if not skip_metadata:
                if folder:
                    # Sync specific folder
                    console.print(f"[bold]Syncing folder: {folder}[/bold]")
                    result = await run_drive_folder_sync(folder)
                else:
                    # Sync all Drive metadata
                    console.print("[bold]Syncing Drive metadata...[/bold]")
                    result = await run_drive_metadata_sync()

                console.print(f"\n[green]Metadata sync complete![/green]")
                console.print(f"  Total files: {result.get('total', 0)}")
                console.print(f"  Successfully synced: {result.get('success', 0)}")
                if result.get('failed', 0) > 0:
                    console.print(f"  [yellow]Failed: {result.get('failed', 0)}[/yellow]")

            # Deep index priority folders if requested
            # Uses database-driven queue instead of crawling Drive
            if index_priority:
                console.print("\n[bold]Deep Indexing Priority Content...[/bold]")
                console.print("[dim]Checking for new or modified files in priority folders...[/dim]")
                await init_postgres()

                try:
                    async for pg_session in get_session():
                        index_result = await run_deep_document_indexing(
                            pg_session,
                            limit=limit,
                        )

                        console.print(f"\n[green]Deep indexing complete![/green]")
                        console.print(f"  Documents processed: {index_result.get('documents_processed', 0)}")
                        console.print(f"  Total chunks: {index_result.get('chunks_total', 0)}")
                        console.print(f"  Skipped: {index_result.get('skipped', 0)}")
                        console.print(f"  Failed: {index_result.get('failed', 0)}")

                finally:
                    await close_postgres()

        finally:
            await close_neo4j()

    asyncio.run(run_drive_sync())


@app.command()
def documents(
    limit: int = typer.Option(30, "--limit", "-l", help="Number of documents to show"),
    folder: str = typer.Option(None, "--folder", "-f", help="Filter by folder path"),
    indexed: bool = typer.Option(False, "--indexed", "-i", help="Show only indexed documents"),
) -> None:
    """List documents from the graph."""
    async def show_documents():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import get_documents, get_document_stats

        await init_neo4j()

        try:
            async for session in get_neo4j_session():
                # Get stats first
                stats = await get_document_stats(session)

                console.print(f"\n[bold]Document Statistics[/bold]")
                console.print(f"  Total: {stats['total']}")
                console.print(f"  Indexed: {stats['indexed']}")
                console.print(f"  Shared: {stats['shared']}")

                if stats['by_folder']:
                    console.print("\n  Top folders:")
                    for folder_name, count in sorted(stats['by_folder'].items(), key=lambda x: -x[1])[:5]:
                        console.print(f"    {folder_name}: {count}")

                # Get document list
                docs = await get_documents(
                    session,
                    folder_path=folder,
                    indexed_only=indexed,
                    limit=limit,
                )

                if not docs:
                    console.print("\n[yellow]No documents found.[/yellow]")
                    return

                console.print()
                table = Table(title=f"Documents ({len(docs)})")
                table.add_column("Name", style="cyan", width=35)
                table.add_column("Type", style="dim", width=12)
                table.add_column("Folder", style="white", width=20)
                table.add_column("Owner", style="green", width=25)
                table.add_column("Idx", style="yellow", width=3)

                for doc in docs:
                    # Get short mime type
                    mime = doc.get("mime_type", "")
                    if "document" in mime:
                        type_str = "Doc"
                    elif "spreadsheet" in mime:
                        type_str = "Sheet"
                    elif "presentation" in mime:
                        type_str = "Slides"
                    elif "pdf" in mime:
                        type_str = "PDF"
                    elif "folder" in mime:
                        type_str = "Folder"
                    elif mime.startswith("image/"):
                        type_str = "Image"
                    else:
                        type_str = mime.split("/")[-1][:10] if mime else "-"

                    folder_path = doc.get("folder_path") or "-"
                    if len(folder_path) > 20:
                        folder_path = "..." + folder_path[-17:]

                    idx_status = "[green]✓[/green]" if doc.get("indexed") else "[dim]-[/dim]"

                    table.add_row(
                        doc.get("name", "-")[:35],
                        type_str,
                        folder_path,
                        (doc.get("owner_email") or "-")[:25],
                        idx_status,
                    )

                console.print(table)

        finally:
            await close_neo4j()

    asyncio.run(show_documents())


@app.command("doc-search")
def doc_search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(10, "--limit", "-l", help="Maximum results"),
) -> None:
    """Search documents using semantic similarity."""
    from cognitex.config import get_settings

    settings = get_settings()
    if not settings.together_api_key.get_secret_value():
        console.print("[red]TOGETHER_API_KEY not configured in .env[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Searching for:[/bold] {query}\n")

    async def run_search():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.services.ingestion import search_documents_semantic

        await init_neo4j()
        await init_postgres()

        try:
            async for pg_session in get_session():
                results = await search_documents_semantic(pg_session, query, limit=limit)

                if not results:
                    console.print("[yellow]No matching documents found.[/yellow]")
                    return

                # Get document details from Neo4j
                async for neo_session in get_neo4j_session():
                    for i, result in enumerate(results, 1):
                        drive_id = result["drive_id"]
                        similarity = result["similarity"]

                        # Get doc name from Neo4j
                        doc_query = "MATCH (d:Document {drive_id: $drive_id}) RETURN d.name as name, d.web_link as link"
                        doc_result = await neo_session.run(doc_query, drive_id=drive_id)
                        doc_record = await doc_result.single()

                        doc_name = doc_record["name"] if doc_record else drive_id
                        doc_link = doc_record["link"] if doc_record else None

                        console.print(f"[bold cyan]{i}. {doc_name}[/bold cyan]")
                        console.print(f"   Similarity: [green]{similarity:.2%}[/green]")
                        if doc_link:
                            console.print(f"   [dim]{doc_link}[/dim]")
                        console.print(f"   [dim]{result['content_preview'][:200]}...[/dim]\n")

        finally:
            await close_postgres()
            await close_neo4j()

    asyncio.run(run_search())


@app.command("deep-index")
def deep_index(
    limit: int = typer.Option(100, "--limit", "-l", help="Max documents to index"),
    max_size: int = typer.Option(10, "--max-size", "-s", help="Max file size in MB"),
    source: str = typer.Option("all", "--source", help="Source to index: drive, github, or all"),
) -> None:
    """
    Deep index documents with semantic chunking.

    Splits documents into overlapping chunks for comprehensive understanding.
    Each chunk gets its own embedding, enabling passage-level retrieval.

    Use --source to choose which sources to index:
    - drive: Only Google Drive priority folders
    - github: Only GitHub priority repos
    - all: Both Drive and GitHub (default)
    """
    from cognitex.config import get_settings

    settings = get_settings()
    if not settings.together_api_key.get_secret_value():
        console.print("[red]TOGETHER_API_KEY not configured in .env[/red]")
        raise typer.Exit(1)

    source = source.lower()
    if source not in ("drive", "github", "all"):
        console.print(f"[red]Invalid source: {source}. Use: drive, github, or all[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Starting deep indexing...[/bold]")
    console.print(f"  Source: {source}")
    console.print(f"  Limit: {limit} documents")
    console.print(f"  Max file size: {max_size}MB")
    console.print("[dim]Using database-driven queue (only indexes new/modified files)[/dim]\n")

    async def run_indexing():
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.services.ingestion import run_deep_document_indexing, run_deep_code_indexing

        await init_postgres()

        try:
            async for pg_session in get_session():
                # Index Drive documents
                if source in ("drive", "all"):
                    console.print("[cyan]Indexing Drive documents...[/cyan]")
                    drive_stats = await run_deep_document_indexing(
                        pg_session,
                        limit=limit,
                        max_file_size=max_size * 1_000_000,
                    )

                    console.print("\n[green]Drive indexing complete:[/green]")
                    console.print(f"  Documents processed: {drive_stats.get('documents_processed', 0)}")
                    console.print(f"  Total chunks: {drive_stats.get('chunks_total', 0)}")
                    console.print(f"  Embeddings: {drive_stats.get('embeddings_total', 0)}")
                    if drive_stats.get('failed', 0) > 0:
                        console.print(f"  [yellow]Failed: {drive_stats.get('failed', 0)}[/yellow]")

                # Index GitHub code
                if source in ("github", "all"):
                    console.print("\n[cyan]Indexing GitHub code...[/cyan]")
                    github_stats = await run_deep_code_indexing(
                        pg_session,
                        limit=limit,
                        max_file_size=max_size * 1_000_000,
                    )

                    console.print("\n[green]GitHub indexing complete:[/green]")
                    console.print(f"  Files processed: {github_stats.get('files_processed', 0)}")
                    console.print(f"  Embeddings: {github_stats.get('embeddings_created', 0)}")
                    if github_stats.get('skipped', 0) > 0:
                        console.print(f"  Skipped: {github_stats.get('skipped', 0)}")
                    if github_stats.get('failed', 0) > 0:
                        console.print(f"  [yellow]Failed: {github_stats.get('failed', 0)}[/yellow]")

                console.print("\n[bold green]Deep indexing complete![/bold green]")

        finally:
            await close_postgres()

    asyncio.run(run_indexing())


@app.command("index-file")
def index_file(
    file_id: str = typer.Argument(..., help="Google Drive file ID to index"),
    file_name: str = typer.Option("", "--name", "-n", help="File name (optional, for display)"),
) -> None:
    """
    Index a single Drive file with chunking and graph analysis.

    Useful for testing auto-indexing or manually re-indexing a specific file.
    """
    from cognitex.config import get_settings

    settings = get_settings()
    if not settings.together_api_key.get_secret_value():
        console.print("[red]TOGETHER_API_KEY not configured in .env[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Auto-indexing file: {file_id}[/bold]")

    async def run_auto_index():
        from cognitex.services.drive import get_drive_service
        from cognitex.services.ingestion import auto_index_drive_file

        drive = get_drive_service()

        # Get file metadata if name not provided
        name = file_name
        mime_type = None
        if not name:
            metadata = drive.get_file_metadata(file_id)
            if not metadata:
                console.print("[red]File not found in Drive[/red]")
                return
            name = metadata.get("name", file_id)
            mime_type = metadata.get("mimeType", "")
            console.print(f"  Name: {name}")
            console.print(f"  MIME type: {mime_type}")
        else:
            # Still need to get MIME type
            metadata = drive.get_file_metadata(file_id)
            if metadata:
                mime_type = metadata.get("mimeType", "")

        if not mime_type:
            console.print("[red]Could not determine file type[/red]")
            return

        console.print("\n[dim]Indexing...[/dim]")

        stats = await auto_index_drive_file(
            file_id=file_id,
            file_name=name,
            mime_type=mime_type,
        )

        if stats.get("error"):
            console.print(f"[red]Error: {stats['error']}[/red]")
            return

        if stats.get("indexed"):
            console.print("\n[bold green]File indexed successfully![/bold green]")
            console.print(f"  Chunks created: {stats['chunks_created']}")
            console.print(f"  Embeddings: {stats['embeddings_created']}")
            console.print(f"  Chunks analyzed: {stats['chunks_analyzed']}")
            console.print(f"  Topics created: {stats['topics_created']}")
            console.print(f"  Concepts created: {stats['concepts_created']}")
        else:
            console.print("[yellow]File was not indexed (possibly skipped)[/yellow]")

    asyncio.run(run_auto_index())


@app.command("chunk-search")
def chunk_search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(10, "--limit", "-l", help="Maximum results"),
) -> None:
    """
    Search document chunks using semantic similarity.

    Returns the most relevant passages from across all indexed documents.
    Use this for more precise retrieval than whole-document search.
    """
    from cognitex.config import get_settings

    settings = get_settings()
    if not settings.together_api_key.get_secret_value():
        console.print("[red]TOGETHER_API_KEY not configured in .env[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Searching chunks for:[/bold] {query}\n")

    async def run_search():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.services.ingestion import search_chunks_semantic

        await init_neo4j()
        await init_postgres()

        try:
            async for pg_session in get_session():
                results = await search_chunks_semantic(pg_session, query, limit=limit)

                if not results:
                    console.print("[yellow]No matching chunks found.[/yellow]")
                    console.print("[dim]Try running 'cognitex deep-index' first.[/dim]")
                    return

                # Group results by document
                docs_seen = {}
                async for neo_session in get_neo4j_session():
                    for i, result in enumerate(results, 1):
                        drive_id = result["drive_id"]
                        similarity = result["similarity"]

                        # Get doc name from Neo4j (cache lookups)
                        if drive_id not in docs_seen:
                            doc_query = "MATCH (d:Document {drive_id: $drive_id}) RETURN d.name as name, d.web_link as link"
                            doc_result = await neo_session.run(doc_query, drive_id=drive_id)
                            doc_record = await doc_result.single()
                            docs_seen[drive_id] = {
                                "name": doc_record["name"] if doc_record else drive_id,
                                "link": doc_record["link"] if doc_record else None,
                            }

                        doc_info = docs_seen[drive_id]
                        chunk_idx = result["chunk_index"]

                        console.print(f"[bold cyan]{i}. {doc_info['name']}[/bold cyan] [dim](chunk {chunk_idx})[/dim]")
                        console.print(f"   Similarity: [green]{similarity:.2%}[/green]")
                        if doc_info["link"]:
                            console.print(f"   [dim]{doc_info['link']}[/dim]")

                        # Show chunk content (truncated)
                        content = result["content"]
                        if len(content) > 300:
                            content = content[:300] + "..."
                        console.print(f"   [dim]{content}[/dim]\n")

        finally:
            await close_postgres()
            await close_neo4j()

    asyncio.run(run_search())


# =============================================================================
# Agent Commands
# =============================================================================

@app.command()
def agent_chat(
    message: str = typer.Argument(None, help="Message to send to the agent"),
) -> None:
    """Chat with the Cognitex agent."""
    from cognitex.config import get_settings

    settings = get_settings()
    if not settings.together_api_key.get_secret_value():
        console.print("[red]TOGETHER_API_KEY not configured in .env[/red]")
        raise typer.Exit(1)

    async def run_chat():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.db.redis import init_redis, close_redis
        from cognitex.agent import get_agent

        await init_neo4j()
        await init_postgres()
        await init_redis()

        try:
            agent = await get_agent()

            if message:
                # Single message mode
                console.print(f"[dim]You:[/dim] {message}\n")
                response = await agent.chat(message)
                console.print(f"[bold cyan]Cognitex:[/bold cyan] {response}")
            else:
                # Interactive mode
                console.print("[bold]Cognitex Agent Chat[/bold]")
                console.print("[dim]Type 'quit' or 'exit' to leave, 'approvals' to see pending actions[/dim]\n")

                while True:
                    try:
                        user_input = console.input("[green]You:[/green] ")
                    except (KeyboardInterrupt, EOFError):
                        break

                    if user_input.lower() in ("quit", "exit", "q"):
                        break

                    if user_input.lower() == "approvals":
                        approvals = await agent.get_pending_approvals()
                        if not approvals:
                            console.print("[dim]No pending approvals[/dim]\n")
                        else:
                            for apr in approvals:
                                console.print(f"[yellow]{apr['id']}[/yellow]: {apr['action_type']}")
                                console.print(f"  [dim]{apr['reasoning'][:100]}...[/dim]\n")
                        continue

                    if user_input.lower().startswith("approve "):
                        approval_id = user_input.split(" ", 1)[1].strip()
                        result = await agent.handle_approval(approval_id, approved=True)
                        if result.get("success"):
                            console.print(f"[green]Approved and executed: {result.get('action')}[/green]\n")
                        else:
                            console.print(f"[red]Failed: {result.get('error')}[/red]\n")
                        continue

                    if user_input.lower().startswith("reject "):
                        approval_id = user_input.split(" ", 1)[1].strip()
                        result = await agent.handle_approval(approval_id, approved=False)
                        console.print(f"[yellow]Rejected: {result.get('action')}[/yellow]\n")
                        continue

                    if not user_input.strip():
                        continue

                    response = await agent.chat(user_input)
                    console.print(f"\n[bold cyan]Cognitex:[/bold cyan] {response}\n")

        finally:
            await close_redis()
            await close_neo4j()
            await close_postgres()

    asyncio.run(run_chat())


@app.command()
def briefing() -> None:
    """Get a briefing from the agent (morning summary)."""
    from cognitex.config import get_settings

    settings = get_settings()
    if not settings.together_api_key.get_secret_value():
        console.print("[red]TOGETHER_API_KEY not configured in .env[/red]")
        raise typer.Exit(1)

    console.print("[bold]Generating briefing...[/bold]\n")

    async def run_briefing():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.db.redis import init_redis, close_redis
        from cognitex.agent import get_agent

        await init_neo4j()
        await init_postgres()
        await init_redis()

        try:
            agent = await get_agent()
            briefing = await agent.morning_briefing()
            console.print(briefing)
        finally:
            await close_redis()
            await close_neo4j()
            await close_postgres()

    asyncio.run(run_briefing())


@app.command()
def approvals(
    action: str = typer.Argument(None, help="Action: 'list', 'approve <id>', or 'reject <id>'"),
    approval_id: str = typer.Argument(None, help="Approval ID for approve/reject actions"),
) -> None:
    """Manage pending agent approvals."""
    async def manage_approvals():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.db.redis import init_redis, close_redis
        from cognitex.agent import get_agent

        await init_neo4j()
        await init_postgres()
        await init_redis()

        try:
            agent = await get_agent()
            pending = await agent.get_pending_approvals()

            if not action or action == "list":
                if not pending:
                    console.print("[dim]No pending approvals[/dim]")
                    return

                console.print(f"[bold]Pending Approvals ({len(pending)})[/bold]\n")

                for apr in pending:
                    action_type = apr["action_type"]
                    params = apr["params"]

                    console.print(f"[bold yellow]{apr['id']}[/bold yellow]")
                    console.print(f"  Action: [cyan]{action_type}[/cyan]")

                    if action_type == "send_email":
                        console.print(f"  To: {params.get('to')}")
                        console.print(f"  Subject: {params.get('subject')}")
                        console.print(f"  [dim]Body preview: {params.get('body', '')[:100]}...[/dim]")
                    elif action_type == "create_event":
                        console.print(f"  Title: {params.get('title')}")
                        console.print(f"  Start: {params.get('start')}")

                    console.print(f"  [dim]Reasoning: {apr['reasoning'][:150]}...[/dim]")
                    console.print()

                console.print("[dim]Use 'cognitex approvals approve <id>' or 'cognitex approvals reject <id>'[/dim]")

            elif action == "approve" and approval_id:
                result = await agent.handle_approval(approval_id, approved=True)
                if result.get("success"):
                    console.print(f"[green]Approved and executed: {result.get('action')}[/green]")
                else:
                    console.print(f"[red]Failed: {result.get('error')}[/red]")

            elif action == "reject" and approval_id:
                result = await agent.handle_approval(approval_id, approved=False)
                console.print(f"[yellow]Rejected: {result.get('action')}[/yellow]")

            else:
                console.print("[red]Invalid action. Use 'list', 'approve <id>', or 'reject <id>'[/red]")

        finally:
            await close_redis()
            await close_neo4j()
            await close_postgres()

    asyncio.run(manage_approvals())


@app.command("agent-run")
def agent_run(
    mode: str = typer.Argument(..., help="Mode: briefing, review, monitor, escalate"),
    trigger: str = typer.Option("Manual CLI trigger", "--trigger", "-t", help="Trigger description"),
) -> None:
    """Run the agent in a specific mode."""
    from cognitex.config import get_settings

    settings = get_settings()
    if not settings.together_api_key.get_secret_value():
        console.print("[red]TOGETHER_API_KEY not configured in .env[/red]")
        raise typer.Exit(1)

    mode_map = {
        "briefing": "BRIEFING",
        "review": "REVIEW",
        "monitor": "MONITOR",
        "escalate": "ESCALATE",
    }

    if mode.lower() not in mode_map:
        console.print(f"[red]Invalid mode. Choose from: {', '.join(mode_map.keys())}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Running agent in {mode} mode...[/bold]\n")

    async def run_agent():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.db.redis import init_redis, close_redis
        from cognitex.agent import get_agent, AgentMode

        await init_neo4j()
        await init_postgres()
        await init_redis()

        try:
            agent = await get_agent()
            agent_mode = AgentMode[mode_map[mode.lower()]]

            result = await agent.run(
                mode=agent_mode,
                trigger=trigger,
            )

            console.print(f"[bold]Execution Result[/bold]")
            console.print(f"  Success: {'[green]Yes[/green]' if result.success else '[red]No[/red]'}")
            console.print(f"  Steps: {result.steps_executed}/{result.steps_total}")

            if result.pending_approvals:
                console.print(f"  Pending approvals: [yellow]{len(result.pending_approvals)}[/yellow]")

            if result.errors:
                console.print(f"  [red]Errors:[/red]")
                for err in result.errors:
                    console.print(f"    - {err}")

            if result.user_notification:
                console.print(f"\n[bold cyan]Agent says:[/bold cyan]")
                console.print(result.user_notification)

        finally:
            await close_redis()
            await close_neo4j()
            await close_postgres()

    asyncio.run(run_agent())


@app.command("discord-test")
def discord_test(
    send_notification: bool = typer.Option(False, "--notify", "-n", help="Send a test notification via Redis pub/sub"),
) -> None:
    """Test Discord bot connectivity and functionality."""
    console.print("[bold]Discord Integration Test[/bold]\n")

    from cognitex.config import get_settings
    settings = get_settings()

    results = {}

    # 1. Check Discord configuration
    console.print("[cyan]1. Checking Discord configuration...[/cyan]")
    bot_token = settings.discord_bot_token.get_secret_value() if settings.discord_bot_token else None
    channel_id = settings.discord_channel_id

    if bot_token:
        results["bot_token"] = True
        console.print("   [green]✓[/green] DISCORD_BOT_TOKEN is configured")
    else:
        results["bot_token"] = False
        console.print("   [red]✗[/red] DISCORD_BOT_TOKEN not set in .env")

    if channel_id:
        results["channel_id"] = True
        console.print(f"   [green]✓[/green] DISCORD_CHANNEL_ID: {channel_id}")
    else:
        results["channel_id"] = False
        console.print("   [red]✗[/red] DISCORD_CHANNEL_ID not set in .env")

    # 2. Check Redis connectivity (needed for notifications)
    console.print("\n[cyan]2. Checking Redis connectivity...[/cyan]")

    async def check_redis():
        from cognitex.db.redis import init_redis, close_redis, get_redis
        try:
            await init_redis()
            redis = get_redis()
            await redis.ping()
            results["redis"] = True
            console.print("   [green]✓[/green] Redis is connected")
            return True
        except Exception as e:
            results["redis"] = False
            console.print(f"   [red]✗[/red] Redis connection failed: {e}")
            return False
        finally:
            try:
                await close_redis()
            except Exception:
                pass

    redis_ok = asyncio.run(check_redis())

    # 3. Check Neo4j connectivity (needed for slash commands)
    console.print("\n[cyan]3. Checking Neo4j connectivity...[/cyan]")

    async def check_neo4j():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        try:
            await init_neo4j()
            async for session in get_neo4j_session():
                result = await session.run("RETURN 1 as n")
                await result.single()
            results["neo4j"] = True
            console.print("   [green]✓[/green] Neo4j is connected")
            return True
        except Exception as e:
            results["neo4j"] = False
            console.print(f"   [red]✗[/red] Neo4j connection failed: {e}")
            return False
        finally:
            try:
                await close_neo4j()
            except Exception:
                pass

    neo4j_ok = asyncio.run(check_neo4j())

    # 4. Check PostgreSQL connectivity (needed for agent memory)
    console.print("\n[cyan]4. Checking PostgreSQL connectivity...[/cyan]")

    async def check_postgres():
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from sqlalchemy import text
        try:
            await init_postgres()
            async for session in get_session():
                await session.execute(text("SELECT 1"))
            results["postgres"] = True
            console.print("   [green]✓[/green] PostgreSQL is connected")
            return True
        except Exception as e:
            results["postgres"] = False
            console.print(f"   [red]✗[/red] PostgreSQL connection failed: {e}")
            return False
        finally:
            try:
                await close_postgres()
            except Exception:
                pass

    postgres_ok = asyncio.run(check_postgres())

    # 5. Check Together.ai (needed for agent chat)
    console.print("\n[cyan]5. Checking Together.ai configuration...[/cyan]")
    together_key = settings.together_api_key.get_secret_value() if settings.together_api_key else None

    if together_key:
        results["together"] = True
        console.print(f"   [green]✓[/green] TOGETHER_API_KEY is configured")
        console.print(f"   [dim]   Planner model: {settings.together_model_planner}[/dim]")
        console.print(f"   [dim]   Executor model: {settings.together_model_executor}[/dim]")
    else:
        results["together"] = False
        console.print("   [red]✗[/red] TOGETHER_API_KEY not set in .env")

    # 6. Test notification pub/sub if requested
    if send_notification and redis_ok:
        console.print("\n[cyan]6. Sending test notification via Redis pub/sub...[/cyan]")

        async def send_test_notification():
            import json
            from cognitex.db.redis import init_redis, close_redis, get_redis
            try:
                await init_redis()
                redis = get_redis()

                test_payload = json.dumps({
                    "title": "🧪 Test Notification",
                    "message": "This is a test notification from `cognitex discord-test --notify`",
                    "urgency": "low",
                    "fields": {
                        "Source": "CLI Test",
                        "Status": "Working",
                    }
                })

                await redis.publish("cognitex:notifications", test_payload)
                results["notification_sent"] = True
                console.print("   [green]✓[/green] Test notification published to cognitex:notifications")
                console.print("   [dim]   If the Discord bot is running, you should see it in the channel[/dim]")
            except Exception as e:
                results["notification_sent"] = False
                console.print(f"   [red]✗[/red] Failed to send notification: {e}")
            finally:
                try:
                    await close_redis()
                except Exception:
                    pass

        asyncio.run(send_test_notification())

    # Summary
    console.print("\n" + "=" * 50)
    console.print("[bold]Summary[/bold]\n")

    all_good = all([
        results.get("bot_token"),
        results.get("channel_id"),
        results.get("redis"),
        results.get("neo4j"),
        results.get("postgres"),
        results.get("together"),
    ])

    if all_good:
        console.print("[bold green]All checks passed! ✓[/bold green]\n")
        console.print("To start the Discord bot:")
        console.print("  [cyan]python -m cognitex.discord_bot[/cyan]\n")
        console.print("Available slash commands:")
        console.print("  /status    - System health check")
        console.print("  /tasks     - List pending tasks")
        console.print("  /today     - Today's schedule")
        console.print("  /briefing  - Morning briefing")
        console.print("  /approvals - Pending approvals\n")
        console.print("Natural language:")
        console.print("  @Cognitex what meetings do I have today?")
        console.print("  @Cognitex draft a reply to the last email from John")
    else:
        console.print("[bold yellow]Some checks failed.[/bold yellow]\n")
        console.print("Fix the issues above before starting the Discord bot.")
        console.print("\nRequired:")
        if not results.get("bot_token"):
            console.print("  - Set DISCORD_BOT_TOKEN in .env")
        if not results.get("channel_id"):
            console.print("  - Set DISCORD_CHANNEL_ID in .env")
        if not results.get("redis"):
            console.print("  - Start Redis: docker compose up -d redis")
        if not results.get("neo4j"):
            console.print("  - Start Neo4j: docker compose up -d neo4j")
        if not results.get("postgres"):
            console.print("  - Start PostgreSQL: docker compose up -d postgres")
        if not results.get("together"):
            console.print("  - Set TOGETHER_API_KEY in .env")


@app.command("watch-setup")
def watch_setup(
    webhook_url: str = typer.Option(None, "--webhook", "-w", help="Public HTTPS webhook URL (or set WEBHOOK_BASE_URL in .env)"),
    pubsub_topic: str = typer.Option(None, "--pubsub", "-p", help="Google Cloud Pub/Sub topic for Gmail (or set GOOGLE_PUBSUB_TOPIC in .env)"),
    gmail: bool = typer.Option(True, "--gmail/--no-gmail", help="Set up Gmail watch"),
    calendar: bool = typer.Option(True, "--calendar/--no-calendar", help="Set up Calendar watch"),
    drive: bool = typer.Option(True, "--drive/--no-drive", help="Set up Drive watch"),
) -> None:
    """Set up Google push notification watches for real-time updates."""
    from cognitex.config import get_settings
    settings = get_settings()

    # Use config values as fallback
    effective_webhook_url = webhook_url or settings.webhook_base_url
    effective_pubsub_topic = pubsub_topic or settings.google_pubsub_topic

    console.print("[bold]Setting up Google Push Notification Watches[/bold]\n")

    if effective_webhook_url:
        console.print(f"  Webhook URL: [cyan]{effective_webhook_url}[/cyan]")
    if effective_pubsub_topic:
        console.print(f"  Pub/Sub Topic: [cyan]{effective_pubsub_topic}[/cyan]")
    console.print()

    if not effective_webhook_url and (calendar or drive):
        console.print("[yellow]Warning: No webhook URL configured. Calendar and Drive watches require a public HTTPS endpoint.[/yellow]")
        console.print("[dim]Use --webhook https://your-domain.com, set WEBHOOK_BASE_URL in .env, or set up ngrok for development.[/dim]\n")

    async def setup_watches():
        from cognitex.services.push_notifications import get_watch_manager

        watch_manager = get_watch_manager(effective_webhook_url)
        results = {}

        if gmail:
            console.print("[cyan]Setting up Gmail watch...[/cyan]")
            if effective_pubsub_topic:
                result = await watch_manager.setup_gmail_watch(effective_pubsub_topic)
            else:
                console.print("  [yellow]Skipping Gmail - requires --pubsub topic or GOOGLE_PUBSUB_TOPIC in .env[/yellow]")
                result = {"skipped": "No Pub/Sub topic"}
            results["Gmail"] = result

        if calendar and effective_webhook_url:
            console.print("[cyan]Setting up Calendar watch...[/cyan]")
            result = await watch_manager.setup_calendar_watch()
            results["Calendar"] = result
        elif calendar:
            results["Calendar"] = {"skipped": "No webhook URL"}

        if drive and effective_webhook_url:
            console.print("[cyan]Setting up Drive watch...[/cyan]")
            result = await watch_manager.setup_drive_watch()
            results["Drive"] = result
        elif drive:
            results["Drive"] = {"skipped": "No webhook URL"}

        console.print("\n[bold]Results:[/bold]")
        for service, result in results.items():
            if "error" in result:
                console.print(f"  {service}: [red]Error - {result['error'][:50]}[/red]")
            elif "skipped" in result:
                console.print(f"  {service}: [yellow]Skipped - {result['skipped']}[/yellow]")
            else:
                expiration = result.get("expiration", "unknown")
                console.print(f"  {service}: [green]Active[/green] (expires: {expiration})")

    asyncio.run(setup_watches())


@app.command("watch-status")
def watch_status() -> None:
    """Show status of active Google push notification watches."""
    console.print("[bold]Active Google Push Notification Watches[/bold]\n")

    from cognitex.services.push_notifications import get_watch_manager
    from datetime import datetime

    watch_manager = get_watch_manager()
    watches = watch_manager.get_active_watches()

    if not watches:
        console.print("[yellow]No active watches. Run 'cognitex watch-setup' to create them.[/yellow]")
        return

    table = Table()
    table.add_column("Service", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Expires", style="white")
    table.add_column("Created", style="dim")

    for key, watch in watches.items():
        expiration_ms = watch.get("expiration")
        if expiration_ms:
            exp_dt = datetime.fromtimestamp(int(expiration_ms) / 1000)
            expires = exp_dt.strftime("%Y-%m-%d %H:%M")
            if exp_dt < datetime.now():
                status = "[red]Expired[/red]"
            else:
                status = "[green]Active[/green]"
        else:
            expires = "Unknown"
            status = "[yellow]Unknown[/yellow]"

        created = watch.get("created_at", "Unknown")
        if created != "Unknown":
            created = created[:16]  # Truncate to date + time

        table.add_row(key, status, expires, created)

    console.print(table)


@app.command("watch-stop")
def watch_stop(
    gmail: bool = typer.Option(False, "--gmail", help="Stop Gmail watch"),
    calendar: bool = typer.Option(False, "--calendar", help="Stop Calendar watch"),
    drive: bool = typer.Option(False, "--drive", help="Stop Drive watch"),
    all_watches: bool = typer.Option(False, "--all", help="Stop all watches"),
) -> None:
    """Stop Google push notification watches."""
    if not any([gmail, calendar, drive, all_watches]):
        console.print("[yellow]Specify which watches to stop: --gmail, --calendar, --drive, or --all[/yellow]")
        return

    async def stop_watches():
        from cognitex.services.push_notifications import get_watch_manager

        watch_manager = get_watch_manager()

        if gmail or all_watches:
            console.print("[cyan]Stopping Gmail watch...[/cyan]")
            await watch_manager.stop_gmail_watch()

        if calendar or all_watches:
            console.print("[cyan]Stopping Calendar watch...[/cyan]")
            await watch_manager.stop_calendar_watch()

        if drive or all_watches:
            console.print("[cyan]Stopping Drive watch...[/cyan]")
            await watch_manager.stop_drive_watch()

        console.print("[green]Done.[/green]")

    asyncio.run(stop_watches())


# =============================================================================
# Task/Project/Goal Management Commands
# =============================================================================

@app.command("task-add")
def task_add(
    title: str = typer.Argument(..., help="Task title"),
    description: str = typer.Option(None, "--desc", "-d", help="Task description"),
    priority: str = typer.Option("medium", "--priority", "-p", help="Priority: low, medium, high, critical"),
    due: str = typer.Option(None, "--due", help="Due date (ISO format, e.g. 2024-01-15)"),
    project: str = typer.Option(None, "--project", help="Project ID to link to"),
    goal: str = typer.Option(None, "--goal", help="Goal ID to link to"),
    effort: float = typer.Option(None, "--effort", "-e", help="Effort estimate in hours"),
    energy: str = typer.Option(None, "--energy", help="Energy cost: low, medium, high"),
) -> None:
    """Create a new task."""
    async def create_task():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.db.graph_schema import init_graph_schema
        from cognitex.services.tasks import get_task_service

        await init_neo4j()
        await init_graph_schema()

        try:
            task_service = get_task_service()
            task = await task_service.create(
                title=title,
                description=description,
                priority=priority,
                due_date=due,
                project_id=project,
                goal_id=goal,
                effort_estimate=effort,
                energy_cost=energy,
            )

            console.print(f"[green]Created task:[/green] {task['id']}")
            console.print(f"  Title: {task['title']}")
            console.print(f"  Priority: {task['priority']}")
            if due:
                console.print(f"  Due: {due}")
            if project:
                console.print(f"  Project: {project}")

        finally:
            await close_neo4j()

    asyncio.run(create_task())


@app.command("task-new")
def task_new() -> None:
    """Interactive form to create a new task."""
    console.print("\n[bold cyan]Create New Task[/bold cyan]\n")

    async def create_task_interactive():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import init_graph_schema, get_projects, get_goals, link_task_to_person
        from cognitex.services.tasks import get_task_service

        await init_neo4j()
        await init_graph_schema()

        try:
            # Fetch options for linking
            async for session in get_neo4j_session():
                # Get projects
                projects_result = await session.run(
                    "MATCH (p:Project) WHERE p.status <> 'completed' RETURN p.id as id, p.title as title ORDER BY p.title LIMIT 20"
                )
                projects = [(r["id"], r["title"]) async for r in projects_result]

                # Get goals
                goals_result = await session.run(
                    "MATCH (g:Goal) WHERE g.status <> 'completed' RETURN g.id as id, g.title as title ORDER BY g.title LIMIT 20"
                )
                goals = [(r["id"], r["title"]) async for r in goals_result]

                # Get contacts
                contacts_result = await session.run(
                    "MATCH (p:Person) WHERE p.email IS NOT NULL RETURN p.email as email, p.name as name ORDER BY p.name LIMIT 30"
                )
                contacts = [(r["email"], r["name"] or r["email"]) async for r in contacts_result]
                break

            # Title (required)
            title = Prompt.ask("[bold]Task title[/bold]")
            if not title:
                console.print("[red]Title is required[/red]")
                return

            # Description
            description = Prompt.ask("Description [dim](optional)[/dim]", default="")

            # Priority
            console.print("\n[bold]Priority:[/bold]")
            priorities = [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("critical", "Critical")]
            for i, (_, label) in enumerate(priorities, 1):
                console.print(f"  [cyan]{i}[/cyan]. {label}")
            priority_choice = Prompt.ask("Select priority", default="2")
            try:
                priority = priorities[int(priority_choice) - 1][0]
            except (ValueError, IndexError):
                priority = "medium"

            # Due date
            console.print("\n[bold]Due date:[/bold]")
            console.print("  [cyan]1[/cyan]. Today")
            console.print("  [cyan]2[/cyan]. Tomorrow")
            console.print("  [cyan]3[/cyan]. This week (Friday)")
            console.print("  [cyan]4[/cyan]. Next week")
            console.print("  [cyan]5[/cyan]. Custom date")
            console.print("  [cyan]6[/cyan]. No deadline")
            due_choice = Prompt.ask("Select due date", default="6")

            due_date = None
            today = datetime.now().date()
            if due_choice == "1":
                due_date = today.isoformat()
            elif due_choice == "2":
                due_date = (today + timedelta(days=1)).isoformat()
            elif due_choice == "3":
                days_until_friday = (4 - today.weekday()) % 7
                if days_until_friday == 0:
                    days_until_friday = 7
                due_date = (today + timedelta(days=days_until_friday)).isoformat()
            elif due_choice == "4":
                due_date = (today + timedelta(days=7)).isoformat()
            elif due_choice == "5":
                custom = Prompt.ask("Enter date (YYYY-MM-DD)")
                if custom:
                    due_date = custom

            # Energy cost
            console.print("\n[bold]Energy cost:[/bold] [dim](cognitive load)[/dim]")
            energy_options = [("1", "1 - Trivial"), ("3", "3 - Low"), ("5", "5 - Medium"), ("7", "7 - High"), ("9", "9 - Exhausting")]
            for i, (_, label) in enumerate(energy_options, 1):
                console.print(f"  [cyan]{i}[/cyan]. {label}")
            energy_choice = Prompt.ask("Select energy cost", default="3")
            try:
                energy = int(energy_options[int(energy_choice) - 1][0])
            except (ValueError, IndexError):
                energy = 5

            # Project link
            project_id = None
            if projects:
                console.print("\n[bold]Link to project:[/bold]")
                project_id = prompt_with_options("Select project", projects)

            # Goal link
            goal_id = None
            if goals:
                console.print("\n[bold]Link to goal:[/bold]")
                goal_id = prompt_with_options("Select goal", goals)

            # Assign to person (primary assignee)
            assignee_email = None
            if contacts:
                console.print("\n[bold]Assign to:[/bold] [dim](primary person responsible)[/dim]")
                assignee_email = prompt_with_options("Select assignee", contacts)

            # Related people (multiple selection)
            related_emails = []
            if contacts:
                console.print("\n[bold]Related people:[/bold] [dim](others involved - requestor, collaborators)[/dim]")
                related_emails = prompt_with_multi_options("Select related people", contacts)
                # Remove assignee from related if selected
                if assignee_email and assignee_email in related_emails:
                    related_emails.remove(assignee_email)

            # Create the task
            task_service = get_task_service()
            task = await task_service.create(
                title=title,
                description=description or None,
                priority=priority,
                due_date=due_date,
                project_id=project_id,
                goal_id=goal_id,
                energy_cost=energy,
            )

            # Link to people
            async for session in get_neo4j_session():
                if assignee_email:
                    await link_task_to_person(session, task["id"], assignee_email, relationship_type="ASSIGNED_TO")
                for email in related_emails:
                    await link_task_to_person(session, task["id"], email, relationship_type="INVOLVES")
                break

            # Summary
            console.print(f"\n[green]✓ Created task:[/green] {task['title']}")
            console.print(f"  ID: [dim]{task['id']}[/dim]")
            console.print(f"  Priority: {priority}")
            if due_date:
                console.print(f"  Due: {due_date}")
            if project_id:
                proj_name = next((p[1] for p in projects if p[0] == project_id), project_id)
                console.print(f"  Project: {proj_name}")
            if goal_id:
                goal_name = next((g[1] for g in goals if g[0] == goal_id), goal_id)
                console.print(f"  Goal: {goal_name}")
            if assignee_email:
                assignee_name = next((c[1] for c in contacts if c[0] == assignee_email), assignee_email)
                console.print(f"  Assigned to: {assignee_name}")
            if related_emails:
                names = [next((c[1] for c in contacts if c[0] == e), e) for e in related_emails]
                console.print(f"  Related: {', '.join(names)}")

        finally:
            await close_neo4j()

    asyncio.run(create_task_interactive())


@app.command("project-new")
def project_new() -> None:
    """Interactive form to create a new project."""
    console.print("\n[bold cyan]Create New Project[/bold cyan]\n")

    async def create_project_interactive():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import init_graph_schema, create_project, link_project_to_goal, link_project_to_person

        await init_neo4j()
        await init_graph_schema()

        try:
            # Fetch options for linking
            async for session in get_neo4j_session():
                # Get goals
                goals_result = await session.run(
                    "MATCH (g:Goal) WHERE g.status <> 'completed' RETURN g.id as id, g.title as title ORDER BY g.title LIMIT 20"
                )
                goals = [(r["id"], r["title"]) async for r in goals_result]

                # Get contacts for owner
                contacts_result = await session.run(
                    "MATCH (p:Person) WHERE p.email IS NOT NULL RETURN p.email as email, p.name as name ORDER BY p.name LIMIT 30"
                )
                contacts = [(r["email"], r["name"] or r["email"]) async for r in contacts_result]
                break

            # Title (required)
            title = Prompt.ask("[bold]Project title[/bold]")
            if not title:
                console.print("[red]Title is required[/red]")
                return

            # Description
            description = Prompt.ask("Description [dim](optional)[/dim]", default="")

            # Status
            console.print("\n[bold]Status:[/bold]")
            statuses = [("planning", "Planning"), ("active", "Active"), ("paused", "Paused")]
            for i, (_, label) in enumerate(statuses, 1):
                console.print(f"  [cyan]{i}[/cyan]. {label}")
            status_choice = Prompt.ask("Select status", default="2")
            try:
                status = statuses[int(status_choice) - 1][0]
            except (ValueError, IndexError):
                status = "active"

            # Target date
            console.print("\n[bold]Target completion:[/bold]")
            console.print("  [cyan]1[/cyan]. End of this month")
            console.print("  [cyan]2[/cyan]. End of next month")
            console.print("  [cyan]3[/cyan]. End of quarter")
            console.print("  [cyan]4[/cyan]. Custom date")
            console.print("  [cyan]5[/cyan]. No target date")
            target_choice = Prompt.ask("Select target date", default="5")

            target_date = None
            today = datetime.now().date()
            if target_choice == "1":
                # End of this month
                next_month = today.replace(day=28) + timedelta(days=4)
                target_date = (next_month - timedelta(days=next_month.day)).isoformat()
            elif target_choice == "2":
                # End of next month
                next_month = (today.replace(day=28) + timedelta(days=4))
                month_after = (next_month.replace(day=28) + timedelta(days=4))
                target_date = (month_after - timedelta(days=month_after.day)).isoformat()
            elif target_choice == "3":
                # End of quarter
                quarter_end_month = ((today.month - 1) // 3 + 1) * 3
                quarter_end = today.replace(month=quarter_end_month, day=1) + timedelta(days=31)
                target_date = (quarter_end - timedelta(days=quarter_end.day)).isoformat()
            elif target_choice == "4":
                custom = Prompt.ask("Enter date (YYYY-MM-DD)")
                if custom:
                    target_date = custom

            # Link to goal
            goal_id = None
            if goals:
                console.print("\n[bold]Link to goal:[/bold]")
                goal_id = prompt_with_options("Select goal", goals)

            # Project owner
            owner_email = None
            if contacts:
                console.print("\n[bold]Project owner:[/bold]")
                owner_email = prompt_with_options("Select owner", contacts)

            # Stakeholders (multiple selection)
            stakeholder_emails = []
            if contacts:
                console.print("\n[bold]Stakeholders:[/bold] [dim](people involved in this project)[/dim]")
                stakeholder_emails = prompt_with_multi_options("Select stakeholders", contacts)
                # Remove owner from stakeholders if selected
                if owner_email and owner_email in stakeholder_emails:
                    stakeholder_emails.remove(owner_email)

            # Create the project
            import uuid
            project_id = f"proj_{uuid.uuid4().hex[:12]}"

            async for session in get_neo4j_session():
                project = await create_project(
                    session,
                    project_id=project_id,
                    title=title,
                    description=description or None,
                    status=status,
                    target_date=target_date,
                )

                # Link to goal if selected
                if goal_id:
                    await link_project_to_goal(session, project["id"], goal_id)

                # Link to owner
                if owner_email:
                    await link_project_to_person(session, project["id"], owner_email, role="owner")

                # Link to stakeholders
                for email in stakeholder_emails:
                    await link_project_to_person(session, project["id"], email, role="stakeholder")

                # Summary
                console.print(f"\n[green]✓ Created project:[/green] {project['title']}")
                console.print(f"  ID: [dim]{project['id']}[/dim]")
                console.print(f"  Status: {status}")
                if target_date:
                    console.print(f"  Target: {target_date}")
                if goal_id:
                    goal_name = next((g[1] for g in goals if g[0] == goal_id), goal_id)
                    console.print(f"  Goal: {goal_name}")
                if owner_email:
                    owner_name = next((c[1] for c in contacts if c[0] == owner_email), owner_email)
                    console.print(f"  Owner: {owner_name}")
                if stakeholder_emails:
                    names = [next((c[1] for c in contacts if c[0] == e), e) for e in stakeholder_emails]
                    console.print(f"  Stakeholders: {', '.join(names)}")
                break

        finally:
            await close_neo4j()

    asyncio.run(create_project_interactive())


@app.command("task-done")
def task_done(
    task_id: str = typer.Argument(..., help="Task ID (short # or full ID) to mark as done"),
) -> None:
    """Mark a task as completed. Accepts short IDs from 'tasks' list (e.g., 1, 2, 3)."""
    async def complete_task():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.db.redis import init_redis, get_redis, close_redis
        from cognitex.services.tasks import get_task_service
        from cognitex.cli.task_ids import resolve_task_id

        await init_neo4j()
        await init_redis()
        redis = get_redis()

        try:
            # Resolve short ID to full UUID
            resolved_id = await resolve_task_id(redis, task_id)
            if not resolved_id:
                console.print(f"[red]Task not found:[/red] {task_id}")
                console.print("[dim]Run 'cognitex tasks' first to see available tasks.[/dim]")
                return

            task_service = get_task_service()
            task = await task_service.complete(resolved_id)

            if task:
                console.print(f"[green]✓[/green] Completed: {task['title']}")
            else:
                console.print(f"[red]Task not found:[/red] {resolved_id}")

        finally:
            await close_redis()
            await close_neo4j()

    asyncio.run(complete_task())


@app.command("task-update")
def task_update(
    task_id: str = typer.Argument(..., help="Task ID (short # or full ID) to update"),
    title: str = typer.Option(None, "--title", "-t", help="New title"),
    status: str = typer.Option(None, "--status", "-s", help="New status: pending, in_progress, done"),
    priority: str = typer.Option(None, "--priority", "-p", help="New priority: low, medium, high, critical"),
    due: str = typer.Option(None, "--due", help="New due date (ISO format)"),
    effort: float = typer.Option(None, "--effort", "-e", help="Effort estimate in hours"),
    energy: str = typer.Option(None, "--energy", help="Energy cost: low, medium, high"),
) -> None:
    """Update an existing task. Accepts short IDs from 'tasks' list."""
    async def update_task():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.db.redis import init_redis, get_redis, close_redis
        from cognitex.services.tasks import get_task_service
        from cognitex.cli.task_ids import resolve_task_id

        await init_neo4j()
        await init_redis()
        redis = get_redis()

        try:
            # Resolve short ID to full UUID
            resolved_id = await resolve_task_id(redis, task_id)
            if not resolved_id:
                console.print(f"[red]Task not found:[/red] {task_id}")
                console.print("[dim]Run 'cognitex tasks' first to see available tasks.[/dim]")
                return

            task_service = get_task_service()
            task = await task_service.update(
                task_id=resolved_id,
                title=title,
                status=status,
                priority=priority,
                due_date=due,
                effort_estimate=effort,
                energy_cost=energy,
            )

            if task:
                console.print(f"[green]Updated task:[/green] {task['title']}")
                console.print(f"  Status: {task['status']}")
                console.print(f"  Priority: {task.get('priority', 'medium')}")
            else:
                console.print(f"[red]Task not found:[/red] {resolved_id}")

        finally:
            await close_redis()
            await close_neo4j()

    asyncio.run(update_task())


@app.command("task-show")
def task_show(
    task_id: str = typer.Argument(..., help="Task ID (short #, full ID, or title search)"),
) -> None:
    """Show detailed task information. Accepts short IDs, full IDs, or title search."""
    async def show_task():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.redis import init_redis, get_redis, close_redis
        from cognitex.services.tasks import get_task_service
        from cognitex.cli.task_ids import resolve_task_id_or_search

        await init_neo4j()
        await init_redis()
        redis = get_redis()

        try:
            # Resolve short ID, full ID, or search by title
            async for session in get_neo4j_session():
                resolved_id = await resolve_task_id_or_search(redis, task_id, session)
                break

            if not resolved_id:
                console.print(f"[red]Task not found:[/red] {task_id}")
                console.print("[dim]Try: short ID (1, 2), full ID (task_xxx), or title search.[/dim]")
                return

            task_service = get_task_service()
            task = await task_service.get(resolved_id)

            if not task:
                console.print(f"[red]Task not found:[/red] {resolved_id}")
                return

            # Header
            console.print(f"\n[bold]{task['title']}[/bold]")
            console.print(f"[dim]ID: {task['id']}[/dim]")

            # Status row
            status = task.get('status', 'pending')
            priority = task.get('priority', 'medium')
            status_color = {"pending": "yellow", "in_progress": "blue", "done": "green"}.get(status, "white")
            priority_color = {"critical": "red bold", "high": "red", "medium": "yellow", "low": "dim"}.get(priority, "white")
            console.print(f"Status: [{status_color}]{status}[/{status_color}]  |  Priority: [{priority_color}]{priority}[/{priority_color}]")

            # Dates and estimates
            date_info = []
            if task.get('due'):
                date_info.append(f"Due: {str(task['due'])[:10]}")
            if task.get('created_at'):
                date_info.append(f"Created: {str(task['created_at'])[:10]}")
            if date_info:
                console.print("  ".join(date_info))

            effort_info = []
            if task.get('effort_estimate'):
                effort_info.append(f"Effort: {task['effort_estimate']}h")
            if task.get('energy_cost'):
                effort_info.append(f"Energy: {task['energy_cost']}")
            if effort_info:
                console.print("  ".join(effort_info))

            # Description
            if task.get('description'):
                console.print(f"\n[bold]Description:[/bold]\n{task['description']}")

            # Source context - this is key for understanding where the task came from
            source_email = task.get('source_email')
            source_event = task.get('source_event')

            if source_email:
                console.print(f"\n[bold cyan]Origin: Email[/bold cyan]")
                sender = source_email.get('sender_name') or source_email.get('sender_email') or 'Unknown'
                console.print(f"  From: {sender}")
                console.print(f"  Subject: {source_email.get('subject', '(no subject)')}")
                if source_email.get('date'):
                    console.print(f"  Date: {str(source_email['date'])[:16]}")
                if source_email.get('snippet'):
                    snippet = source_email['snippet'][:200]
                    console.print(f"  [dim]{snippet}{'...' if len(source_email.get('snippet', '')) > 200 else ''}[/dim]")

            if source_event:
                console.print(f"\n[bold cyan]Origin: Calendar Event[/bold cyan]")
                console.print(f"  Event: {source_event.get('title', 'Untitled')}")
                if source_event.get('start'):
                    console.print(f"  When: {str(source_event['start'])[:16]}")

            # Project and Goal context
            if task.get('projects'):
                console.print(f"\n[bold]Project{'s' if len(task['projects']) > 1 else ''}:[/bold]")
                for proj in task['projects']:
                    status_icon = {"active": "●", "paused": "◐", "completed": "✓"}.get(proj.get('status'), "○")
                    console.print(f"  {status_icon} {proj['title']} [dim]({proj['id']})[/dim]")

            if task.get('goals'):
                console.print(f"\n[bold]Goal{'s' if len(task['goals']) > 1 else ''}:[/bold]")
                for goal in task['goals']:
                    timeframe = f" [{goal['timeframe']}]" if goal.get('timeframe') else ""
                    console.print(f"  → {goal['title']}{timeframe} [dim]({goal['id']})[/dim]")

            # People
            if task.get('people'):
                console.print(f"\n[bold]People:[/bold]")
                for person in task['people']:
                    name = person.get('name') or person.get('email')
                    role = person.get('role') or 'assignee'
                    console.print(f"  - {name} ({role})")

            # Linked documents
            if task.get('documents'):
                console.print(f"\n[bold]Documents:[/bold]")
                for doc in task['documents'][:5]:
                    name = doc.get('name', doc.get('drive_id'))
                    console.print(f"  📄 {name}")

            # Linked code
            if task.get('codefiles'):
                console.print(f"\n[bold]Code Files:[/bold]")
                for cf in task['codefiles'][:5]:
                    repo = f" ({cf['repo']})" if cf.get('repo') else ""
                    lang = f" [{cf['language']}]" if cf.get('language') else ""
                    console.print(f"  📝 {cf.get('path', cf.get('name'))}{lang}{repo}")

            # Blockers
            if task.get('blocked_by'):
                console.print(f"\n[yellow bold]⚠ Blocked by:[/yellow bold]")
                for blocker in task['blocked_by']:
                    status_icon = {"done": "✓", "in_progress": "◐"}.get(blocker.get('status'), "○")
                    console.print(f"  [{status_icon}] {blocker['title']} [dim]({blocker['id']})[/dim]")

        finally:
            await close_redis()
            await close_neo4j()

    asyncio.run(show_task())


@app.command("task-link")
def task_link(
    task_id: str = typer.Argument(..., help="Task ID (short # or full ID)"),
    project: str = typer.Option(None, "--project", "-p", help="Project ID to link"),
    goal: str = typer.Option(None, "--goal", "-g", help="Goal ID to link"),
    document: str = typer.Option(None, "--doc", "-d", help="Drive document ID to link"),
    blocked_by: str = typer.Option(None, "--blocked-by", "-b", help="Task ID that blocks this task"),
    person: str = typer.Option(None, "--person", help="Email of person to assign"),
) -> None:
    """Link a task to projects, goals, documents, people, or other tasks."""
    async def link_task():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.redis import init_redis, get_redis, close_redis
        from cognitex.db import graph_schema as gs
        from cognitex.services.tasks import get_task_service
        from cognitex.cli.task_ids import resolve_task_id

        await init_neo4j()
        await init_redis()
        redis = get_redis()

        try:
            # Resolve task short ID
            resolved_task_id = await resolve_task_id(redis, task_id)
            if not resolved_task_id:
                console.print(f"[red]Task not found:[/red] {task_id}")
                console.print("[dim]Run 'cognitex tasks' first to see available tasks.[/dim]")
                return

            # Resolve blocked_by short ID if provided
            resolved_blocked_by = None
            if blocked_by:
                resolved_blocked_by = await resolve_task_id(redis, blocked_by)
                if not resolved_blocked_by:
                    console.print(f"[yellow]Warning: Blocking task not found:[/yellow] {blocked_by}")

            task_service = get_task_service()
            linked = []

            if project:
                if await task_service.link_to_project(resolved_task_id, project):
                    linked.append(f"Project: {project}")

            if goal:
                if await task_service.link_to_goal(resolved_task_id, goal):
                    linked.append(f"Goal: {goal}")

            if document:
                if await task_service.link_to_document(resolved_task_id, document):
                    linked.append(f"Document: {document}")

            if resolved_blocked_by:
                if await task_service.set_blocked_by(resolved_task_id, resolved_blocked_by):
                    linked.append(f"Blocked by: {resolved_blocked_by}")

            if person:
                async for session in get_neo4j_session():
                    if await gs.link_task_to_person(session, resolved_task_id, person, relationship_type="ASSIGNED_TO"):
                        linked.append(f"Person: {person}")
                    break

            if linked:
                console.print(f"[green]Linked task:[/green]")
                for link in linked:
                    console.print(f"  → {link}")
            else:
                console.print("[yellow]No links specified. Use --project, --goal, --doc, --blocked-by, or --person[/yellow]")

        finally:
            await close_redis()
            await close_neo4j()

    asyncio.run(link_task())


@app.command("project-add")
def project_add(
    title: str = typer.Argument(..., help="Project title"),
    description: str = typer.Option(None, "--desc", "-d", help="Project description"),
    status: str = typer.Option("active", "--status", "-s", help="Status: planning, active, paused, completed"),
    target: str = typer.Option(None, "--target", help="Target completion date (ISO format)"),
    goal: str = typer.Option(None, "--goal", help="Goal ID to link to"),
) -> None:
    """Create a new project."""
    async def create_project():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.db.graph_schema import init_graph_schema
        from cognitex.services.tasks import get_project_service

        await init_neo4j()
        await init_graph_schema()

        try:
            project_service = get_project_service()
            project = await project_service.create(
                title=title,
                description=description,
                status=status,
                target_date=target,
                goal_id=goal,
            )

            console.print(f"[green]Created project:[/green] {project['id']}")
            console.print(f"  Title: {project['title']}")
            console.print(f"  Status: {project['status']}")
            if target:
                console.print(f"  Target: {target}")

        finally:
            await close_neo4j()

    asyncio.run(create_project())


@app.command()
def projects(
    status: str = typer.Option(None, "--status", "-s", help="Filter by status"),
    archived: bool = typer.Option(False, "--archived", "-a", help="Include archived projects"),
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum results"),
) -> None:
    """List projects."""
    async def list_projects():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.db.redis import init_redis, get_redis, close_redis
        from cognitex.services.tasks import get_project_service
        from cognitex.cli.short_ids import store_project_ids

        await init_neo4j()
        await init_redis()

        try:
            project_service = get_project_service()
            project_list = await project_service.list(
                status=status,
                include_archived=archived,
                limit=limit,
            )

            if not project_list:
                console.print("[yellow]No projects found.[/yellow]")
                return

            # Store short IDs
            redis = get_redis()
            project_ids = [p['id'] for p in project_list]
            await store_project_ids(redis, project_ids)

            table = Table(title=f"Projects ({len(project_list)})")
            table.add_column("#", style="cyan", width=3)
            table.add_column("Title", style="white", width=32)
            table.add_column("Path", style="dim", width=20)
            table.add_column("Status", style="green", width=10)
            table.add_column("Tasks", style="yellow", width=8)
            table.add_column("Target", style="magenta", width=12)

            for i, project in enumerate(project_list, 1):
                task_count = project.get('task_count', 0)
                done_count = project.get('done_count', 0)
                task_str = f"{done_count}/{task_count}"

                target = project.get('target_date')
                target_str = str(target)[:10] if target else "-"

                local_path = project.get('local_path') or "-"
                if len(local_path) > 20:
                    local_path = "..." + local_path[-17:]

                table.add_row(
                    str(i),
                    project['title'][:32],
                    local_path,
                    project.get('status', 'active'),
                    task_str,
                    target_str,
                )

            console.print(table)
            console.print("\n[dim]Use short IDs: project-show 1, project-link 2 --goal ...[/dim]")

        finally:
            await close_redis()
            await close_neo4j()

    asyncio.run(list_projects())


@app.command("project-show")
def project_show(
    project_id: str = typer.Argument(..., help="Project ID (short # or full ID)"),
    with_tasks: bool = typer.Option(False, "--tasks", "-t", help="Show project tasks"),
) -> None:
    """Show detailed project information."""
    async def show_project():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.db.redis import init_redis, get_redis, close_redis
        from cognitex.services.tasks import get_project_service
        from cognitex.cli.short_ids import resolve_project_id

        await init_neo4j()
        await init_redis()
        redis = get_redis()

        try:
            # Resolve short ID to full UUID
            resolved_id = await resolve_project_id(redis, project_id)
            if not resolved_id:
                console.print(f"[red]Project not found:[/red] {project_id}")
                console.print("[dim]Run 'cognitex projects' first to see available projects.[/dim]")
                return

            project_service = get_project_service()
            project = await project_service.get(resolved_id)

            if not project:
                console.print(f"[red]Project not found:[/red] {project_id}")
                return

            console.print(f"\n[bold]{project['title']}[/bold]")
            console.print(f"ID: [cyan]{project['id']}[/cyan]")
            console.print(f"Status: {project.get('status', 'active')}")

            if project.get('description'):
                console.print(f"\n{project['description']}")

            if project.get('target_date'):
                console.print(f"\nTarget: {project['target_date']}")

            console.print(f"\nTasks: {project.get('task_count', 0)} ({project.get('done_count', 0)} done)")

            if project.get('goal'):
                console.print(f"Goal: {project['goal']}")

            if project.get('repositories'):
                console.print(f"\nRepositories: {len(project['repositories'])}")
                for repo in project['repositories'][:5]:
                    console.print(f"  - {repo['full_name']}")

            if project.get('related_projects'):
                console.print(f"\nRelated projects: {len(project['related_projects'])}")
                for rp in project['related_projects'][:5]:
                    console.print(f"  - {rp['title']} ({rp['id']})")

            if with_tasks:
                tasks = await project_service.get_tasks(resolved_id, include_done=True)
                if tasks:
                    console.print(f"\n[bold]Tasks:[/bold]")
                    for task in tasks:
                        status_icon = {
                            "pending": "[yellow]○[/yellow]",
                            "in_progress": "[blue]◐[/blue]",
                            "done": "[green]●[/green]",
                        }.get(task.get("status", "pending"), "○")
                        console.print(f"  {status_icon} {task['title'][:50]} ({task['id']})")

        finally:
            await close_redis()
            await close_neo4j()

    asyncio.run(show_project())


@app.command("project-link")
def project_link(
    project_id: str = typer.Argument(..., help="Project ID (short # or full ID)"),
    goal: str = typer.Option(None, "--goal", "-g", help="Goal ID to link"),
    owner: str = typer.Option(None, "--owner", "-o", help="Owner email"),
    stakeholder: str = typer.Option(None, "--stakeholder", "-s", help="Stakeholder email"),
) -> None:
    """Link a project to goals and people."""
    async def link_project():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.redis import init_redis, get_redis, close_redis
        from cognitex.db.graph_schema import link_project_to_goal, link_project_to_person
        from cognitex.cli.short_ids import resolve_project_id

        await init_neo4j()
        await init_redis()
        redis = get_redis()

        try:
            # Resolve project short ID
            resolved_id = await resolve_project_id(redis, project_id)
            if not resolved_id:
                console.print(f"[red]Project not found:[/red] {project_id}")
                console.print("[dim]Run 'cognitex projects' first to see available projects.[/dim]")
                return

            linked = []

            async for session in get_neo4j_session():
                if goal:
                    await link_project_to_goal(session, resolved_id, goal)
                    linked.append(f"Goal: {goal}")

                if owner:
                    await link_project_to_person(session, resolved_id, owner, role="owner")
                    linked.append(f"Owner: {owner}")

                if stakeholder:
                    await link_project_to_person(session, resolved_id, stakeholder, role="stakeholder")
                    linked.append(f"Stakeholder: {stakeholder}")
                break

            if linked:
                console.print(f"[green]Linked project:[/green]")
                for link in linked:
                    console.print(f"  → {link}")
            else:
                console.print("[yellow]No links specified. Use --goal, --owner, or --stakeholder[/yellow]")

        finally:
            await close_redis()
            await close_neo4j()

    asyncio.run(link_project())


@app.command("goal-add")
def goal_add(
    title: str = typer.Argument(..., help="Goal title"),
    description: str = typer.Option(None, "--desc", "-d", help="Goal description"),
    timeframe: str = typer.Option(None, "--timeframe", "-t", help="Timeframe: quarterly, yearly, multi_year"),
    parent: str = typer.Option(None, "--parent", help="Parent goal ID"),
) -> None:
    """Create a new goal."""
    async def create_goal():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.db.graph_schema import init_graph_schema
        from cognitex.services.tasks import get_goal_service

        await init_neo4j()
        await init_graph_schema()

        try:
            goal_service = get_goal_service()
            goal = await goal_service.create(
                title=title,
                description=description,
                timeframe=timeframe,
                parent_goal_id=parent,
            )

            console.print(f"[green]Created goal:[/green] {goal['id']}")
            console.print(f"  Title: {goal['title']}")
            if timeframe:
                console.print(f"  Timeframe: {timeframe}")

        finally:
            await close_neo4j()

    asyncio.run(create_goal())


@app.command("goal-parse")
def goal_parse(
    description: str = typer.Argument(..., help="Goal description to parse"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show what would be created without creating"),
    no_projects: bool = typer.Option(False, "--no-projects", help="Don't create extracted projects"),
    no_tasks: bool = typer.Option(False, "--no-tasks", help="Don't create extracted tasks"),
    no_people: bool = typer.Option(False, "--no-people", help="Don't link mentioned people"),
) -> None:
    """Parse a goal description and create structured graph entities.

    Uses AI to extract projects, tasks, people, and themes from a natural
    language goal description and creates them in the semantic graph.

    Example:
        cognitex goal-parse "Build a personal AI assistant that manages my emails,
        calendar and tasks. Scott will help with the backend, target Q1 2025."
    """
    async def parse_goal():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.db.graph_schema import init_graph_schema
        from cognitex.services.goal_parser import parse_and_create_goal

        await init_neo4j()
        await init_graph_schema()

        try:
            console.print(f"\n[bold cyan]Parsing goal...[/bold cyan]")
            console.print(f"[dim]{description[:100]}{'...' if len(description) > 100 else ''}[/dim]\n")

            result = await parse_and_create_goal(
                description,
                create_projects=not no_projects,
                create_tasks=not no_tasks,
                link_people=not no_people,
                dry_run=dry_run,
            )

            parsed = result["parsed"]

            # Show parsed structure
            console.print(f"[bold]Extracted Structure[/bold] [dim](confidence: {parsed['confidence']:.0%})[/dim]\n")
            console.print(f"  [bold]Title:[/bold] {parsed['title']}")
            if parsed['timeframe']:
                console.print(f"  [bold]Timeframe:[/bold] {parsed['timeframe']}")
            if parsed['target_date']:
                console.print(f"  [bold]Target:[/bold] {parsed['target_date']}")

            if parsed['themes']:
                console.print(f"  [bold]Themes:[/bold] {', '.join(parsed['themes'])}")

            if parsed['projects']:
                console.print(f"\n  [bold]Projects ({len(parsed['projects'])}):[/bold]")
                for p in parsed['projects']:
                    console.print(f"    • {p['title']}")
                    if p.get('description'):
                        console.print(f"      [dim]{p['description'][:60]}...[/dim]")

            if parsed['tasks']:
                console.print(f"\n  [bold]Tasks ({len(parsed['tasks'])}):[/bold]")
                for t in parsed['tasks']:
                    priority = t.get('priority', 'medium')
                    console.print(f"    • {t['title']} [{priority}]")

            if parsed['people']:
                console.print(f"\n  [bold]People ({len(parsed['people'])}):[/bold]")
                for p in parsed['people']:
                    role = p.get('role', 'stakeholder')
                    name = p.get('name', p.get('email_hint', 'unknown'))
                    console.print(f"    • {name} ({role})")

            if parsed['success_criteria']:
                console.print(f"\n  [bold]Success Criteria:[/bold]")
                for c in parsed['success_criteria']:
                    console.print(f"    ✓ {c}")

            # Show what was created
            if dry_run:
                console.print(f"\n[yellow]Dry run - nothing created.[/yellow]")
            else:
                created = result["created"]
                console.print(f"\n[green]Created:[/green]")
                if created['goal']:
                    console.print(f"  Goal: {created['goal']['id']}")
                if created['projects']:
                    console.print(f"  Projects: {len(created['projects'])}")
                if created['tasks']:
                    console.print(f"  Tasks: {len(created['tasks'])}")
                if created['people_linked']:
                    console.print(f"  People linked: {len(created['people_linked'])}")

        finally:
            await close_neo4j()

    asyncio.run(parse_goal())


@app.command()
def goals(
    status: str = typer.Option(None, "--status", "-s", help="Filter by status: active, achieved, abandoned"),
    timeframe: str = typer.Option(None, "--timeframe", "-t", help="Filter by timeframe"),
    achieved: bool = typer.Option(False, "--achieved", "-a", help="Include achieved goals"),
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum results"),
) -> None:
    """List goals."""
    async def list_goals():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.services.tasks import get_goal_service

        await init_neo4j()

        try:
            goal_service = get_goal_service()
            goal_list = await goal_service.list(
                status=status,
                timeframe=timeframe,
                include_achieved=achieved,
                limit=limit,
            )

            if not goal_list:
                console.print("[yellow]No goals found.[/yellow]")
                return

            table = Table(title=f"Goals ({len(goal_list)})")
            table.add_column("ID", style="cyan", width=16)
            table.add_column("Title", style="white", width=35)
            table.add_column("Timeframe", style="green", width=10)
            table.add_column("Status", style="yellow", width=10)
            table.add_column("Projects", style="magenta", width=8)

            for goal in goal_list:
                table.add_row(
                    goal['id'],
                    goal['title'][:35],
                    goal.get('timeframe') or "-",
                    goal.get('status', 'active'),
                    str(goal.get('project_count', 0)),
                )

            console.print(table)

        finally:
            await close_neo4j()

    asyncio.run(list_goals())


@app.command("goal-show")
def goal_show(
    goal_id: str = typer.Argument(..., help="Goal ID to show"),
    with_projects: bool = typer.Option(False, "--projects", "-p", help="Show linked projects"),
) -> None:
    """Show detailed goal information."""
    async def show_goal():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.services.tasks import get_goal_service

        await init_neo4j()

        try:
            goal_service = get_goal_service()
            goal = await goal_service.get(goal_id)

            if not goal:
                console.print(f"[red]Goal not found:[/red] {goal_id}")
                return

            console.print(f"\n[bold]{goal['title']}[/bold]")
            console.print(f"ID: [cyan]{goal['id']}[/cyan]")
            console.print(f"Status: {goal.get('status', 'active')}")

            if goal.get('timeframe'):
                console.print(f"Timeframe: {goal['timeframe']}")

            if goal.get('description'):
                console.print(f"\n{goal['description']}")

            if goal.get('parent_goal'):
                console.print(f"\nParent: {goal['parent_goal']['title']} ({goal['parent_goal']['id']})")

            if goal.get('child_goals'):
                console.print(f"\nChild goals: {len(goal['child_goals'])}")
                for child in goal['child_goals'][:5]:
                    console.print(f"  - {child['title']} ({child['id']})")

            if with_projects:
                projects = await goal_service.get_projects(goal_id)
                if projects:
                    console.print(f"\n[bold]Projects:[/bold]")
                    for project in projects:
                        done = project.get('done_count', 0)
                        total = project.get('task_count', 0)
                        console.print(f"  - {project['title']} ({done}/{total} tasks)")

        finally:
            await close_neo4j()

    asyncio.run(show_goal())


@app.command("goal-achieve")
def goal_achieve(
    goal_id: str = typer.Argument(..., help="Goal ID to mark as achieved"),
) -> None:
    """Mark a goal as achieved."""
    async def achieve_goal():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.services.tasks import get_goal_service

        await init_neo4j()

        try:
            goal_service = get_goal_service()
            goal = await goal_service.achieve(goal_id)

            if goal:
                console.print(f"[green]✓[/green] Achieved: {goal['title']}")
            else:
                console.print(f"[red]Goal not found:[/red] {goal_id}")

        finally:
            await close_neo4j()

    asyncio.run(achieve_goal())


@app.command("web")
def web(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host to bind to"),
    port: int = typer.Option(8080, "--port", "-p", help="Port to listen on"),
) -> None:
    """Start the web dashboard for visual overview of tasks, projects, and goals."""
    from cognitex.web.app import run_server

    console.print(f"[bold]Starting Cognitex Dashboard[/bold]")
    console.print(f"  Open: [cyan]http://{host}:{port}[/cyan]")
    console.print(f"\n[dim]Press Ctrl+C to stop[/dim]\n")

    run_server(host=host, port=port)


@app.command("api")
def api(
    host: str = typer.Option("0.0.0.0", "--host", "-h", help="Host to bind to"),
    port: int = typer.Option(8000, "--port", "-p", help="Port to listen on"),
) -> None:
    """Start the API server for webhooks and REST endpoints.

    This server receives push notifications from Google (Gmail, Calendar, Drive)
    and routes them through Redis pub/sub to the Discord bot.

    Webhook endpoints:
      - POST /webhooks/google/gmail    - Gmail push notifications (via Pub/Sub)
      - POST /webhooks/google/calendar - Calendar change notifications
      - POST /webhooks/google/drive    - Drive change notifications

    For development, use ngrok to expose the server:
      ngrok http 8000

    Then configure WEBHOOK_BASE_URL in .env with your ngrok URL.
    """
    import uvicorn

    console.print(f"[bold]Starting Cognitex API Server[/bold]")
    console.print(f"  URL: [cyan]http://{host}:{port}[/cyan]")
    console.print(f"  Docs: [cyan]http://{host}:{port}/docs[/cyan]")
    console.print(f"\n  Webhook endpoints:")
    console.print(f"    POST /webhooks/google/gmail")
    console.print(f"    POST /webhooks/google/calendar")
    console.print(f"    POST /webhooks/google/drive")
    console.print(f"\n[dim]Press Ctrl+C to stop[/dim]\n")

    uvicorn.run("cognitex.api.main:app", host=host, port=port, reload=False)


@app.command("generate-sync-key")
def generate_sync_key() -> None:
    """Generate a secure random key for SYNC_API_KEY."""
    import secrets
    key = secrets.token_urlsafe(32)
    console.print(f"\n[bold green]Generated Sync API Key:[/bold green]")
    console.print(f"{key}\n")
    console.print("Add this to your .env file:")
    console.print(f"[cyan]SYNC_API_KEY={key}[/cyan]\n")


@app.command("agent-status")
def agent_status() -> None:
    """Show agent system status."""
    from cognitex.config import get_settings

    settings = get_settings()

    console.print("[bold]Agent Configuration[/bold]\n")

    table = Table()
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("Planner Model", settings.together_model_planner)
    table.add_row("Executor Model", settings.together_model_executor)
    table.add_row("Embedding Model", settings.together_model_embedding)
    table.add_row(
        "Together API",
        "[green]Configured[/green]" if settings.together_api_key.get_secret_value() else "[red]Missing[/red]"
    )

    console.print(table)

    # Check memory stats if possible
    async def check_memory():
        from cognitex.db.redis import init_redis, close_redis, get_redis
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from sqlalchemy import text

        try:
            await init_redis()
            await init_postgres()

            redis = get_redis()  # get_redis() is sync, returns async Redis client

            # Count working memory items
            context_exists = await redis.exists("cognitex:memory:working:context")
            pending_count = await redis.scard("cognitex:memory:working:approvals:pending")

            console.print("\n[bold]Working Memory (Redis)[/bold]")
            console.print(f"  Context: {'[green]Active[/green]' if context_exists else '[dim]Empty[/dim]'}")
            console.print(f"  Pending approvals: {pending_count}")

            # Count episodic memories
            async for session in get_session():
                try:
                    result = await session.execute(text(
                        "SELECT memory_type, COUNT(*) FROM agent_memory GROUP BY memory_type"
                    ))
                    rows = result.fetchall()

                    console.print("\n[bold]Episodic Memory (Postgres)[/bold]")
                    if rows:
                        for row in rows:
                            console.print(f"  {row[0]}: {row[1]}")
                    else:
                        console.print("  [dim]No memories stored yet[/dim]")
                except Exception:
                    console.print("\n[bold]Episodic Memory (Postgres)[/bold]")
                    console.print("  [dim]Table not initialized yet[/dim]")

        except Exception as e:
            console.print(f"\n[yellow]Could not check memory status: {e}[/yellow]")
        finally:
            try:
                await close_redis()
                await close_postgres()
            except Exception:
                pass

    asyncio.run(check_memory())


@app.command("check-replies")
def check_replies(
    days: int = typer.Option(1, "--days", "-d", help="Number of days to look back"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be completed without doing it"),
) -> None:
    """Check recent sent emails for task auto-completion.

    Scans your recent sent emails to find replies to email threads that have
    associated tasks. Uses AI to determine if your reply completes the task.
    """
    async def check_for_completions():
        from datetime import datetime, timedelta
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.services.gmail import GmailService, extract_email_metadata
        from cognitex.services.ingestion import (
            get_user_email,
            check_sent_email_for_task_completion,
            auto_complete_tasks_from_reply,
        )
        from cognitex.db.graph_schema import get_tasks_by_email_thread

        await init_neo4j()

        try:
            # Get user's email address
            user_email = await get_user_email()
            if not user_email:
                console.print("[red]Could not determine your email address.[/red]")
                return

            console.print(f"[dim]Checking sent emails for: {user_email}[/dim]")

            # Fetch recent sent emails
            gmail = GmailService()
            cutoff = datetime.now() - timedelta(days=days)
            query = f"from:me after:{cutoff.strftime('%Y/%m/%d')}"

            console.print(f"[dim]Fetching sent emails from last {days} day(s)...[/dim]")

            result = gmail.list_messages(query=query, max_results=50)
            messages = result.get("messages", [])

            if not messages:
                console.print("[yellow]No sent emails found in the specified period.[/yellow]")
                return

            console.print(f"Found {len(messages)} sent email(s)")

            # Get full metadata for messages
            full_messages = gmail.get_message_batch([m["id"] for m in messages], format="metadata")
            emails = [extract_email_metadata(m) for m in full_messages]

            # Check each sent email for potential task completion
            tasks_found = 0
            tasks_completed = []

            for email_data in emails:
                # Verify it's from the user
                sender = email_data.get("sender_email", "").lower()
                if sender != user_email:
                    continue

                # Check for related tasks
                tasks = await check_sent_email_for_task_completion(email_data, user_email)

                if tasks:
                    tasks_found += len(tasks)
                    subject = email_data.get("subject", "(no subject)")[:50]
                    console.print(f"\n[cyan]Reply:[/cyan] {subject}")
                    console.print(f"  [dim]Thread has {len(tasks)} pending task(s)[/dim]")

                    for t in tasks:
                        console.print(f"    • {t['title']}")

                    if not dry_run:
                        # Use LLM to determine completion
                        completed = await auto_complete_tasks_from_reply(email_data, tasks)
                        tasks_completed.extend(completed)
                        if completed:
                            console.print(f"  [green]✓ Auto-completed {len(completed)} task(s)[/green]")
                    else:
                        console.print(f"  [yellow](dry run - would analyze for completion)[/yellow]")

            # Summary
            console.print(f"\n[bold]Summary:[/bold]")
            console.print(f"  Sent emails checked: {len(emails)}")
            console.print(f"  Tasks found in threads: {tasks_found}")

            if not dry_run:
                console.print(f"  Tasks auto-completed: {len(tasks_completed)}")
            else:
                console.print(f"  [yellow]Dry run - no tasks were modified[/yellow]")

        finally:
            await close_neo4j()

    asyncio.run(check_for_completions())


@app.command("classify-emails")
def classify_emails(
    days: int = typer.Option(7, "--days", "-d", help="Number of days to look back"),
    batch_size: int = typer.Option(20, "--batch-size", "-b", help="Process in batches of this size"),
    limit: int = typer.Option(500, "--limit", "-l", help="Maximum emails to process (0 for no limit)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be classified without doing it"),
    force: bool = typer.Option(False, "--force", "-f", help="Re-classify emails that already have classification"),
) -> None:
    """Classify existing emails using LLM.

    Backfills email classification for emails that were ingested without
    classification. This enables the autonomous agent to detect actionable
    emails and draft responses.
    """
    async def run_classification():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, run_query
        from cognitex.services.llm import get_llm_service

        await init_neo4j()

        try:
            # Query emails needing classification
            limit_clause = f"LIMIT {limit}" if limit > 0 else ""
            if force:
                query = f"""
                MATCH (e:Email)
                WHERE e.date >= datetime() - duration({{days: $days}})
                  AND NOT (e)-[:SENT_BY]->(:Person {{is_user: true}})
                RETURN e.gmail_id as gmail_id, e.subject as subject, e.snippet as snippet,
                       e.classification as current_classification
                ORDER BY e.date DESC
                {limit_clause}
                """
            else:
                query = f"""
                MATCH (e:Email)
                WHERE e.date >= datetime() - duration({{days: $days}})
                  AND e.classification IS NULL
                  AND NOT (e)-[:SENT_BY]->(:Person {{is_user: true}})
                RETURN e.gmail_id as gmail_id, e.subject as subject, e.snippet as snippet
                ORDER BY e.date DESC
                {limit_clause}
                """

            emails = await run_query(query, {"days": days})

            if not emails:
                console.print("[green]No emails need classification.[/green]")
                return

            console.print(f"Found [cyan]{len(emails)}[/cyan] emails to classify")

            if dry_run:
                console.print("[yellow]Dry run - showing first 10 emails:[/yellow]")
                for e in emails[:10]:
                    subj = (e.get("subject") or "(no subject)")[:60]
                    current = e.get("current_classification", "none")
                    console.print(f"  • {subj} [dim][{current}][/dim]")
                return

            # Process in batches
            llm = get_llm_service()
            classified = 0
            errors = 0

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Classifying emails...", total=len(emails))

                for i in range(0, len(emails), batch_size):
                    batch = emails[i:i + batch_size]

                    for email in batch:
                        try:
                            # Build email data for classification
                            email_data = {
                                "gmail_id": email["gmail_id"],
                                "subject": email.get("subject", ""),
                                "snippet": email.get("snippet", ""),
                                "sender_email": "",  # Not available in query
                                "sender_name": "",
                            }

                            # Classify
                            result = await llm.classify_email(email_data)

                            # Update Neo4j (need write session)
                            from cognitex.db.neo4j import get_neo4j_session
                            update_query = """
                            MATCH (e:Email {gmail_id: $gmail_id})
                            SET e.classification = $classification,
                                e.urgency = $urgency,
                                e.action_required = $action_required,
                                e.sentiment = $sentiment
                            """
                            async for write_session in get_neo4j_session(access_mode="WRITE"):
                                await write_session.run(update_query, {
                                    "gmail_id": email["gmail_id"],
                                    "classification": result.get("classification"),
                                    "urgency": result.get("urgency"),
                                    "action_required": result.get("action_required", False),
                                    "sentiment": result.get("sentiment"),
                                })
                                break

                            classified += 1
                            progress.update(task, advance=1)

                        except Exception as e:
                            errors += 1
                            progress.update(task, advance=1)
                            logger.warning(
                                "Failed to classify email",
                                gmail_id=email.get("gmail_id"),
                                error=str(e),
                            )

                    # Brief pause between batches to avoid rate limits
                    await asyncio.sleep(0.5)

            # Summary
            console.print(f"\n[bold]Classification complete:[/bold]")
            console.print(f"  Classified: [green]{classified}[/green]")
            if errors:
                console.print(f"  Errors: [red]{errors}[/red]")

            # Show classification breakdown
            stats_query = """
            MATCH (e:Email)
            WHERE e.date >= datetime() - duration({days: $days})
              AND e.classification IS NOT NULL
            RETURN e.classification as classification, COUNT(*) as count
            ORDER BY count DESC
            """
            stats = await run_query(stats_query, {"days": days})

            if stats:
                console.print(f"\n[bold]Classification breakdown (last {days} days):[/bold]")
                for s in stats:
                    console.print(f"  {s['classification']}: {s['count']}")

        finally:
            await close_neo4j()

    asyncio.run(run_classification())


@app.command("backfill-labels")
def backfill_labels(
    limit: int = typer.Option(500, "--limit", "-l", help="Maximum emails to process"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be updated"),
) -> None:
    """Backfill Gmail labels for existing emails.

    Fetches label information from Gmail API and updates Neo4j Email nodes
    with labels and is_sent status. This enables filtering to inbox-only emails.
    """
    async def run_backfill():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, run_query, get_neo4j_session
        from cognitex.services.gmail import GmailService

        await init_neo4j()

        try:
            # Get emails without labels
            query = f"""
            MATCH (e:Email)
            WHERE e.labels IS NULL OR e.labels = []
            RETURN e.gmail_id as gmail_id, e.subject as subject
            ORDER BY e.date DESC
            LIMIT {limit}
            """
            emails = await run_query(query, {})

            if not emails:
                console.print("[green]All emails already have labels.[/green]")
                return

            console.print(f"Found [cyan]{len(emails)}[/cyan] emails needing label backfill")

            if dry_run:
                console.print("[yellow]Dry run - showing first 10:[/yellow]")
                for e in emails[:10]:
                    subj = (e.get("subject") or "(no subject)")[:60]
                    console.print(f"  • {subj}")
                return

            # Initialize Gmail service
            gmail = GmailService()
            user_email = gmail.get_profile().get("emailAddress", "").lower()

            updated = 0
            errors = 0

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Fetching labels...", total=len(emails))

                for email in emails:
                    try:
                        gmail_id = email["gmail_id"]

                        # Fetch message from Gmail to get labels
                        msg = gmail.get_message(gmail_id, format="metadata")
                        if not msg:
                            progress.update(task, advance=1)
                            continue

                        labels = msg.get("labelIds", [])

                        # Determine is_sent from labels or sender
                        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
                        from_header = headers.get("from", "")
                        sender_email = ""
                        if "<" in from_header:
                            sender_email = from_header.split("<")[1].split(">")[0].lower()
                        else:
                            sender_email = from_header.lower()

                        is_sent = "SENT" in labels or sender_email == user_email

                        # Update Neo4j
                        update_query = """
                        MATCH (e:Email {gmail_id: $gmail_id})
                        SET e.labels = $labels,
                            e.is_sent = $is_sent
                        """
                        async for write_session in get_neo4j_session(access_mode="WRITE"):
                            await write_session.run(update_query, {
                                "gmail_id": gmail_id,
                                "labels": labels,
                                "is_sent": is_sent,
                            })
                            break

                        updated += 1
                        progress.update(task, advance=1)

                        # Rate limit
                        await asyncio.sleep(0.1)

                    except Exception as e:
                        errors += 1
                        progress.update(task, advance=1)
                        logger.warning(
                            "Failed to fetch labels",
                            gmail_id=email.get("gmail_id"),
                            error=str(e),
                        )

            console.print(f"\n[bold]Label backfill complete:[/bold]")
            console.print(f"  Updated: [green]{updated}[/green]")
            if errors:
                console.print(f"  Errors: [red]{errors}[/red]")

            # Show label distribution
            stats_query = """
            MATCH (e:Email)
            WHERE e.labels IS NOT NULL
            UNWIND e.labels as label
            RETURN label, COUNT(*) as count
            ORDER BY count DESC
            LIMIT 10
            """
            stats = await run_query(stats_query, {})

            if stats:
                console.print(f"\n[bold]Top 10 labels:[/bold]")
                for s in stats:
                    console.print(f"  {s['label']}: {s['count']}")

        finally:
            await close_neo4j()

    asyncio.run(run_backfill())


# ============================================================================
# GitHub Integration
# ============================================================================


@app.command("github-repos")
def github_repos(
    include_forks: bool = typer.Option(False, "--forks", "-f", help="Include forked repositories"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum repos to list"),
) -> None:
    """List your GitHub repositories."""
    from cognitex.services.github import get_github_service

    try:
        github = get_github_service()
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        console.print("[dim]Add GITHUB_TOKEN to your .env file[/dim]")
        return

    user = github.get_user()
    console.print(f"[dim]Authenticated as: {user['login']} ({user['name'] or 'N/A'})[/dim]\n")

    repos = github.list_repos(include_forks=include_forks, limit=limit)

    if not repos:
        console.print("[yellow]No repositories found.[/yellow]")
        return

    table = Table(title=f"GitHub Repositories ({len(repos)})")
    table.add_column("Name", style="cyan", width=30)
    table.add_column("Language", style="green", width=12)
    table.add_column("Updated", style="yellow", width=12)
    table.add_column("Private", style="dim", width=7)

    for repo in repos:
        updated = repo["pushed_at"][:10] if repo["pushed_at"] else "-"
        table.add_row(
            repo["full_name"],
            repo["language"] or "-",
            updated,
            "Yes" if repo["is_private"] else "No",
        )

    console.print(table)
    console.print("\n[dim]Sync a repo: cognitex github-sync owner/repo[/dim]")


@app.command("github-sync")
def github_sync(
    repo_name: str = typer.Argument(..., help="Repository to sync (e.g., 'owner/repo')"),
    index_code: bool = typer.Option(True, "--index/--no-index", help="Index code files for semantic search"),
    link_project: str = typer.Option(None, "--project", "-p", help="Link to existing project ID"),
    skip_embeddings: bool = typer.Option(False, "--skip-embeddings", help="Store code but skip embedding generation"),
) -> None:
    """Sync a GitHub repository to the graph and optionally index code."""
    async def sync_repo():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.services.github import get_github_service
        from cognitex.db.graph_schema import (
            create_repository,
            create_codefile,
            link_project_to_repository,
            get_repository,
        )

        await init_neo4j()

        try:
            github = get_github_service()
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            return

        # Get repo info
        console.print(f"[dim]Fetching repository info for {repo_name}...[/dim]")
        repo = github.get_repo(repo_name)

        if not repo:
            console.print(f"[red]Repository not found: {repo_name}[/red]")
            return

        console.print(f"[green]Found:[/green] {repo['full_name']}")
        console.print(f"  Language: {repo['language'] or 'N/A'}")
        console.print(f"  Description: {(repo['description'] or 'No description')[:60]}")

        # Create repository node
        async for session in get_neo4j_session():
            await create_repository(
                session,
                repo_id=repo["id"],
                name=repo["name"],
                full_name=repo["full_name"],
                url=repo["url"],
                description=repo["description"],
                primary_language=repo["language"],
                default_branch=repo["default_branch"],
            )
            console.print("[green]✓[/green] Repository node created")

            # Link to project if specified
            if link_project:
                success = await link_project_to_repository(session, link_project, repo["id"])
                if success:
                    console.print(f"[green]✓[/green] Linked to project: {link_project}")
                else:
                    console.print(f"[yellow]Warning: Could not link to project {link_project}[/yellow]")
            break

        # Index code files
        if index_code:
            console.print("\n[dim]Scanning repository for code files...[/dim]")

            files = list(github.get_indexable_files(repo_name))
            console.print(f"Found {len(files)} files to index")

            if files:
                await init_postgres()

                indexed_count = 0
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TaskProgressColumn(),
                    console=console,
                ) as progress:
                    task = progress.add_task("Indexing files...", total=len(files))

                    for file_info in files:
                        progress.update(task, description=f"Indexing {file_info['name'][:30]}...")

                        # Get file content
                        content = github.get_file_content(repo_name, file_info["path"])

                        if content:
                            # Create CodeFile node
                            async for session in get_neo4j_session():
                                file_id = f"{repo['id']}:{file_info['path']}"
                                await create_codefile(
                                    session,
                                    codefile_id=file_id,
                                    path=file_info["path"],
                                    name=file_info["name"],
                                    repository_id=repo["id"],
                                    language=_detect_language(file_info["name"]),
                                )
                                break

                            # Store content and optionally generate embedding
                            try:
                                async for pg_session in get_session():
                                    from cognitex.services.ingestion import index_code_content
                                    await index_code_content(
                                        pg_session,
                                        file_id=file_id,
                                        path=file_info["path"],
                                        content=content,
                                        repo_name=repo_name,
                                        skip_embedding=skip_embeddings,
                                    )
                                    break
                                indexed_count += 1
                            except Exception as e:
                                logger.debug("Failed to index file", path=file_info["path"], error=str(e))

                        progress.advance(task)

                if skip_embeddings:
                    console.print(f"[green]✓[/green] Indexed {indexed_count} files (embeddings skipped)")
                else:
                    console.print(f"[green]✓[/green] Indexed {indexed_count} files with embeddings")

                await close_postgres()

        await close_neo4j()

        console.print(f"\n[bold green]Repository synced successfully![/bold green]")

    asyncio.run(sync_repo())


@app.command("sessions-sync")
def sessions_sync(
    cli: str = typer.Option("claude", "--cli", "-c", help="CLI tool to sync from (claude)"),
    project: str = typer.Option(None, "--project", "-p", help="Limit to specific project path"),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-sync of all sessions"),
) -> None:
    """Sync coding CLI sessions into the knowledge graph.

    Ingests sessions from AI coding assistants (Claude Code, etc.) to provide
    rich context about project development progress, decisions, and next steps.
    """
    async def run_sync():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.db.graph_schema import init_graph_schema
        from cognitex.services.coding_sessions import get_session_ingester

        console.print(f"\n[bold]Syncing {cli} coding sessions...[/bold]\n")

        await init_neo4j()
        await init_graph_schema()

        ingester = get_session_ingester()

        # Discover sessions
        sessions = await ingester.discover_sessions(cli)

        if project:
            sessions = [s for s in sessions if project in s["project_path"]]

        if not sessions:
            console.print("[yellow]No sessions found.[/yellow]")
            return

        console.print(f"Found {len(sessions)} sessions to process\n")

        # Show session table
        table = Table(title="Discovered Sessions")
        table.add_column("Session ID", style="cyan")
        table.add_column("Project", style="green")
        table.add_column("Modified", style="dim")
        table.add_column("Size", justify="right")

        for s in sessions[:20]:  # Show first 20
            table.add_row(
                s["session_id"][:12],
                s["project_path"].split("/")[-1],
                s["modified_at"].strftime("%Y-%m-%d %H:%M"),
                f"{s['size_bytes'] // 1024}KB",
            )
        console.print(table)

        if len(sessions) > 20:
            console.print(f"[dim]... and {len(sessions) - 20} more[/dim]\n")

        # Sync with progress
        console.print()
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Syncing sessions...", total=len(sessions))

            ingested = 0
            for session_meta in sessions:
                progress.update(task, description=f"Processing {session_meta['session_id'][:8]}...")

                if force:
                    ingester._processed_sessions.discard(session_meta["session_id"])

                result = await ingester.ingest_session(session_meta, force=force)
                if result:
                    ingested += 1

                progress.advance(task)

        await close_neo4j()

        console.print(f"\n[bold green]✓ Synced {ingested} coding sessions![/bold green]")
        console.print("[dim]Sessions are linked to projects and can provide development context.[/dim]")

    asyncio.run(run_sync())


@app.command("sessions-search")
def sessions_search(
    query: str = typer.Argument(..., help="Keyword or semantic search for coding sessions"),
    project: str = typer.Option(None, "--project", "-p", help="Filter by project"),
    limit: int = typer.Option(10, "--limit", "-l", help="Number of sessions"),
) -> None:
    """Search coding sessions for specific decisions or topics."""

    async def run_search():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session

        await init_neo4j()

        try:
            async for session in get_neo4j_session():
                # Neo4j simple string matching on summary, decisions, and topics
                cypher = """
                MATCH (cs:CodingSession)
                WHERE toLower(cs.summary) CONTAINS toLower($query)
                   OR any(d IN cs.decisions WHERE toLower(d) CONTAINS toLower($query))
                   OR any(t IN cs.topics WHERE toLower(t) CONTAINS toLower($query))
                """
                if project:
                    cypher += " AND cs.project_path CONTAINS $project"

                cypher += """
                RETURN cs.session_id as id, cs.summary as summary,
                       cs.project_path as path, cs.ended_at as date,
                       cs.decisions as decisions
                ORDER BY cs.ended_at DESC
                LIMIT $limit
                """

                result = await session.run(
                    cypher, query=query, project=project or "", limit=limit
                )
                records = await result.data()

                if not records:
                    console.print("[yellow]No sessions found matching query.[/yellow]")
                    return

                console.print(f"\n[bold]Found {len(records)} sessions:[/bold]\n")

                for r in records:
                    date_str = str(r["date"])[:16] if r["date"] else "unknown"
                    project_name = r["path"].split("/")[-1] if r["path"] else "unknown"
                    session_id = r["id"][:8] if r["id"] else "?"
                    console.print(
                        f"[cyan]{date_str}[/cyan] [green]{project_name}[/green] [dim]({session_id})[/dim]"
                    )
                    if r["summary"]:
                        console.print(f"  {r['summary']}")
                    if r["decisions"]:
                        console.print(f"  [dim]Decisions: {r['decisions'][0]}[/dim]")
                    console.print("")

        finally:
            await close_neo4j()

    asyncio.run(run_search())


@app.command("sessions-context")
def sessions_context(
    project: str = typer.Argument(..., help="Project name to get context for"),
    limit: int = typer.Option(5, "--limit", "-l", help="Number of recent sessions"),
) -> None:
    """Show development context from recent coding sessions for a project."""
    async def show_context():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.services.coding_sessions import get_session_ingester

        await init_neo4j()

        ingester = get_session_ingester()
        sessions = await ingester.get_project_development_context(project, limit=limit)

        if not sessions:
            console.print(f"[yellow]No coding sessions found for project '{project}'[/yellow]")
            console.print("[dim]Try running: cognitex sessions-sync first[/dim]")
            await close_neo4j()
            return

        console.print(f"\n[bold]Development Context for {project}[/bold]\n")

        for i, s in enumerate(sessions, 1):
            console.print(f"[bold cyan]Session {i}:[/bold cyan] {s.get('slug', s.get('session_id', 'unknown')[:8])}")
            console.print(f"  [dim]Branch:[/dim] {s.get('git_branch', 'unknown')}")
            console.print(f"  [dim]State:[/dim] {s.get('completion_state', 'unknown')}")

            if s.get("summary"):
                console.print(f"\n  [bold]Summary:[/bold] {s['summary']}")

            if s.get("decisions"):
                console.print("\n  [bold]Decisions:[/bold]")
                for d in s["decisions"][:5]:
                    console.print(f"    • {d}")

            if s.get("next_steps"):
                console.print("\n  [bold]Next Steps:[/bold]")
                for step in s["next_steps"][:5]:
                    console.print(f"    → {step}")

            console.print()

        # Aggregate next steps
        next_steps = await ingester.get_development_next_steps(project)
        if next_steps:
            console.print("[bold yellow]Aggregated Next Steps:[/bold yellow]")
            for step in next_steps[:10]:
                console.print(f"  → {step}")

        await close_neo4j()

    asyncio.run(show_context())


@app.command("github-embeddings")
def github_embeddings(
    repo: str = typer.Option(None, "--repo", "-r", help="Limit to specific repo (owner/repo)"),
    limit: int = typer.Option(0, "--limit", "-l", help="Max files to process (0 = all)"),
    max_failures: int = typer.Option(3, "--max-failures", help="Stop after N consecutive failures"),
) -> None:
    """Generate embeddings for indexed code that doesn't have them yet."""
    async def generate_embeddings():
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.services.llm import get_llm_service
        from sqlalchemy import text

        await init_postgres()
        llm = get_llm_service()

        # Find code files without embeddings
        query = text("""
            SELECT cc.file_id, cc.path, cc.content, cc.repo_name, cc.content_hash
            FROM code_content cc
            LEFT JOIN embeddings e ON e.entity_type = 'code' AND e.entity_id = cc.file_id
            WHERE e.id IS NULL
            """ + ("AND cc.repo_name = :repo" if repo else "") + """
            ORDER BY cc.indexed_at
            """ + (f"LIMIT {limit}" if limit > 0 else ""))

        async for session in get_session():
            params = {"repo": repo} if repo else {}
            result = await session.execute(query, params)
            files = result.fetchall()
            break

        if not files:
            console.print("[green]All indexed code has embeddings.[/green]")
            return

        console.print(f"Found {len(files)} files without embeddings")

        generated = 0
        failed = 0
        consecutive_failures = 0
        last_error = None

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Generating embeddings...", total=len(files))

            for file_row in files:
                file_id, path, content, repo_name, content_hash = file_row
                progress.update(task, description=f"Embedding {path[:40]}...")

                # Truncate to ~350 tokens (~1200 chars) for bge-base-en-v1.5 (512 token limit)
                embedding_text = f"File: {path}\n\n{content[:1100]}"

                try:
                    embedding = await llm.generate_embedding(embedding_text)
                    # Convert list to pgvector string format
                    embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

                    async for session in get_session():
                        # Use raw SQL with proper casting for pgvector
                        embed_query = text("""
                            INSERT INTO embeddings (entity_type, entity_id, content_hash, embedding)
                            VALUES ('code', :file_id, :content_hash, CAST(:embedding AS vector))
                            ON CONFLICT (entity_type, entity_id) DO UPDATE SET
                                content_hash = EXCLUDED.content_hash,
                                embedding = EXCLUDED.embedding,
                                created_at = NOW()
                        """)
                        await session.execute(embed_query, {
                            "file_id": file_id,
                            "content_hash": content_hash,
                            "embedding": embedding_str,
                        })
                        await session.commit()
                        break

                    generated += 1
                    consecutive_failures = 0  # Reset on success
                except Exception as e:
                    last_error = str(e)
                    logger.debug("Failed to generate embedding", file=path, error=last_error)
                    failed += 1
                    consecutive_failures += 1

                    # Fail fast if API is consistently failing
                    if consecutive_failures >= max_failures:
                        progress.stop()
                        console.print(f"\n[red]Stopping after {consecutive_failures} consecutive failures[/red]")
                        console.print(f"[red]Last error: {last_error[:200]}[/red]")
                        break

                progress.advance(task)

        await close_postgres()

        console.print(f"\n[green]✓[/green] Generated {generated} embeddings")
        if failed:
            console.print(f"[yellow]⚠[/yellow] Failed: {failed}")
            if last_error:
                console.print(f"[dim]Last error: {last_error[:150]}...[/dim]")

    asyncio.run(generate_embeddings())


@app.command("github-search")
def github_search(
    query: str = typer.Argument(..., help="Search query for code"),
    repo: str = typer.Option(None, "--repo", "-r", help="Limit search to specific repo"),
    limit: int = typer.Option(10, "--limit", "-l", help="Maximum results"),
) -> None:
    """Search indexed code using semantic similarity."""
    async def search_code():
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.services.ingestion import search_code_semantic

        await init_postgres()

        try:
            async for session in get_session():
                results = await search_code_semantic(session, query, repo_filter=repo, limit=limit)
                break

            if not results:
                console.print("[yellow]No matching code found.[/yellow]")
                return

            console.print(f"[bold]Code search results for:[/bold] {query}\n")

            for i, result in enumerate(results, 1):
                similarity = result.get("similarity", 0)
                color = "green" if similarity > 0.7 else "yellow" if similarity > 0.5 else "dim"

                console.print(f"[{color}]{i}. {result['repo_name']}[/{color}]")
                console.print(f"   [cyan]{result['path']}[/cyan]")
                console.print(f"   Similarity: {similarity:.2%}")

                # Show preview
                preview = result.get("content_preview", "")[:200]
                if preview:
                    console.print(f"   [dim]{preview}...[/dim]")
                console.print()

        finally:
            await close_postgres()

    asyncio.run(search_code())


@app.command("repo-link")
def repo_link(
    repo_name: str = typer.Argument(..., help="Repository name (owner/repo or just repo name)"),
    project: str = typer.Option(None, "--project", "-p", help="Project ID or title to link to"),
) -> None:
    """Link a repository to a project."""
    async def link_repo():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import link_project_to_repository

        await init_neo4j()

        async for session in get_neo4j_session():
            # Find the repository
            if "/" in repo_name:
                result = await session.run(
                    "MATCH (r:Repository {full_name: $name}) RETURN r.id as id, r.full_name as name",
                    name=repo_name
                )
            else:
                result = await session.run(
                    "MATCH (r:Repository) WHERE r.name = $name OR r.full_name CONTAINS $name RETURN r.id as id, r.full_name as name",
                    name=repo_name
                )
            repos = await result.data()

            if not repos:
                console.print(f"[red]Repository not found: {repo_name}[/red]")
                console.print("[dim]Use 'cognitex github-repos' to see available repos[/dim]")
                await close_neo4j()
                return

            repo = repos[0]

            # Find or select project
            if project:
                # Try to match by ID or title
                result = await session.run(
                    "MATCH (p:Project) WHERE p.id = $q OR toLower(p.title) CONTAINS toLower($q) RETURN p.id as id, p.title as title",
                    q=project
                )
                projects = await result.data()
            else:
                # List all projects for selection
                result = await session.run("MATCH (p:Project) RETURN p.id as id, p.title as title ORDER BY p.title")
                projects = await result.data()

                if not projects:
                    console.print("[yellow]No projects found. Create one first with the web dashboard.[/yellow]")
                    await close_neo4j()
                    return

                console.print(f"\n[bold]Link {repo['name']} to which project?[/bold]")
                for i, p in enumerate(projects, 1):
                    console.print(f"  [cyan]{i}[/cyan]. {p['title']}")

                choice = Prompt.ask("Select project", default="1")
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(projects):
                        projects = [projects[idx]]
                    else:
                        console.print("[red]Invalid selection[/red]")
                        await close_neo4j()
                        return
                except ValueError:
                    console.print("[red]Invalid selection[/red]")
                    await close_neo4j()
                    return

            if not projects:
                console.print(f"[red]Project not found: {project}[/red]")
                await close_neo4j()
                return

            proj = projects[0]

            # Create the link
            await link_project_to_repository(session, proj['id'], repo['id'])
            console.print(f"[green]✓[/green] Linked [bold]{repo['name']}[/bold] to project [bold]{proj['title']}[/bold]")
            break

        await close_neo4j()

    asyncio.run(link_repo())


def _detect_language(filename: str) -> str | None:
    """Detect programming language from filename."""
    ext_map = {
        ".py": "Python",
        ".js": "JavaScript",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".jsx": "JavaScript",
        ".go": "Go",
        ".rs": "Rust",
        ".java": "Java",
        ".kt": "Kotlin",
        ".rb": "Ruby",
        ".php": "PHP",
        ".c": "C",
        ".cpp": "C++",
        ".h": "C",
        ".hpp": "C++",
        ".swift": "Swift",
        ".scala": "Scala",
        ".sql": "SQL",
        ".sh": "Shell",
        ".bash": "Shell",
        ".yaml": "YAML",
        ".yml": "YAML",
        ".json": "JSON",
        ".toml": "TOML",
        ".md": "Markdown",
        ".html": "HTML",
        ".css": "CSS",
        ".scss": "SCSS",
    }
    from pathlib import Path
    ext = Path(filename).suffix.lower()
    return ext_map.get(ext)


# =============================================================================
# Decision Memory Commands
# =============================================================================

@app.command("decision-traces")
def decision_traces(
    limit: int = typer.Option(20, "--limit", "-l", help="Number of traces to show"),
    status: str = typer.Option(None, "--status", "-s", help="Filter by status (pending/approved/edited/rejected/auto_executed)"),
    action: str = typer.Option(None, "--action", "-a", help="Filter by action type"),
) -> None:
    """Show recent decision traces for behavioral learning."""
    async def show_traces():
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.agent.decision_memory import init_decision_memory

        await init_postgres()

        try:
            decision_memory = await init_decision_memory()
            traces = await decision_memory.traces.get_recent_traces(
                limit=limit,
                status=status,
                action_type=action,
            )

            if not traces:
                console.print("[yellow]No decision traces found.[/yellow]")
                return

            table = Table(title=f"Decision Traces ({len(traces)} most recent)")
            table.add_column("ID", style="dim", width=15)
            table.add_column("Trigger", style="cyan", width=12)
            table.add_column("Action", style="green", width=15)
            table.add_column("Status", width=12)
            table.add_column("Quality", justify="right", width=8)
            table.add_column("Created", style="dim", width=16)

            for t in traces:
                # Color status
                status_style = {
                    "pending": "yellow",
                    "approved": "green",
                    "auto_executed": "blue",
                    "edited": "magenta",
                    "rejected": "red",
                }.get(t["status"], "white")

                quality = f"{t['quality_score']:.2f}" if t["quality_score"] is not None else "-"

                # Format created time
                created = t["created_at"][:16] if t["created_at"] else "-"

                table.add_row(
                    t["id"][:15],
                    t["trigger_type"],
                    t["action_type"],
                    f"[{status_style}]{t['status']}[/{status_style}]",
                    quality,
                    created,
                )

            console.print(table)

            # Show summary
            console.print(f"\n[dim]Filter by status: --status pending|approved|edited|rejected|auto_executed[/dim]")

        finally:
            await close_postgres()

    asyncio.run(show_traces())


@app.command("decision-trace")
def decision_trace_detail(
    trace_id: str = typer.Argument(..., help="Trace ID to show details for"),
) -> None:
    """Show detailed information about a specific decision trace."""
    async def show_detail():
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.agent.decision_memory import init_decision_memory
        import json

        await init_postgres()

        try:
            decision_memory = await init_decision_memory()
            trace = await decision_memory.traces.get_trace(trace_id)

            if not trace:
                console.print(f"[red]Trace not found: {trace_id}[/red]")
                return

            console.print(f"\n[bold]Decision Trace: {trace['id']}[/bold]\n")

            # Basic info
            console.print(f"[cyan]Trigger:[/cyan] {trace['trigger_type']}")
            if trace.get('trigger_summary'):
                console.print(f"[cyan]Summary:[/cyan] {trace['trigger_summary']}")
            console.print(f"[cyan]Action:[/cyan] {trace['action_type']}")
            console.print(f"[cyan]Status:[/cyan] {trace['status']}")
            if trace.get('quality_score') is not None:
                console.print(f"[cyan]Quality Score:[/cyan] {trace['quality_score']:.2f}")

            # Reasoning
            if trace.get('reasoning'):
                console.print(f"\n[bold]Reasoning:[/bold]")
                console.print(f"  {trace['reasoning']}")

            # Proposed action
            if trace.get('proposed_action'):
                console.print(f"\n[bold]Proposed Action:[/bold]")
                console.print(json.dumps(trace['proposed_action'], indent=2, default=str))

            # Final action (if different)
            if trace.get('final_action') and trace['final_action'] != trace.get('proposed_action'):
                console.print(f"\n[bold]Final Action (after edits):[/bold]")
                console.print(json.dumps(trace['final_action'], indent=2, default=str))

            # Feedback
            if trace.get('explicit_feedback'):
                console.print(f"\n[bold]User Feedback:[/bold]")
                console.print(f"  {trace['explicit_feedback']}")

            # Context (truncated)
            if trace.get('context'):
                console.print(f"\n[bold]Context:[/bold] [dim](truncated)[/dim]")
                ctx = trace['context']
                if isinstance(ctx, str):
                    import json as json_module
                    try:
                        ctx = json_module.loads(ctx)
                    except:
                        pass
                console.print(json.dumps(ctx, indent=2, default=str)[:500] + "...")

            # Timestamps
            console.print(f"\n[dim]Created: {trace.get('created_at', '-')}[/dim]")
            if trace.get('resolved_at'):
                console.print(f"[dim]Resolved: {trace['resolved_at']}[/dim]")

        finally:
            await close_postgres()

    asyncio.run(show_detail())


@app.command("training-stats")
def training_stats() -> None:
    """Show statistics about available training data for fine-tuning."""
    async def show_stats():
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.agent.decision_memory import init_decision_memory

        await init_postgres()

        try:
            decision_memory = await init_decision_memory()
            stats = await decision_memory.exporter.get_training_stats()

            console.print("\n[bold]Training Data Statistics[/bold]\n")

            table = Table()
            table.add_column("Metric", style="cyan")
            table.add_column("Value", justify="right", style="green")

            table.add_row("Total Traces", str(stats["total_traces"]))
            table.add_row("High Quality (≥0.8)", str(stats["high_quality"]))
            table.add_row("Medium Quality (0.6-0.8)", str(stats["medium_quality"]))
            table.add_row("Low Quality (<0.6)", str(stats["low_quality"]))
            table.add_row("Trainable Examples", f"[bold]{stats['trainable']}[/bold]")
            table.add_row("Average Quality", f"{stats['avg_quality']:.2f}")

            console.print(table)

            if stats["trainable"] > 0:
                console.print(f"\n[green]✓ {stats['trainable']} examples ready for fine-tuning[/green]")
                console.print("[dim]Export with: cognitex training-export --output training_data.jsonl[/dim]")
            else:
                console.print("\n[yellow]No training data available yet.[/yellow]")
                console.print("[dim]Decision traces are captured automatically as you interact with the agent.[/dim]")

        finally:
            await close_postgres()

    asyncio.run(show_stats())


@app.command("training-export")
def training_export(
    output: str = typer.Option("training_data.jsonl", "--output", "-o", help="Output file path"),
    min_quality: float = typer.Option(0.6, "--min-quality", "-q", help="Minimum quality score to include"),
    limit: int = typer.Option(None, "--limit", "-l", help="Maximum number of examples to export"),
) -> None:
    """Export training data for fine-tuning in JSONL format."""
    async def export_data():
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.agent.decision_memory import init_decision_memory
        import json

        await init_postgres()

        try:
            decision_memory = await init_decision_memory()
            examples = await decision_memory.exporter.export_training_data(
                min_quality=min_quality,
                limit=limit,
            )

            if not examples:
                console.print("[yellow]No training data to export.[/yellow]")
                return

            # Write JSONL
            with open(output, "w") as f:
                for example in examples:
                    f.write(json.dumps(example, default=str) + "\n")

            console.print(f"[green]✓ Exported {len(examples)} training examples to {output}[/green]")

            # Show quality distribution
            high = sum(1 for e in examples if e["quality_score"] >= 0.8)
            med = sum(1 for e in examples if 0.6 <= e["quality_score"] < 0.8)
            console.print(f"[dim]  High quality (≥0.8): {high}[/dim]")
            console.print(f"[dim]  Medium quality (0.6-0.8): {med}[/dim]")

        finally:
            await close_postgres()

    asyncio.run(export_data())


@app.command("comm-patterns")
def communication_patterns(
    email: str = typer.Option(None, "--email", "-e", help="Show pattern for specific email address"),
) -> None:
    """Show learned communication patterns for contacts."""
    async def show_patterns():
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.agent.decision_memory import init_decision_memory
        from sqlalchemy import text

        await init_postgres()

        try:
            decision_memory = await init_decision_memory()

            if email:
                # Show specific pattern
                pattern = await decision_memory.patterns.get_pattern(email)
                if not pattern:
                    console.print(f"[yellow]No pattern found for: {email}[/yellow]")
                    return

                console.print(f"\n[bold]Communication Pattern: {email}[/bold]\n")

                table = Table()
                table.add_column("Attribute", style="cyan")
                table.add_column("Value", style="green")

                for key, value in pattern.items():
                    if value and key not in ["person_email"]:
                        if isinstance(value, list):
                            value = ", ".join(str(v) for v in value[:5])
                        table.add_row(key.replace("_", " ").title(), str(value))

                console.print(table)
            else:
                # List all patterns
                async for session in get_session():
                    result = await session.execute(text("""
                        SELECT person_email, person_name, relationship_type,
                               preferred_tone, interaction_count, pattern_confidence
                        FROM communication_patterns
                        ORDER BY interaction_count DESC
                        LIMIT 50
                    """))

                    rows = result.fetchall()
                    if not rows:
                        console.print("[yellow]No communication patterns learned yet.[/yellow]")
                        return

                    table = Table(title="Communication Patterns")
                    table.add_column("Email", style="cyan")
                    table.add_column("Name", style="green")
                    table.add_column("Relationship")
                    table.add_column("Tone")
                    table.add_column("Interactions", justify="right")
                    table.add_column("Confidence", justify="right")

                    for row in rows:
                        table.add_row(
                            row.person_email[:30],
                            (row.person_name or "-")[:20],
                            row.relationship_type or "-",
                            row.preferred_tone or "-",
                            str(row.interaction_count),
                            f"{row.pattern_confidence:.2f}" if row.pattern_confidence else "-",
                        )

                    console.print(table)
                    break

        finally:
            await close_postgres()

    asyncio.run(show_patterns())


@app.command("extract-rules")
def extract_rules(
    min_occurrences: int = typer.Option(3, "--min", "-m", help="Minimum occurrences to create rule"),
) -> None:
    """Extract preference rules from decision patterns."""
    async def extract():
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.agent.decision_memory import init_decision_memory

        await init_postgres()

        try:
            decision_memory = await init_decision_memory()
            console.print("[bold]Analyzing decision patterns...[/bold]\n")

            rule_ids = await decision_memory.extract_rules_from_patterns(
                min_occurrences=min_occurrences
            )

            if rule_ids:
                console.print(f"[green]Created/reinforced {len(rule_ids)} preference rules[/green]")

                # Show the rules
                rules = await decision_memory.rules.get_matching_rules({})
                if rules:
                    table = Table(title="Active Preference Rules")
                    table.add_column("Name", style="cyan")
                    table.add_column("Type")
                    table.add_column("Condition")
                    table.add_column("Preference")
                    table.add_column("Confidence", justify="right")

                    for rule in rules[:10]:
                        cond_str = ", ".join(f"{k}={v}" for k, v in (rule.get("condition") or {}).items())
                        pref_str = ", ".join(f"{k}={v}" for k, v in (rule.get("preference") or {}).items())
                        table.add_row(
                            rule.get("rule_name", "")[:40],
                            rule.get("rule_type", "-"),
                            cond_str[:30],
                            pref_str[:40],
                            f"{rule.get('confidence', 0):.0%}",
                        )

                    console.print(table)
            else:
                console.print("[yellow]No patterns found meeting criteria.[/yellow]")
                console.print(f"Need at least {min_occurrences} similar decisions with quality >= 0.6")

        finally:
            await close_postgres()

    asyncio.run(extract())


@app.command("pref-rules")
def preference_rules() -> None:
    """Show learned preference rules."""
    async def show_rules():
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.agent.decision_memory import init_decision_memory
        from sqlalchemy import text

        await init_postgres()

        try:
            decision_memory = await init_decision_memory()

            async for session in get_session():
                result = await session.execute(text("""
                    SELECT id, rule_type, rule_name, condition, preference,
                           confidence, evidence_count, user_confirmed, created_at
                    FROM preference_rules
                    WHERE is_active = true
                    ORDER BY confidence DESC, evidence_count DESC
                    LIMIT 30
                """))

                rows = result.fetchall()
                if not rows:
                    console.print("[yellow]No preference rules learned yet.[/yellow]")
                    console.print("Rules are extracted from consistent decision patterns.")
                    console.print("Run: cognitex extract-rules")
                    return

                table = Table(title="Preference Rules")
                table.add_column("Name", style="cyan")
                table.add_column("Type")
                table.add_column("Confidence", justify="right")
                table.add_column("Evidence", justify="right")
                table.add_column("Confirmed")

                for row in rows:
                    table.add_row(
                        (row.rule_name or "-")[:45],
                        row.rule_type or "-",
                        f"{row.confidence:.0%}" if row.confidence else "-",
                        str(row.evidence_count),
                        "Yes" if row.user_confirmed else "No",
                    )

                console.print(table)
                break

        finally:
            await close_postgres()

    asyncio.run(show_rules())


@app.command("analyze-chunks")
def analyze_chunks(
    limit: int = typer.Option(50, "--limit", "-n", help="Number of chunks to analyze"),
) -> None:
    """Analyze document chunks and integrate into semantic graph."""
    async def analyze():
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import init_graph_schema
        from cognitex.services.ingestion import analyze_chunks_batch

        await init_postgres()
        await init_neo4j()
        await init_graph_schema()

        console.print(f"[bold]Analyzing up to {limit} chunks for semantic graph...[/bold]")

        try:
            async for pg_session in get_session():
                async for neo4j_session in get_neo4j_session():
                    stats = await analyze_chunks_batch(pg_session, neo4j_session, limit=limit)

                    console.print()
                    console.print("[green]Analysis complete![/green]")
                    console.print(f"  Chunks processed: {stats['processed']}")
                    console.print(f"  Chunks skipped (already done): {stats['skipped']}")
                    console.print(f"  Topics linked: {stats['topics_created']}")
                    console.print(f"  Concepts linked: {stats['concepts_created']}")
                    console.print(f"  People linked: {stats['people_linked']}")
                    if stats['errors'] > 0:
                        console.print(f"  [red]Errors: {stats['errors']}[/red]")
                    break
                break

        finally:
            await close_postgres()
            await close_neo4j()

    asyncio.run(analyze())


@app.command("topics")
def list_topics(
    limit: int = typer.Option(30, "--limit", "-n", help="Number of topics to show"),
) -> None:
    """List extracted topics from document analysis."""
    async def show():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import get_topics

        await init_neo4j()

        try:
            async for session in get_neo4j_session():
                topics = await get_topics(session, limit=limit)

                if not topics:
                    console.print("[yellow]No topics found. Run 'cognitex analyze-chunks' first.[/yellow]")
                    return

                table = Table(title=f"Topics ({len(topics)} shown)")
                table.add_column("Topic", style="cyan")
                table.add_column("Mentions", justify="right")
                table.add_column("Chunks", justify="right")
                table.add_column("Documents", justify="right")

                for t in topics:
                    table.add_row(
                        t["name"],
                        str(t.get("mention_count", 0)),
                        str(t.get("chunk_count", 0)),
                        str(t.get("document_count", 0)),
                    )

                console.print(table)
                break

        finally:
            await close_neo4j()

    asyncio.run(show())


@app.command("concepts")
def list_concepts(
    limit: int = typer.Option(30, "--limit", "-n", help="Number of concepts to show"),
) -> None:
    """List extracted concepts from document analysis."""
    async def show():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import get_concepts

        await init_neo4j()

        try:
            async for session in get_neo4j_session():
                concepts = await get_concepts(session, limit=limit)

                if not concepts:
                    console.print("[yellow]No concepts found. Run 'cognitex analyze-chunks' first.[/yellow]")
                    return

                table = Table(title=f"Concepts ({len(concepts)} shown)")
                table.add_column("Concept", style="cyan")
                table.add_column("Mentions", justify="right")
                table.add_column("Chunks", justify="right")
                table.add_column("Documents", justify="right")

                for c in concepts:
                    table.add_row(
                        c["name"][:50],
                        str(c.get("mention_count", 0)),
                        str(c.get("chunk_count", 0)),
                        str(c.get("document_count", 0)),
                    )

                console.print(table)
                break

        finally:
            await close_neo4j()

    asyncio.run(show())


@app.command("topic-explore")
def explore_topic(
    topic: str = typer.Argument(..., help="Topic name to explore"),
) -> None:
    """Explore connections for a specific topic."""
    async def explore():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import get_topic_connections, find_chunks_by_topic

        await init_neo4j()

        try:
            async for session in get_neo4j_session():
                connections = await get_topic_connections(session, topic)
                chunks = await find_chunks_by_topic(session, topic, limit=5)

                if not chunks and not any(connections.values()):
                    console.print(f"[yellow]Topic '{topic}' not found.[/yellow]")
                    return

                console.print(f"\n[bold cyan]Topic: {topic}[/bold cyan]\n")

                if connections["related_topics"]:
                    console.print("[bold]Related Topics:[/bold]")
                    for t in connections["related_topics"][:10]:
                        console.print(f"  - {t}")

                if connections["related_concepts"]:
                    console.print("\n[bold]Related Concepts:[/bold]")
                    for c in connections["related_concepts"][:10]:
                        console.print(f"  - {c}")

                if connections["mentioned_people"]:
                    console.print("\n[bold]People Mentioned:[/bold]")
                    for p in connections["mentioned_people"][:10]:
                        console.print(f"  - {p}")

                if connections["documents"]:
                    console.print("\n[bold]Documents:[/bold]")
                    for d in connections["documents"][:10]:
                        console.print(f"  - {d}")

                if chunks:
                    console.print("\n[bold]Sample Chunks:[/bold]")
                    for c in chunks[:3]:
                        console.print(f"  [{c['document_name']}]")
                        if c.get('summary'):
                            console.print(f"    {c['summary'][:150]}...")
                        console.print()

                break

        finally:
            await close_neo4j()

    asyncio.run(explore())


@app.command("semantic-stats")
def semantic_stats() -> None:
    """Show statistics about the semantic graph."""
    async def show():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.db.graph_schema import get_semantic_graph_stats
        from sqlalchemy import text

        await init_postgres()
        await init_neo4j()

        try:
            # Get PostgreSQL chunk stats
            async for pg_session in get_session():
                result = await pg_session.execute(text("""
                    SELECT COUNT(*) as chunks,
                           COUNT(DISTINCT drive_id) as documents
                    FROM document_chunks
                """))
                pg_stats = result.fetchone()
                break

            # Get Neo4j graph stats
            async for neo4j_session in get_neo4j_session():
                graph_stats = await get_semantic_graph_stats(neo4j_session)
                break

            console.print("\n[bold]Semantic Graph Statistics[/bold]\n")
            console.print(f"PostgreSQL:")
            console.print(f"  Chunks stored: {pg_stats.chunks}")
            console.print(f"  Documents chunked: {pg_stats.documents}")
            console.print()
            console.print(f"Neo4j Graph:")
            console.print(f"  Chunk nodes: {graph_stats['chunks']}")
            console.print(f"  Chunks analyzed: {graph_stats['analyzed']}")
            console.print(f"  Topics: {graph_stats['topics']}")
            console.print(f"  Concepts: {graph_stats['concepts']}")

            if pg_stats.chunks > 0:
                pct = (graph_stats['analyzed'] / pg_stats.chunks) * 100
                console.print(f"\n  [cyan]Analysis progress: {pct:.1f}%[/cyan]")

        finally:
            await close_postgres()
            await close_neo4j()

    asyncio.run(show())


# =============================================================================
# Phase 3: State Model, Decision Policy, Context Packs
# =============================================================================


@app.command("state")
def show_state() -> None:
    """Show current operating state and mode."""
    async def show():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.agent.state_model import get_state_estimator, OperatingMode, ModeRules

        await init_neo4j()

        try:
            estimator = get_state_estimator()
            state = await estimator.get_current_state()

            console.print("\n[bold]Current Operating State[/bold]\n")

            # Mode with color coding
            mode_colors = {
                OperatingMode.DEEP_FOCUS: "green",
                OperatingMode.FRAGMENTED: "yellow",
                OperatingMode.OVERLOADED: "red",
                OperatingMode.AVOIDANT: "magenta",
                OperatingMode.HYPERFOCUS: "cyan",
                OperatingMode.TRANSITION: "blue",
            }
            color = mode_colors.get(state.mode, "white")
            console.print(f"Mode: [{color}]{state.mode.value}[/{color}]")

            # Get mode rules
            rules = ModeRules.get_rules(state.mode)
            console.print(f"  {rules['description']}")

            console.print("\n[bold]Signals:[/bold]")
            signals = state.signals
            console.print(f"  Available block: {signals.available_block_minutes or 'Unknown'} minutes")
            console.print(f"  Time to next commitment: {signals.time_to_next_commitment_minutes or 'Unknown'} minutes")
            console.print(f"  Interruption pressure: {signals.interruption_pressure:.0%}")
            console.print(f"  Fatigue level: {signals.fatigue_level:.0%}")
            console.print(f"  Focus score: {signals.focus_score or 'Unknown'}")

            console.print("\n[bold]Mode Rules:[/bold]")
            console.print(f"  Max task friction: {rules['max_task_friction']}")
            console.print(f"  Notification gate: {rules['notification_gate']}")
            console.print(f"  Allowed task types: {', '.join(rules['allowed_task_types'][:3])}...")

        finally:
            await close_neo4j()

    asyncio.run(show())


@app.command("state-set")
def set_state(
    mode: str = typer.Option(None, "--mode", "-m", help="Set operating mode"),
    fatigue: float = typer.Option(None, "--fatigue", "-f", help="Set fatigue level (0-1)"),
    focus: float = typer.Option(None, "--focus", help="Set focus score (0-1)"),
) -> None:
    """Update current operating state."""
    async def update():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.agent.state_model import get_state_estimator, OperatingMode

        await init_neo4j()

        try:
            estimator = get_state_estimator()

            # Parse mode if provided
            new_mode = None
            if mode:
                try:
                    new_mode = OperatingMode(mode.lower())
                except ValueError:
                    console.print(f"[red]Invalid mode: {mode}[/red]")
                    console.print("Valid modes: " + ", ".join(m.value for m in OperatingMode))
                    return

            # Calculate fatigue delta if provided
            fatigue_delta = None
            if fatigue is not None:
                current = await estimator.get_current_state()
                fatigue_delta = fatigue - current.signals.fatigue_level

            # Update state
            state = await estimator.update_state(
                mode=new_mode,
                fatigue_delta=fatigue_delta,
                focus_score=focus,
            )

            console.print(f"[green]State updated[/green]")
            console.print(f"  Mode: {state.mode.value}")
            console.print(f"  Fatigue: {state.signals.fatigue_level:.0%}")
            console.print(f"  Focus: {state.signals.focus_score or 'N/A'}")

        finally:
            await close_neo4j()

    asyncio.run(update())


@app.command("next-action")
def next_action(
    limit: int = typer.Option(3, "--limit", "-n", help="Number of recommendations"),
) -> None:
    """Get recommended next actions based on state and tasks."""
    async def recommend():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.agent.decision_policy import (
            get_decision_policy,
            TaskContext,
            TaskType,
        )
        from cognitex.services.tasks import get_task_service

        await init_neo4j()

        try:
            # Get pending tasks
            task_service = get_task_service()
            tasks = await task_service.list(include_done=False, limit=20)

            if not tasks:
                console.print("[yellow]No pending tasks found[/yellow]")
                return

            # Convert to TaskContext objects
            task_contexts = []
            for t in tasks:
                # Infer task type from priority/properties
                task_type = TaskType.QUICK_WINS
                if t.get("effort_estimate", 30) > 60:
                    task_type = TaskType.DEEP_WORK
                elif "email" in t.get("title", "").lower():
                    task_type = TaskType.EMAIL
                elif t.get("priority") == "high":
                    task_type = TaskType.URGENT_ONLY

                ctx = TaskContext(
                    task_id=t["id"],
                    title=t["title"],
                    task_type=task_type,
                    estimated_minutes=int(t.get("effort_estimate", 30)),
                    start_friction=3 if t.get("energy_cost") == "high" else 2,
                    urgency_score=0.8 if t.get("priority") == "critical" else 0.5,
                    goal_alignment=0.7 if t.get("goal_id") else 0.3,
                    project_id=t.get("project_id"),
                    goal_id=t.get("goal_id"),
                )
                task_contexts.append(ctx)

            # Get recommendations
            policy = get_decision_policy()
            recommendations = await policy.select_next_actions(
                task_contexts,
                max_recommendations=limit,
            )

            console.print("\n[bold]Recommended Next Actions[/bold]\n")

            for i, rec in enumerate(recommendations, 1):
                if rec.utility_score > 0:
                    console.print(f"[cyan]{i}. {rec.task.title}[/cyan]")
                    console.print(f"   Utility: {rec.utility_score:.2f} | {rec.reasoning}")
                    if rec.mvs_action:
                        console.print(f"   [green]Start with:[/green] {rec.mvs_action}")
                    if rec.warnings:
                        for w in rec.warnings:
                            console.print(f"   [yellow]⚠ {w}[/yellow]")
                    console.print()

        finally:
            await close_neo4j()

    asyncio.run(recommend())


@app.command("context-pack")
def context_pack(
    event_id: str = typer.Option(None, "--event", "-e", help="Calendar event ID"),
    task_id: str = typer.Option(None, "--task", "-t", help="Task ID"),
    show_pack: str = typer.Option(None, "--show", "-s", help="Show pack by ID (partial match ok)"),
    list_packs: bool = typer.Option(False, "--list", "-l", help="List existing context packs"),
) -> None:
    """Compile or show context pack for an event or task."""
    async def compile():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.agent.context_pack import get_context_pack_compiler, BuildStage

        await init_neo4j()

        # Show a specific pack by ID
        if show_pack:
            async for session in get_neo4j_session():
                result = await session.run("""
                    MATCH (p:ContextPack)
                    WHERE p.id STARTS WITH $pack_id OR p.id = $pack_id
                    RETURN p
                    LIMIT 1
                """, pack_id=show_pack)
                record = await result.single()

            if not record:
                console.print(f"[red]Pack not found: {show_pack}[/red]")
                await close_neo4j()
                return

            p = record["p"]
            console.print("\n[bold]Context Pack[/bold]\n")
            console.print(f"Pack ID: {p.get('id')}")
            console.print(f"Objective: {p.get('objective') or 'Not set'}")
            console.print(f"Build Stage: {p.get('build_stage')}")
            console.print(f"Readiness: {(p.get('readiness_score') or 0):.0%}")

            if p.get("last_touch_recap"):
                console.print(f"\n[bold]Last Touch:[/bold] {p.get('last_touch_recap')}")

            if p.get("decision_list"):
                console.print("\n[bold]Decisions Needed:[/bold]")
                for d in p.get("decision_list"):
                    console.print(f"  • {d}")

            if p.get("dont_forget"):
                console.print("\n[bold]Don't Forget:[/bold]")
                for r in p.get("dont_forget"):
                    console.print(f"  ⚠ {r}")

            if p.get("missing_prerequisites"):
                console.print("\n[bold]Missing Prerequisites:[/bold]")
                for m in p.get("missing_prerequisites"):
                    console.print(f"  [red]✗[/red] {m}")

            if p.get("artifact_links"):
                console.print("\n[bold]Relevant Documents:[/bold]")
                for a in p.get("artifact_links"):
                    console.print(f"  📄 {a}")

            await close_neo4j()
            return

        # List existing packs
        if list_packs:
            async for session in get_neo4j_session():
                result = await session.run("""
                    MATCH (p:ContextPack)
                    OPTIONAL MATCH (e:CalendarEvent {gcal_id: p.event_id})
                    OPTIONAL MATCH (t:Task {id: p.task_id})
                    RETURN p.id as pack_id,
                           p.build_stage as stage,
                           p.readiness_score as readiness,
                           p.objective as objective,
                           e.summary as event_name,
                           t.title as task_name,
                           p.created_at as created
                    ORDER BY p.created_at DESC
                    LIMIT 20
                """)
                packs = await result.data()

            if not packs:
                console.print("[yellow]No context packs found[/yellow]")
                await close_neo4j()
                return

            table = Table(title="Context Packs")
            table.add_column("Pack ID", style="cyan")
            table.add_column("For")
            table.add_column("Stage")
            table.add_column("Readiness")

            for p in packs:
                # Extract event/task name from objective if not found via join
                target = p.get("event_name") or p.get("task_name")
                if not target:
                    obj = p.get("objective") or ""
                    # Objective is often "Complete: <name>" format
                    target = obj.replace("Complete: ", "").replace("Prepare for: ", "")[:40]
                readiness = p.get("readiness") or 0
                pack_id = p.get("pack_id") or "N/A"
                table.add_row(
                    pack_id[:16] + "..." if len(pack_id) > 16 else pack_id,
                    target[:30] + "..." if len(target) > 30 else target,
                    p.get("stage") or "?",
                    f"{readiness:.0%}",
                )

            console.print(table)
            await close_neo4j()
            return

        if not event_id and not task_id:
            console.print("[yellow]Specify --event, --task, or --list[/yellow]")
            await close_neo4j()
            return

        try:
            compiler = get_context_pack_compiler()

            if event_id:
                # Get event from calendar
                from cognitex.services.calendar import get_calendar_service
                cal = get_calendar_service()
                event = await cal.get_event(event_id)
                if not event:
                    console.print(f"[red]Event not found: {event_id}[/red]")
                    return

                pack = await compiler.compile_for_event(event, BuildStage.T_24H)
            else:
                # Get task
                from cognitex.services.tasks import get_task_service
                task_service = get_task_service()
                task = await task_service.get(task_id)
                if not task:
                    console.print(f"[red]Task not found: {task_id}[/red]")
                    return

                pack = await compiler.compile_for_task(task, BuildStage.T_24H)

            # Display pack
            console.print("\n[bold]Context Pack[/bold]\n")
            console.print(f"Pack ID: {pack.pack_id}")
            console.print(f"Objective: {pack.objective or 'Not set'}")
            console.print(f"Build Stage: {pack.build_stage.value}")
            console.print(f"Readiness: {pack.readiness_score:.0%}")

            if pack.last_touch_recap:
                console.print(f"\n[bold]Last Touch:[/bold] {pack.last_touch_recap}")

            if pack.decision_list:
                console.print("\n[bold]Decisions Needed:[/bold]")
                for d in pack.decision_list:
                    console.print(f"  • {d}")

            if pack.dont_forget:
                console.print("\n[bold]Don't Forget:[/bold]")
                for r in pack.dont_forget:
                    console.print(f"  ⚠ {r}")

            if pack.missing_prerequisites:
                console.print("\n[bold]Missing Prerequisites:[/bold]")
                for m in pack.missing_prerequisites:
                    console.print(f"  [red]✗[/red] {m}")

            if pack.prep_tasks_needed:
                console.print("\n[bold]Prep Tasks:[/bold]")
                for p in pack.prep_tasks_needed:
                    console.print(f"  • {p['task']} ({p['minutes']} min, {p['priority']})")

        finally:
            await close_neo4j()

    asyncio.run(compile())


@app.command("day-plan")
def day_plan(
    plan_b: bool = typer.Option(False, "--plan-b", "-b", help="Show Plan B (minimum viable)"),
) -> None:
    """Show today's day plan (Plan A or Plan B)."""
    async def show():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.services.calendar import CalendarService
        from cognitex.services.tasks import get_task_service
        from cognitex.agent.context_pack import get_two_track_planner

        await init_neo4j()

        try:
            # Get today's events
            from datetime import timedelta as td
            cal = CalendarService()
            now = datetime.now()
            tomorrow = now + td(days=1)
            result = cal.list_events(time_max=tomorrow)
            events = result.get("items", [])

            # Get pending tasks
            task_service = get_task_service()
            tasks = await task_service.list(include_done=False, limit=50)

            # Create plans
            planner = get_two_track_planner()
            plan_a, plan_b_plan = await planner.create_day_plans(events, tasks)

            plan = plan_b_plan if plan_b else plan_a
            plan_type = "B (Minimum Viable)" if plan_b else "A (Normal Capacity)"

            console.print(f"\n[bold]Day Plan {plan_type}[/bold]\n")
            console.print(f"Date: {plan.date.strftime('%Y-%m-%d')}")
            console.print(f"Items: {len(plan.items)}")
            console.print(f"Capacity used: {plan.capacity_used:.0%}")

            console.print("\n[bold]Schedule:[/bold]")

            for item in plan.items[:15]:
                if "start" in item:
                    # Calendar event - handle Google Calendar dict format
                    start_raw = item.get("start", "")
                    if isinstance(start_raw, dict):
                        start_str = start_raw.get("dateTime") or start_raw.get("date", "")
                    else:
                        start_str = start_raw
                    start = start_str[:16] if start_str else ""
                    summary = item.get("summary", "Event")
                    is_protected = item.get("id") in plan.protected_items
                    marker = "🔒" if is_protected else "📅"
                    console.print(f"  {marker} {start} - {summary}")
                else:
                    # Task
                    title = item.get("title", "Task")
                    est = item.get("effort_estimate", 30)
                    console.print(f"  📋 {title} ({est} min)")

            # Show switch recommendation if applicable
            should_switch, reason = await planner.should_switch_to_plan_b()
            if should_switch and not plan_b:
                console.print(f"\n[yellow]⚠ Consider switching to Plan B: {reason}[/yellow]")

        finally:
            await close_neo4j()

    asyncio.run(show())


@app.command("inbox-queue")
def inbox_queue(
    queue: str = typer.Option("inbox", "--queue", "-q", help="Queue to show"),
) -> None:
    """Show captured inbox items awaiting processing."""
    async def show():
        from cognitex.agent.interruption_firewall import get_interruption_firewall

        firewall = get_interruption_firewall()
        items = await firewall.get_queued_items(queue=queue)

        if not items:
            console.print(f"[green]No items in {queue} queue[/green]")
            return

        console.print(f"\n[bold]Inbox Queue: {queue}[/bold] ({len(items)} items)\n")

        for item in items[:20]:
            urgency_colors = {
                "critical": "red",
                "urgent": "yellow",
                "important": "cyan",
                "normal": "white",
                "low": "dim",
            }
            color = urgency_colors.get(item.urgency.value, "white")
            console.print(f"[{color}]{item.urgency.value.upper()}[/{color}] {item.subject}")
            console.print(f"  From: {item.sender or 'Unknown'} | {item.item_type}")
            console.print(f"  Action: {item.suggested_action}")
            console.print()

    asyncio.run(show())


@app.command("init-phase3")
def init_phase3() -> None:
    """Initialize Phase 3 schema (claims, state, context packs)."""
    async def init():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.db.phase3_schema import init_phase3_schema

        console.print("[bold]Initializing Phase 3 Schema...[/bold]")

        await init_neo4j()

        try:
            await init_phase3_schema()
            console.print("[green]✓ Phase 3 schema initialized[/green]")
            console.print("\nNew entity types available:")
            console.print("  • Claim - Atomic statements with evidence grading")
            console.print("  • LiteratureItem - Bibliographic objects with DOI")
            console.print("  • SpanAnchor - Immutable provenance anchors")
            console.print("  • StateSnapshot - User operating state tracking")
            console.print("  • ContextPack - Pre-compiled context for events/tasks")
            console.print("  • Draft - Writing artifacts with citation links")
            console.print("  • Run - Experiment/analysis registry")
            console.print("  • ReviewerComment - Reviewer response management")

        finally:
            await close_neo4j()

    asyncio.run(init())


# =============================================================================
# Linking Commands
# =============================================================================

@app.command("link-folder")
def link_folder_cmd(
    folder_name: str = typer.Argument(..., help="Folder name to search for"),
    project: str = typer.Option(..., "--project", "-p", help="Project short ID or title"),
) -> None:
    """Link a Drive folder to a project (all docs auto-link)."""
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.services.linking import get_linking_service, init_linking_schema
        from cognitex.services.drive import get_drive_service
        from cognitex.services.tasks import get_project_service

        await init_postgres()
        await init_neo4j()

        try:
            # Initialize linking schema
            async for session in get_session():
                await init_linking_schema(session)

            # Resolve project
            project_svc = get_project_service()
            projects = await project_svc.list()
            project_id = None
            project_title = None

            # Try short ID first
            try:
                idx = int(project) - 1
                if 0 <= idx < len(projects):
                    project_id = projects[idx]["id"]
                    project_title = projects[idx]["title"]
            except ValueError:
                # Try matching by title
                for p in projects:
                    if project.lower() in p["title"].lower():
                        project_id = p["id"]
                        project_title = p["title"]
                        break

            if not project_id:
                console.print(f"[red]Project not found: {project}[/red]")
                return

            # Find folder in Drive
            drive = get_drive_service()
            folder_id = None
            folder_path = None

            # Search for folder by name
            with console.status(f"Searching for folder '{folder_name}'..."):
                for file in drive.list_all_files():
                    if file.get("mimeType") == "application/vnd.google-apps.folder":
                        if folder_name.lower() in file["name"].lower():
                            folder_id = file["id"]
                            folder_path = file.get("name")
                            break

            if not folder_id:
                console.print(f"[red]Folder not found: {folder_name}[/red]")
                return

            # Create the mapping
            linking = get_linking_service()
            async for session in get_session():
                await linking.add_folder_mapping(
                    session,
                    folder_id=folder_id,
                    project_id=project_id,
                    folder_name=folder_path,
                    project_title=project_title,
                    auto_link_new_files=True,
                )

                # Also link existing documents in this folder
                count = await linking.link_folder_contents_to_project(
                    session, folder_id, project_id
                )

            console.print(f"[green]✓ Linked folder '{folder_path}' to project '{project_title}'[/green]")
            console.print(f"  Linked {count} existing documents")

        finally:
            await close_postgres()
            await close_neo4j()

    asyncio.run(run())


@app.command("link-contact")
def link_contact_cmd(
    email: str = typer.Argument(..., help="Contact email address"),
    project: str = typer.Option(..., "--project", "-p", help="Project short ID or title"),
    role: str = typer.Option(None, "--role", "-r", help="Role (stakeholder, team_member, client)"),
) -> None:
    """Link a contact to a project (emails auto-link)."""
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.services.linking import get_linking_service, init_linking_schema
        from cognitex.services.tasks import get_project_service

        await init_postgres()
        await init_neo4j()

        try:
            # Initialize schema
            async for session in get_session():
                await init_linking_schema(session)

            # Resolve project
            project_svc = get_project_service()
            projects = await project_svc.list()
            project_id = None
            project_title = None

            try:
                idx = int(project) - 1
                if 0 <= idx < len(projects):
                    project_id = projects[idx]["id"]
                    project_title = projects[idx]["title"]
            except ValueError:
                for p in projects:
                    if project.lower() in p["title"].lower():
                        project_id = p["id"]
                        project_title = p["title"]
                        break

            if not project_id:
                console.print(f"[red]Project not found: {project}[/red]")
                return

            # Create the mapping
            linking = get_linking_service()
            async for session in get_session():
                await linking.add_contact_mapping(
                    session,
                    contact_email=email,
                    project_id=project_id,
                    project_title=project_title,
                    role=role,
                    auto_link_emails=True,
                )

            console.print(f"[green]✓ Linked contact '{email}' to project '{project_title}'[/green]")
            if role:
                console.print(f"  Role: {role}")
            console.print("  Future emails from this contact will auto-link to the project")

        finally:
            await close_postgres()
            await close_neo4j()

    asyncio.run(run())


@app.command("link-mappings")
def link_mappings_cmd(
    project: str = typer.Option(None, "--project", "-p", help="Filter by project"),
) -> None:
    """Show folder and contact mapping rules."""
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.services.linking import get_linking_service, init_linking_schema
        from cognitex.services.tasks import get_project_service

        await init_postgres()
        await init_neo4j()

        try:
            async for session in get_session():
                await init_linking_schema(session)

            # Resolve project if specified
            project_id = None
            if project:
                project_svc = get_project_service()
                projects = await project_svc.list()
                try:
                    idx = int(project) - 1
                    if 0 <= idx < len(projects):
                        project_id = projects[idx]["id"]
                except ValueError:
                    for p in projects:
                        if project.lower() in p["title"].lower():
                            project_id = p["id"]
                            break

            linking = get_linking_service()

            # Folder mappings
            async for session in get_session():
                folders = await linking.get_folder_mappings(session, project_id=project_id)
                contacts = await linking.get_contact_mappings(session, project_id=project_id)

            console.print("\n[bold]Folder Mappings[/bold]")
            if folders:
                table = Table()
                table.add_column("Folder")
                table.add_column("Project")
                table.add_column("Auto-Link")

                for f in folders:
                    table.add_row(
                        f.get("folder_name") or f["folder_id"][:12],
                        f.get("project_title") or f["project_id"][:12],
                        "✓" if f.get("auto_link_new_files") else "✗",
                    )
                console.print(table)
            else:
                console.print("[dim]  No folder mappings[/dim]")

            console.print("\n[bold]Contact Mappings[/bold]")
            if contacts:
                table = Table()
                table.add_column("Email")
                table.add_column("Project")
                table.add_column("Role")
                table.add_column("Auto-Link")

                for c in contacts:
                    table.add_row(
                        c["contact_email"],
                        c.get("project_title") or c["project_id"][:12],
                        c.get("role") or "-",
                        "✓" if c.get("auto_link_emails") else "✗",
                    )
                console.print(table)
            else:
                console.print("[dim]  No contact mappings[/dim]")

        finally:
            await close_postgres()
            await close_neo4j()

    asyncio.run(run())


@app.command("link-suggestions")
def link_suggestions_cmd(
    approve: str = typer.Option(None, "--approve", "-a", help="Approve suggestion by ID"),
    reject: str = typer.Option(None, "--reject", "-r", help="Reject suggestion by ID"),
    approve_all: bool = typer.Option(False, "--approve-all", help="Approve all pending suggestions"),
    min_confidence: float = typer.Option(0.0, "--min-confidence", "-c", help="Min confidence for batch approve (0.0-1.0)"),
    limit: int = typer.Option(20, "--limit", "-l", help="Number to show (or max to batch approve)"),
) -> None:
    """View and manage suggested links."""
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.services.linking import get_linking_service, init_linking_schema

        await init_postgres()
        await init_neo4j()

        try:
            async for session in get_session():
                await init_linking_schema(session)

            linking = get_linking_service()

            # Batch approve all (or filtered by confidence)
            if approve_all:
                async for session in get_session():
                    approved, failed = await linking.batch_approve_suggestions(
                        session,
                        min_confidence=min_confidence,
                        limit=limit if limit != 20 else None,
                    )
                if approved > 0:
                    console.print(f"[green]✓ Approved {approved} suggestions[/green]")
                if failed > 0:
                    console.print(f"[yellow]! {failed} failed to create links[/yellow]")
                if approved == 0 and failed == 0:
                    console.print("[dim]No pending suggestions to approve[/dim]")
                return

            if approve:
                async for session in get_session():
                    success = await linking.approve_suggestion(session, approve)
                if success:
                    console.print(f"[green]✓ Approved suggestion {approve}[/green]")
                else:
                    console.print(f"[red]Suggestion not found: {approve}[/red]")
                return

            if reject:
                async for session in get_session():
                    success = await linking.reject_suggestion(session, reject)
                if success:
                    console.print(f"[yellow]✗ Rejected suggestion {reject}[/yellow]")
                else:
                    console.print(f"[red]Suggestion not found: {reject}[/red]")
                return

            # Show pending suggestions
            async for session in get_session():
                suggestions = await linking.get_pending_suggestions(session, limit=limit)
                stats = await linking.get_suggestion_stats(session)

            console.print(f"\n[bold]Suggested Links[/bold] (pending: {stats['pending']}, approved: {stats['approved']}, rejected: {stats['rejected']})\n")

            if not suggestions:
                console.print("[dim]No pending suggestions[/dim]")
                return

            table = Table()
            table.add_column("ID", no_wrap=True)
            table.add_column("Source")
            table.add_column("Target")
            table.add_column("Confidence")
            table.add_column("Reason")

            for s in suggestions:
                table.add_row(
                    s["id"],  # Full ID needed for --approve/--reject
                    f"{s['source_type']}: {s.get('source_name') or s['source_id'][:20]}",
                    f"{s['target_type']}: {s.get('target_name') or s['target_id'][:20]}",
                    f"{s['confidence']:.0%}",
                    (s.get("reason") or "-")[:40],
                )
            console.print(table)

            console.print("\n[dim]Use --approve ID or --reject ID to manage suggestions[/dim]")

        finally:
            await close_postgres()
            await close_neo4j()

    asyncio.run(run())


@app.command("sync-graph")
def sync_graph_cmd(
    limit: int = typer.Option(500, "--limit", "-l", help="Maximum files to sync"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be synced without doing it"),
) -> None:
    """Sync PostgreSQL drive_files to Neo4j Document nodes.

    Ensures that all indexed Drive files have corresponding Document nodes
    in the Neo4j graph. This fixes "document does not exist" errors when
    the autonomous agent tries to link documents.
    """
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.db.neo4j import init_neo4j, close_neo4j, run_query
        from cognitex.services.linking import sync_drive_to_neo4j
        from sqlalchemy import text

        await init_postgres()
        await init_neo4j()

        try:
            if dry_run:
                # Check what would be synced
                async for session in get_session():
                    # Query drive_files count
                    result = await session.execute(text("""
                        SELECT COUNT(*) as total,
                               SUM(CASE WHEN is_indexed THEN 1 ELSE 0 END) as indexed
                        FROM drive_files
                    """))
                    pg_stats = result.mappings().one()

                    # Query Neo4j Document count
                    neo_result = await run_query("MATCH (d:Document) RETURN COUNT(d) as count", {})
                    neo_count = neo_result[0]["count"] if neo_result else 0

                    console.print(f"\n[bold]Graph Sync Status:[/bold]")
                    console.print(f"  PostgreSQL drive_files: {pg_stats['total']} total, {pg_stats['indexed']} indexed")
                    console.print(f"  Neo4j Document nodes: {neo_count}")

                    # Sample of files that would be synced
                    sample_result = await session.execute(text("""
                        SELECT df.drive_id, df.name
                        FROM drive_files df
                        WHERE df.is_indexed = true
                        ORDER BY df.modified_time DESC
                        LIMIT 10
                    """))
                    samples = sample_result.mappings().all()

                    console.print(f"\n[yellow]Sample files that would be checked:[/yellow]")
                    for f in samples:
                        console.print(f"  • {f['name'][:50]} [dim]({f['drive_id'][:12]}...)[/dim]")

                return

            # Perform sync
            console.print("Syncing Drive files to Neo4j graph...")

            async for session in get_session():
                stats = await sync_drive_to_neo4j(session, limit=limit)

                console.print(f"\n[bold]Sync complete:[/bold]")
                console.print(f"  Files checked: {stats['checked']}")
                console.print(f"  Nodes created: [green]{stats['created']}[/green]")
                console.print(f"  Already existed: {stats['already_exists']}")
                if stats['errors']:
                    console.print(f"  Errors: [red]{stats['errors']}[/red]")

        finally:
            await close_postgres()
            await close_neo4j()

    asyncio.run(run())


@app.command("project-content")
def project_content_cmd(
    project: str = typer.Argument(..., help="Project short ID or title"),
) -> None:
    """Show all content linked to a project."""
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.services.linking import get_linking_service
        from cognitex.services.tasks import get_project_service

        await init_postgres()
        await init_neo4j()

        try:
            # Resolve project
            project_svc = get_project_service()
            projects = await project_svc.list()
            project_id = None

            try:
                idx = int(project) - 1
                if 0 <= idx < len(projects):
                    project_id = projects[idx]["id"]
            except ValueError:
                for p in projects:
                    if project.lower() in p["title"].lower():
                        project_id = p["id"]
                        break

            if not project_id:
                console.print(f"[red]Project not found: {project}[/red]")
                return

            linking = get_linking_service()
            summary = await linking.get_project_content_summary(project_id)

            if not summary:
                console.print(f"[red]Project not found in graph[/red]")
                return

            console.print(f"\n[bold]{summary['project_title']}[/bold]\n")

            table = Table(show_header=False)
            table.add_column("Type", style="cyan")
            table.add_column("Count", justify="right")

            table.add_row("Documents", str(summary["documents"]))
            table.add_row("Emails", str(summary["emails"]))
            table.add_row("Repositories", str(summary["repositories"]))
            table.add_row("Tasks", str(summary["tasks"]))
            table.add_row("Team Members", str(summary["members"]))

            console.print(table)

        finally:
            await close_postgres()
            await close_neo4j()

    asyncio.run(run())


@app.command("analyze-links")
def analyze_links_cmd(
    node_type: str = typer.Option(None, "--type", "-t", help="Node type to analyze (Task, Project, Goal)"),
    limit: int = typer.Option(10, "--limit", "-l", help="Number of nodes to analyze"),
    auto_apply: bool = typer.Option(False, "--auto", "-a", help="Auto-apply high-confidence links (>=80%)"),
    show_unlinked: bool = typer.Option(False, "--show-unlinked", "-u", help="Only show unlinked nodes without analyzing"),
) -> None:
    """Analyze unlinked nodes and suggest relationships using AI.

    Uses the LLM to examine tasks, projects, and goals that have few or no
    relationships and suggests appropriate links based on their names and descriptions.

    Examples:
      cognitex analyze-links                    # Analyze 10 unlinked nodes
      cognitex analyze-links --type Task        # Only analyze tasks
      cognitex analyze-links --limit 20 --auto  # Analyze 20, auto-apply high confidence
      cognitex analyze-links --show-unlinked    # Just show what needs linking
    """
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.services.linking import get_linking_service, init_linking_schema

        await init_postgres()
        await init_neo4j()

        try:
            async for session in get_session():
                await init_linking_schema(session)

            linking = get_linking_service()

            if show_unlinked:
                # Just show unlinked nodes
                unlinked = await linking.get_unlinked_nodes(node_type=node_type, limit=limit)

                if not unlinked:
                    console.print("[dim]No unlinked nodes found[/dim]")
                    return

                console.print(f"\n[bold]Unlinked Nodes ({len(unlinked)} found)[/bold]\n")

                table = Table()
                table.add_column("Type")
                table.add_column("Name")
                table.add_column("Relationships")
                table.add_column("Description")

                for node in unlinked:
                    table.add_row(
                        node["type"],
                        (node.get("name") or "-")[:40],
                        str(node.get("rel_count", 0)),
                        (node.get("description") or "-")[:50],
                    )

                console.print(table)
                console.print("\n[dim]Use 'analyze-links' without --show-unlinked to generate suggestions[/dim]")
                return

            # Run analysis
            console.print(f"[cyan]Analyzing unlinked nodes...[/cyan]")
            if auto_apply:
                console.print("[yellow]Auto-apply enabled: High confidence links (>=80%) will be created automatically[/yellow]")

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                task = progress.add_task("Analyzing with LLM...", total=None)

                async for session in get_session():
                    suggestions = await linking.analyze_and_suggest_links(
                        session,
                        node_type=node_type,
                        limit=limit,
                        auto_apply=auto_apply,
                    )

                progress.update(task, completed=True)

            if not suggestions:
                console.print("\n[dim]No suggestions generated. Nodes may already be well-linked or no matches found.[/dim]")
                return

            # Display results
            console.print(f"\n[bold]Link Suggestions ({len(suggestions)} generated)[/bold]\n")

            table = Table()
            table.add_column("Source")
            table.add_column("Target")
            table.add_column("Confidence")
            table.add_column("Status")
            table.add_column("Reason")

            auto_count = 0
            pending_count = 0

            for s in suggestions:
                status = s.get("status", "pending")
                if status == "auto_applied":
                    auto_count += 1
                    status_display = "[green]auto-applied[/green]"
                else:
                    pending_count += 1
                    status_display = "[yellow]pending[/yellow]"

                table.add_row(
                    s["source"][:30],
                    s["target"][:30],
                    f"{s['confidence']:.0%}",
                    status_display,
                    (s.get("reason") or "-")[:35],
                )

            console.print(table)

            # Summary
            console.print(f"\n[bold]Summary:[/bold]")
            if auto_count > 0:
                console.print(f"  [green]Auto-applied: {auto_count}[/green]")
            if pending_count > 0:
                console.print(f"  [yellow]Pending approval: {pending_count}[/yellow]")
                console.print("\n[dim]Use 'cognitex link-suggestions' to review and approve pending suggestions[/dim]")

        finally:
            await close_postgres()
            await close_neo4j()

    asyncio.run(run())


@app.command("init-linking")
def init_linking_cmd() -> None:
    """Initialize linking schema (folder/contact mappings, suggestions)."""
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.services.linking import init_linking_schema

        await init_postgres()

        try:
            async for session in get_session():
                await init_linking_schema(session)
            console.print("[green]✓ Linking schema initialized[/green]")
            console.print("\nNew tables available:")
            console.print("  • folder_project_mappings - Auto-link docs from folders")
            console.print("  • contact_project_mappings - Auto-link emails from contacts")
            console.print("  • suggested_links - AI-suggested links queue")

        finally:
            await close_postgres()

    asyncio.run(run())


@app.command("drive-metadata")
def drive_metadata_cmd(
    limit: int = typer.Option(None, "--limit", "-l", help="Limit number of files to index"),
) -> None:
    """Index all Drive files for metadata (name, folder path, etc.)."""
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.services.drive_metadata import DriveMetadataIndexer

        await init_postgres()

        try:
            indexer = DriveMetadataIndexer()
            console.print("[cyan]Indexing Drive file metadata...[/cyan]")

            stats = await indexer.index_all_files(limit=limit)

            console.print("\n[green]✓ Drive metadata indexing complete[/green]")
            console.print(f"  Total files: {stats['total']}")
            console.print(f"  Priority folder files: {stats['priority']}")
            if stats['errors'] > 0:
                console.print(f"  [yellow]Errors: {stats['errors']}[/yellow]")

        finally:
            await close_postgres()

    asyncio.run(run())


@app.command("github-metadata")
def github_metadata_cmd(
    limit: int = typer.Option(None, "--limit", "-l", help="Limit number of repos to index"),
    include_files: bool = typer.Option(False, "--files", "-f", help="Also index file metadata for priority repos"),
) -> None:
    """Index all GitHub repos for metadata (name, language, etc.)."""
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.services.github_metadata import GithubMetadataIndexer

        await init_postgres()

        try:
            indexer = GithubMetadataIndexer()
            console.print("[cyan]Indexing GitHub repository metadata...[/cyan]")

            stats = await indexer.index_all_repos(limit=limit)

            console.print("\n[green]✓ GitHub repo metadata indexing complete[/green]")
            console.print(f"  Total repos: {stats['total']}")
            console.print(f"  Priority repos: {stats['priority']}")
            if stats['errors'] > 0:
                console.print(f"  [yellow]Errors: {stats['errors']}[/yellow]")

            # Optionally index files for priority repos
            if include_files and stats['priority'] > 0:
                console.print("\n[cyan]Indexing files for priority repos...[/cyan]")
                file_stats = await indexer.index_priority_repo_files()
                console.print(f"  Repos processed: {file_stats['repos_processed']}")
                console.print(f"  Files indexed: {file_stats['files_indexed']}")
                if file_stats['errors'] > 0:
                    console.print(f"  [yellow]Errors: {file_stats['errors']}[/yellow]")

        finally:
            await close_postgres()

    asyncio.run(run())


@app.command("semantic-analyze")
def semantic_analyze_cmd(
    folder: str = typer.Option(None, "--folder", "-f", help="Specific folder to analyze"),
    limit: int = typer.Option(None, "--limit", "-l", help="Limit number of files"),
    force: bool = typer.Option(False, "--force", help="Re-analyze already processed files"),
    max_size: int = typer.Option(5, "--max-size", "-s", help="Max file size in MB (default 5)"),
) -> None:
    """Run semantic analysis on priority folder documents using Gemini."""
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.services.semantic_analysis import SemanticAnalyzer

        await init_postgres()
        await init_neo4j()

        try:
            analyzer = SemanticAnalyzer()
            console.print("[cyan]Running semantic analysis on priority documents...[/cyan]")
            if folder:
                console.print(f"  Folder filter: {folder}")
            console.print(f"  Max file size: {max_size}MB")

            stats = await analyzer.analyze_priority_files(
                folder=folder,
                limit=limit,
                skip_analyzed=not force,
                max_file_size_mb=max_size,
            )

            console.print("\n[green]✓ Semantic analysis complete[/green]")
            console.print(f"  Documents analyzed: {stats['analyzed']}")
            console.print(f"  Skipped (no content): {stats['skipped']}")
            if stats.get('skipped_size', 0) > 0:
                console.print(f"  Skipped (too large): {stats['skipped_size']}")
            if stats['errors'] > 0:
                console.print(f"  [yellow]Errors: {stats['errors']}[/yellow]")

        finally:
            await close_postgres()
            await close_neo4j()

    asyncio.run(run())


@app.command("semantic-status")
def semantic_status_cmd() -> None:
    """Show semantic analysis progress and statistics."""
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from sqlalchemy import text

        await init_postgres()

        try:
            async for session in get_session():
                # Get drive files stats
                try:
                    result = await session.execute(text("""
                        SELECT
                            COUNT(*) as total_files,
                            COUNT(*) FILTER (WHERE is_priority) as priority_files,
                            COUNT(DISTINCT folder_path) as unique_folders
                        FROM drive_files
                    """))
                    drive_stats = result.mappings().one_or_none()
                except Exception:
                    drive_stats = None

                # Get analysis stats
                try:
                    result = await session.execute(text("""
                        SELECT COUNT(*) as analyzed_count
                        FROM document_analysis
                    """))
                    analysis_stats = result.mappings().one_or_none()
                except Exception:
                    analysis_stats = None

                # Get concept/topic counts from analysis
                try:
                    result = await session.execute(text("""
                        SELECT
                            SUM(jsonb_array_length(key_concepts)) as total_concepts,
                            SUM(jsonb_array_length(topics)) as total_topics
                        FROM document_analysis
                    """))
                    semantic_stats = result.mappings().one_or_none()
                except Exception:
                    semantic_stats = None

                console.print("\n[bold]Drive File Index[/bold]")
                if drive_stats and drive_stats['total_files']:
                    console.print(f"  Total indexed files: {drive_stats['total_files']}")
                    console.print(f"  Priority folder files: {drive_stats['priority_files']}")
                    console.print(f"  Unique folders: {drive_stats['unique_folders']}")
                else:
                    console.print("  [dim]No files indexed yet. Run: cognitex drive-metadata[/dim]")

                console.print("\n[bold]Semantic Analysis[/bold]")
                if analysis_stats and analysis_stats['analyzed_count']:
                    analyzed = analysis_stats['analyzed_count']
                    priority = drive_stats['priority_files'] if drive_stats else 0
                    console.print(f"  Documents analyzed: {analyzed}")
                    if priority > 0:
                        pct = (analyzed / priority) * 100
                        console.print(f"  Progress: {pct:.1f}% of priority files")

                    if semantic_stats:
                        console.print(f"  Total concepts extracted: {semantic_stats['total_concepts'] or 0}")
                        console.print(f"  Total topics extracted: {semantic_stats['total_topics'] or 0}")
                else:
                    console.print("  [dim]No documents analyzed yet. Run: cognitex semantic-analyze[/dim]")

                # Show error count
                try:
                    result = await session.execute(text("""
                        SELECT COUNT(DISTINCT file_id) as error_count FROM analysis_errors
                    """))
                    error_stats = result.mappings().one_or_none()
                    if error_stats and error_stats['error_count']:
                        console.print(f"  [yellow]Files with errors: {error_stats['error_count']} (run: cognitex semantic-errors)[/yellow]")
                except Exception:
                    pass

        finally:
            await close_postgres()

    asyncio.run(run())


@app.command("semantic-errors")
def semantic_errors_cmd(
    limit: int = typer.Option(50, "--limit", "-l", help="Max errors to show"),
    retry: bool = typer.Option(False, "--retry", "-r", help="Retry failed files"),
) -> None:
    """Show files that failed semantic analysis (for retry)."""
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres, get_session
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.services.semantic_analysis import ensure_tables
        from sqlalchemy import text

        await init_postgres()
        if retry:
            # Neo4j needed for save_analysis() -> _update_graph()
            await init_neo4j()
        await ensure_tables()  # Ensure analysis_errors table exists

        try:
            async for session in get_session():
                # Get failed files with their error info
                result = await session.execute(text("""
                    SELECT
                        ae.file_id,
                        df.name as file_name,
                        df.folder_path,
                        ae.error_type,
                        ae.error_message,
                        ae.created_at,
                        COUNT(*) OVER (PARTITION BY ae.file_id) as attempt_count
                    FROM analysis_errors ae
                    LEFT JOIN drive_files df ON ae.file_id = df.id
                    WHERE ae.file_id NOT IN (SELECT file_id FROM document_analysis)
                    ORDER BY ae.created_at DESC
                    LIMIT :limit
                """), {"limit": limit})
                errors = result.mappings().all()

                if not errors:
                    console.print("[green]No failed analyses to show.[/green]")
                    return

                # Get unique file count
                unique_files = len(set(e['file_id'] for e in errors))
                console.print(f"\n[bold]Analysis Errors[/bold] ({unique_files} unique files)\n")

                table = Table(show_header=True, header_style="bold")
                table.add_column("File Name", style="cyan", max_width=40)
                table.add_column("Folder", style="dim", max_width=30)
                table.add_column("Error Type", style="yellow")
                table.add_column("Attempts", justify="right")
                table.add_column("Last Error", style="red", max_width=50)

                seen_files = set()
                for error in errors:
                    if error['file_id'] in seen_files:
                        continue
                    seen_files.add(error['file_id'])

                    file_name = error['file_name'] or error['file_id'][:20] + "..."
                    folder = error['folder_path'] or "-"
                    if len(folder) > 30:
                        folder = "..." + folder[-27:]

                    error_msg = error['error_message'] or "-"
                    if len(error_msg) > 50:
                        error_msg = error_msg[:47] + "..."

                    table.add_row(
                        file_name,
                        folder,
                        error['error_type'],
                        str(error['attempt_count']),
                        error_msg,
                    )

                console.print(table)

                if retry:
                    console.print("\n[bold]Retrying failed files...[/bold]")
                    from cognitex.services.semantic_analysis import SemanticAnalyzer
                    from cognitex.services.drive import get_drive_service

                    analyzer = SemanticAnalyzer()
                    drive = get_drive_service()

                    # Get unique file IDs that haven't been analyzed
                    result = await session.execute(text("""
                        SELECT DISTINCT ae.file_id, df.mime_type
                        FROM analysis_errors ae
                        JOIN drive_files df ON ae.file_id = df.id
                        WHERE ae.file_id NOT IN (SELECT file_id FROM document_analysis)
                        LIMIT :limit
                    """), {"limit": limit})
                    retry_files = result.mappings().all()

                    success = 0
                    failed = 0
                    for file in retry_files:
                        try:
                            content = drive.get_file_content(file['file_id'], file['mime_type'])
                            if content:
                                analysis = await analyzer.analyze_document(file['file_id'], content)
                                if analysis:
                                    await analyzer.save_analysis(analysis)
                                    success += 1
                                    console.print(f"  [green]OK[/green] {file['file_id'][:20]}...")
                                else:
                                    failed += 1
                            else:
                                failed += 1
                        except Exception as e:
                            failed += 1
                            console.print(f"  [red]FAIL[/red] {file['file_id'][:20]}... - {str(e)[:50]}")

                    console.print(f"\n[bold]Retry complete:[/bold] {success} succeeded, {failed} failed")
                else:
                    console.print("\n[dim]To retry failed files: cognitex semantic-errors --retry[/dim]")

        finally:
            await close_postgres()
            if retry:
                await close_neo4j()

    asyncio.run(run())


# =============================================================================
# Phase 4: Learning System Commands
# =============================================================================

@app.command("learning-stats")
def learning_stats_cmd() -> None:
    """Show learning system statistics and insights."""
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.agent.learning import init_learning_system, get_learning_system

        await init_postgres()
        await init_neo4j()

        try:
            await init_learning_system()
            ls = get_learning_system()
            summary = await ls.get_learning_summary()

            console.print("\n[bold]Learning System Summary[/bold]\n")

            # Proposal stats
            proposal_stats = summary.get("proposals", {}).get("stats", {})
            if proposal_stats:
                table = Table(title="Proposal Learning", show_header=False)
                table.add_column("Metric", style="cyan")
                table.add_column("Value", justify="right")

                table.add_row("Total proposals", str(proposal_stats.get("total", 0)))
                table.add_row("Approved", str(proposal_stats.get("approved", 0)))
                table.add_row("Rejected", str(proposal_stats.get("rejected", 0)))
                table.add_row("Pending", str(proposal_stats.get("pending", 0)))
                table.add_row("Approval rate", f"{proposal_stats.get('approval_rate', 0):.1f}%")

                console.print(table)
                console.print()

            # Duration calibration
            duration = summary.get("duration", {}).get("overall", {})
            if duration:
                table = Table(title="Duration Calibration", show_header=False)
                table.add_column("Metric", style="cyan")
                table.add_column("Value", justify="right")

                table.add_row("Total timing records", str(duration.get("total_records", 0)))
                table.add_row("Hours tracked", str(duration.get("total_hours_tracked", 0)))
                table.add_row("Overall pace factor", f"{duration.get('overall_pace_factor', 1.0):.2f}x")
                table.add_row("Projects tracked", str(duration.get("projects_tracked", 0)))

                console.print(table)
                console.print()

            # Deferral risk
            deferrals = summary.get("deferrals", {})
            if deferrals.get("high_risk_tasks"):
                console.print("[bold]High Deferral Risk Tasks[/bold]")
                for task in deferrals["high_risk_tasks"][:5]:
                    console.print(
                        f"  [yellow]{task['risk_score']:.0%}[/yellow] "
                        f"{task['title'][:50]} "
                        f"[dim]({', '.join(task.get('risk_factors', [])[:2])})[/dim]"
                    )
                console.print()

            # Rule stats
            rules = summary.get("rules", {}).get("stats", {})
            if rules:
                table = Table(title="Preference Rules", show_header=False)
                table.add_column("Metric", style="cyan")
                table.add_column("Value", justify="right")

                table.add_row("Total rules", str(rules.get("total", 0)))
                table.add_row("Validated", str(rules.get("validated", 0)))
                table.add_row("Active", str(rules.get("active", 0)))
                table.add_row("Candidate", str(rules.get("candidate", 0)))
                table.add_row("Deprecated", str(rules.get("deprecated", 0)))
                if rules.get("avg_success_rate"):
                    table.add_row("Avg success rate", f"{rules['avg_success_rate']:.1f}%")

                console.print(table)
                console.print()

            # Insights
            insights = summary.get("insights", [])
            if insights:
                console.print("[bold]Insights[/bold]")
                for insight in insights:
                    console.print(f"  [green]•[/green] {insight}")

        finally:
            await close_postgres()
            await close_neo4j()

    asyncio.run(run())


@app.command("learning-update")
def learning_update_cmd() -> None:
    """Run a policy update cycle (validate rules, extract patterns)."""
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.agent.learning import init_learning_system, get_learning_system

        await init_postgres()
        await init_neo4j()

        try:
            await init_learning_system()
            ls = get_learning_system()

            console.print("[bold]Running policy update...[/bold]")
            results = await ls.run_policy_update()

            console.print("\n[bold]Update Results[/bold]")

            validation = results.get("rules_validated", {})
            console.print(f"  Rules validated: {validation.get('validated', 0)}")
            console.print(f"  Rules promoted to active: {validation.get('promoted_to_active', 0)}")
            console.print(f"  Rules deprecated: {validation.get('deprecated', 0)}")
            console.print(f"  Rules extracted: {results.get('rules_extracted', 0)}")

            if results.get("error"):
                console.print(f"\n[red]Error: {results['error']}[/red]")
            else:
                console.print("\n[green]Policy update complete[/green]")

        finally:
            await close_postgres()
            await close_neo4j()

    asyncio.run(run())


@app.command("calibration")
def calibration_cmd() -> None:
    """Show duration calibration (personal pace factors)."""
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.services.tasks import get_calibration_summary

        await init_postgres()

        try:
            summary = await get_calibration_summary()

            console.print("\n[bold]Duration Calibration[/bold]\n")

            overall = summary.get("overall", {})
            if overall:
                console.print(f"Total timing records: {overall.get('total_records', 0)}")
                console.print(f"Hours tracked: {overall.get('total_hours_tracked', 0)}")
                console.print(f"Overall pace factor: {overall.get('overall_pace_factor', 1.0):.2f}x")
                console.print()

            by_project = summary.get("by_project", {})
            if by_project:
                table = Table(title="By Project", show_header=True)
                table.add_column("Project", style="cyan")
                table.add_column("Pace Factor", justify="right")
                table.add_column("Samples", justify="right")
                table.add_column("Interpretation")

                for project_id, cal in by_project.items():
                    pace = cal["pace_factor"]
                    if pace > 1.3:
                        interp = f"[red]{int((pace-1)*100)}% longer than estimated[/red]"
                    elif pace < 0.8:
                        interp = f"[green]{int((1-pace)*100)}% faster than estimated[/green]"
                    else:
                        interp = "[dim]accurate estimates[/dim]"

                    table.add_row(
                        project_id[:30],
                        f"{pace:.2f}x",
                        str(cal["sample_size"]),
                        interp,
                    )

                console.print(table)

            insights = summary.get("insights", [])
            if insights:
                console.print("\n[bold]Insights[/bold]")
                for insight in insights:
                    console.print(f"  [green]•[/green] {insight}")

        finally:
            await close_postgres()

    asyncio.run(run())


@app.command("deferral-risk")
def deferral_risk_cmd(
    min_risk: float = typer.Option(0.5, "--min", "-m", help="Minimum risk score"),
    limit: int = typer.Option(10, "--limit", "-l", help="Max tasks to show"),
) -> None:
    """Show tasks with high deferral risk."""
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.agent.state_model import get_high_risk_tasks

        await init_postgres()

        try:
            tasks = await get_high_risk_tasks(min_risk=min_risk, limit=limit)

            if not tasks:
                console.print(f"[green]No tasks with deferral risk >= {min_risk:.0%}[/green]")
                return

            console.print(f"\n[bold]High Deferral Risk Tasks[/bold] (>= {min_risk:.0%})\n")

            table = Table(show_header=True)
            table.add_column("Risk", style="yellow", justify="right")
            table.add_column("Title", style="cyan", max_width=40)
            table.add_column("Factors", style="dim", max_width=40)
            table.add_column("Intervention")

            for task in tasks:
                risk_color = "red" if task["risk_score"] >= 0.7 else "yellow"
                table.add_row(
                    f"[{risk_color}]{task['risk_score']:.0%}[/{risk_color}]",
                    task["title"][:40],
                    ", ".join(task.get("risk_factors", [])[:3]),
                    task.get("recommended_intervention") or "-",
                )

            console.print(table)

        finally:
            await close_postgres()

    asyncio.run(run())


@app.command("init-phase4")
def init_phase4_cmd() -> None:
    """Initialize Phase 4 learning system schema."""
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.db.phase4_schema import init_phase4_schema

        await init_postgres()
        await init_neo4j()

        try:
            console.print("[bold]Initializing Phase 4 schema...[/bold]")
            await init_phase4_schema()
            console.print("[green]Phase 4 schema initialized successfully[/green]")

        finally:
            await close_postgres()
            await close_neo4j()

    asyncio.run(run())


@app.command("consolidate")
def consolidate_cmd(
    days_back: int = typer.Option(1, "--days", "-d", help="Number of days to consolidate"),
    archive: bool = typer.Option(False, "--archive", "-a", help="Also archive old memories"),
    archive_days: int = typer.Option(30, "--archive-days", help="Archive memories older than N days"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be consolidated without writing"),
) -> None:
    """Run memory consolidation (dreaming) to summarize and prune memories.

    This command consolidates episodic memories from the specified number of days,
    generating daily summaries, extracting behavioral patterns, and optionally
    archiving old low-value memories.

    Examples:
        cognitex consolidate                 # Consolidate yesterday
        cognitex consolidate -d 7            # Consolidate last 7 days
        cognitex consolidate --archive       # Also archive old memories
        cognitex consolidate --dry-run       # Preview without writing
    """
    async def run():
        from cognitex.db.postgres import init_postgres, close_postgres
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.agent.consolidation import MemoryConsolidator

        await init_postgres()
        await init_neo4j()

        try:
            consolidator = MemoryConsolidator()

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Consolidating memories...", total=days_back + (1 if archive else 0))

                # Consolidate each day
                for i in range(days_back):
                    target_date = datetime.now() - timedelta(days=i + 1)
                    date_str = target_date.strftime("%Y-%m-%d")
                    progress.update(task, description=f"Consolidating {date_str}...")

                    if dry_run:
                        console.print(f"[dim]Would consolidate: {date_str}[/dim]")
                    else:
                        result = await consolidator.run_nightly_consolidation(
                            target_date=target_date
                        )
                        if result.get("summary_id"):
                            console.print(f"[green]✓[/green] {date_str}: {result.get('event_count', 0)} events consolidated")
                        else:
                            console.print(f"[yellow]○[/yellow] {date_str}: No events to consolidate")

                    progress.advance(task)

                # Archive old memories if requested
                if archive:
                    progress.update(task, description="Archiving old memories...")
                    if dry_run:
                        console.print(f"[dim]Would archive memories older than {archive_days} days[/dim]")
                    else:
                        archive_result = await consolidator.archive_old_memories(
                            older_than_days=archive_days
                        )
                        console.print(
                            f"[green]✓[/green] Archived {archive_result.get('archived_count', 0)} old memories"
                        )
                    progress.advance(task)

            console.print("\n[bold green]Consolidation complete![/bold green]")

        except Exception as e:
            console.print(f"[red]Error during consolidation: {e}[/red]")
            raise typer.Exit(1)

        finally:
            await close_postgres()
            await close_neo4j()

    asyncio.run(run())


# =============================================================================
# Bootstrap Commands - Personality, Identity, Context files
# =============================================================================

bootstrap_app = typer.Typer(
    name="bootstrap",
    help="Manage bootstrap files (SOUL, USER, AGENTS, TOOLS, MEMORY, IDENTITY, CONTEXT)",
    no_args_is_help=True,
)
app.add_typer(bootstrap_app, name="bootstrap")


@bootstrap_app.command("init")
def bootstrap_init() -> None:
    """Initialize bootstrap files in ~/.cognitex/bootstrap/."""

    async def run_init():
        from cognitex.agent.bootstrap import init_bootstrap, BOOTSTRAP_DIR

        await init_bootstrap()

        console.print("\n[bold green]Bootstrap files initialized![/bold green]")
        console.print(f"Location: {BOOTSTRAP_DIR}")
        console.print("\nFiles created:")
        console.print("  [cyan]SOUL.md[/cyan] - Communication style and voice")
        console.print("  [cyan]USER.md[/cyan] - Operator profile (replaces IDENTITY.md)")
        console.print("  [cyan]AGENTS.md[/cyan] - Operating constitution and safety rules")
        console.print("  [cyan]TOOLS.md[/cyan] - Infrastructure and tool configuration")
        console.print("  [cyan]MEMORY.md[/cyan] - Curated operational memory")
        console.print("  [cyan]IDENTITY.md[/cyan] - Legacy user context (fallback)")
        console.print("  [cyan]CONTEXT.md[/cyan] - Auto-updated ambient context")
        console.print("\nEdit these files to customize how Cognitex operates.")
        console.print("Use [bold]cognitex bootstrap edit soul[/bold] to open in your editor.")

    asyncio.run(run_init())


@bootstrap_app.command("edit")
def bootstrap_edit(
    file: str = typer.Argument(
        ..., help="File to edit: soul, user, agents, tools, memory, identity, or context"
    ),
) -> None:
    """Open a bootstrap file in your editor."""
    import os
    import subprocess

    from cognitex.agent.bootstrap import BOOTSTRAP_DIR

    file_map = {
        "soul": "SOUL.md",
        "user": "USER.md",
        "agents": "AGENTS.md",
        "tools": "TOOLS.md",
        "memory": "MEMORY.md",
        "identity": "IDENTITY.md",
        "context": "CONTEXT.md",
    }

    filename = file_map.get(file.lower())
    if not filename:
        console.print(f"[red]Unknown file: {file}[/red]")
        console.print(f"Valid options: {', '.join(file_map.keys())}")
        raise typer.Exit(1)

    filepath = BOOTSTRAP_DIR / filename

    if not filepath.exists():
        console.print(f"[yellow]File doesn't exist. Run 'cognitex bootstrap init' first.[/yellow]")
        raise typer.Exit(1)

    editor = os.environ.get("EDITOR", "nano")
    try:
        subprocess.run([editor, str(filepath)], check=True)
        console.print(f"[green]Saved {filename}[/green]")
    except subprocess.CalledProcessError:
        console.print(f"[red]Editor exited with error[/red]")
    except FileNotFoundError:
        console.print(f"[red]Editor not found: {editor}[/red]")
        console.print(f"Set EDITOR environment variable or edit directly: {filepath}")


@bootstrap_app.command("show")
def bootstrap_show(
    file: str = typer.Argument(
        None, help="File to show: soul, user, agents, tools, memory, identity, context (or all)"
    ),
) -> None:
    """Display bootstrap file contents."""

    async def run_show():
        from cognitex.agent.bootstrap import (
            BOOTSTRAP_FILES,
            get_bootstrap_loader,
            init_bootstrap,
        )

        await init_bootstrap()
        loader = get_bootstrap_loader()

        file_map = {
            "soul": "SOUL.md",
            "user": "USER.md",
            "agents": "AGENTS.md",
            "tools": "TOOLS.md",
            "memory": "MEMORY.md",
            "identity": "IDENTITY.md",
            "context": "CONTEXT.md",
        }

        if file:
            filename = file_map.get(file.lower())
            if not filename:
                console.print(f"[red]Unknown file: {file}[/red]")
                console.print(f"Valid options: {', '.join(file_map.keys())}")
                raise typer.Exit(1)
            files = [filename]
        else:
            files = list(BOOTSTRAP_FILES.keys())

        for filename in files:
            loaded = await loader.get_file(filename)
            if loaded:
                console.print(f"\n[bold cyan]═══ {filename} ═══[/bold cyan]")
                console.print(loaded.raw_content)
            else:
                console.print(f"\n[yellow]{filename} not found[/yellow]")

    asyncio.run(run_show())


# =============================================================================
# Skills Commands - Teachable agent behaviors
# =============================================================================

skills_app = typer.Typer(
    name="skills",
    help="Manage skills (teachable agent behaviors)",
    no_args_is_help=True,
)
app.add_typer(skills_app, name="skills")


@skills_app.command("list")
def skills_list() -> None:
    """List all available skills (bundled and user)."""

    async def run_list():
        from cognitex.agent.skills import init_skills, get_skills_loader

        await init_skills()
        loader = get_skills_loader()

        skills = await loader.list_skills()

        if not skills:
            console.print("[yellow]No skills found.[/yellow]")
            return

        table = Table(title="Available Skills")
        table.add_column("Name", style="cyan")
        table.add_column("Type", style="dim")
        table.add_column("Format", style="dim")
        table.add_column("Version", style="dim")
        table.add_column("Purpose", style="white")
        table.add_column("Rules", justify="right")
        table.add_column("Eligible", justify="center")

        for skill in skills:
            skill_type = "[green]user[/green]" if skill["is_user_skill"] else "[dim]bundled[/dim]"
            if skill.get("source") == "community":
                skill_type = "[magenta]community[/magenta]"
            eligible = "[green]yes[/green]" if skill.get("eligible", True) else "[red]no[/red]"
            table.add_row(
                skill["name"],
                skill_type,
                skill.get("format", "cognitex_legacy"),
                skill.get("version", "1.0.0"),
                skill["purpose"][:50] + "..." if len(skill["purpose"]) > 50 else skill["purpose"],
                str(skill["rules_count"]),
                eligible,
            )

        console.print(table)
        console.print("\n[dim]User skills override bundled skills with the same name.[/dim]")
        console.print("[dim]Use 'cognitex skills show <name>' to see full skill definition.[/dim]")

    asyncio.run(run_list())


@skills_app.command("show")
def skills_show(
    name: str = typer.Argument(..., help="Skill name to display"),
) -> None:
    """Display a skill's full definition."""

    async def run_show():
        from cognitex.agent.skills import init_skills, get_skills_loader

        await init_skills()
        loader = get_skills_loader()

        skill = await loader.get_skill(name)
        if not skill:
            console.print(f"[red]Skill not found: {name}[/red]")
            raise typer.Exit(1)

        skill_type = "[green]user skill[/green]" if skill.is_user_skill else "[dim]bundled skill[/dim]"
        console.print(f"\n[bold cyan]═══ {skill.name} ═══[/bold cyan] ({skill_type})")
        console.print(f"Path: {skill.path}")
        console.print("")
        console.print(skill.raw_content)

    asyncio.run(run_show())


@skills_app.command("edit")
def skills_edit(
    name: str = typer.Argument(..., help="Skill name to edit (creates if doesn't exist)"),
) -> None:
    """Edit a skill in your editor (creates user skill if needed)."""
    import os
    import subprocess

    from cognitex.agent.skills import USER_SKILLS_DIR, BUNDLED_SKILLS_DIR

    # Check user skill first, then bundled
    user_skill_path = USER_SKILLS_DIR / name / "SKILL.md"
    bundled_skill_path = BUNDLED_SKILLS_DIR / name / "SKILL.md"

    if user_skill_path.exists():
        filepath = user_skill_path
    elif bundled_skill_path.exists():
        # Copy bundled to user for editing
        console.print(f"[yellow]Copying bundled skill to user directory for editing...[/yellow]")
        user_skill_path.parent.mkdir(parents=True, exist_ok=True)
        user_skill_path.write_text(bundled_skill_path.read_text())
        filepath = user_skill_path
    else:
        # Create new user skill
        console.print(f"[yellow]Creating new skill: {name}[/yellow]")
        user_skill_path.parent.mkdir(parents=True, exist_ok=True)
        template = f"""# {name.replace('-', ' ').title()}

## Purpose
Describe what this skill helps the agent accomplish.

## What IS
- Example of what this skill should recognize
- Another example

## What is NOT
- Example of what this skill should ignore
- Another example

## Rules
1. First rule for the agent to follow
2. Second rule

## Examples

### Example 1
Input: ...
Output: ...
"""
        user_skill_path.write_text(template)
        filepath = user_skill_path

    editor = os.environ.get("EDITOR", "nano")
    try:
        subprocess.run([editor, str(filepath)], check=True)
        console.print(f"[green]Saved {name} skill[/green]")
    except subprocess.CalledProcessError:
        console.print(f"[red]Editor exited with error[/red]")


@skills_app.command("info")
def skills_info(
    name: str = typer.Argument(..., help="Skill name to inspect"),
) -> None:
    """Show detailed info about a skill (format, version, eligibility, requires)."""

    async def run_info():
        from cognitex.agent.skills import init_skills, get_skills_loader

        await init_skills()
        loader = get_skills_loader()

        skill = await loader.get_skill(name)
        if not skill:
            console.print(f"[red]Skill not found: {name}[/red]")
            raise typer.Exit(1)

        skill_type = "[green]user[/green]" if skill.is_user_skill else "[dim]bundled[/dim]"
        if skill.source == "community":
            skill_type = "[magenta]community[/magenta]"

        console.print(f"\n[bold cyan]{skill.name}[/bold cyan] ({skill_type})")
        console.print(f"  Format:      {skill.format}")
        console.print(f"  Version:     {skill.version}")
        console.print(f"  Path:        {skill.path}")

        if skill.description:
            console.print(f"  Description: {skill.description}")
        if skill.purpose:
            console.print(f"  Purpose:     {skill.purpose[:80]}")

        eligible_str = "[green]yes[/green]" if skill.eligible else f"[red]no[/red] — {skill.ineligibility_reason}"
        console.print(f"  Eligible:    {eligible_str}")

        if skill.requires_bins:
            console.print(f"  Bins:        {', '.join(skill.requires_bins)}")
        if skill.requires_env:
            console.print(f"  Env vars:    {', '.join(skill.requires_env)}")
        if skill.requires_config:
            console.print(f"  Config keys: {', '.join(skill.requires_config)}")

        console.print(f"  Rules:       {len(skill.rules)}")
        console.print(f"  Examples:    {len(skill.examples)}")

    asyncio.run(run_info())


@skills_app.command("search")
def skills_search(
    query: str = typer.Argument(..., help="Search term for community skills"),
) -> None:
    """Search the community skill registry."""

    async def run_search():
        from cognitex.services.skill_registry import get_skill_registry

        registry = get_skill_registry()
        results = await registry.search(query)

        if not results:
            console.print(f"[yellow]No community skills matching '{query}'.[/yellow]")
            console.print("[dim]Run 'cognitex skills sync' first to download the registry.[/dim]")
            return

        table = Table(title=f"Community Skills matching '{query}'")
        table.add_column("Slug", style="cyan")
        table.add_column("Version", style="dim")
        table.add_column("Description", style="white")
        table.add_column("Installed", justify="center")

        for listing in results:
            installed = "[green]yes[/green]" if listing.installed else "[dim]no[/dim]"
            table.add_row(listing.slug, listing.version, listing.description[:60], installed)

        console.print(table)

    asyncio.run(run_search())


@skills_app.command("install")
def skills_install(
    slug: str = typer.Argument(..., help="Community skill slug to install"),
) -> None:
    """Install a skill from the community registry."""

    async def run_install():
        from cognitex.services.skill_registry import get_skill_registry

        registry = get_skill_registry()
        success = await registry.install(slug)

        if success:
            console.print(f"[green]Installed community skill: {slug}[/green]")
        else:
            console.print(f"[red]Failed to install '{slug}'. Run 'cognitex skills sync' first.[/red]")

    asyncio.run(run_install())


@skills_app.command("update")
def skills_update(
    slug: str = typer.Argument(None, help="Specific skill slug to update (default: all)"),
    all_skills: bool = typer.Option(False, "--all", help="Update all community skills"),
) -> None:
    """Update installed community skills."""

    async def run_update():
        from cognitex.services.skill_registry import get_skill_registry

        registry = get_skill_registry()

        target = None if all_skills else slug
        updated = await registry.update(target)

        if updated:
            console.print(f"[green]Updated {len(updated)} skill(s): {', '.join(updated)}[/green]")
        else:
            console.print("[yellow]No skills were updated.[/yellow]")

    asyncio.run(run_update())


@skills_app.command("sync")
def skills_sync() -> None:
    """Clone or pull the community skill registry."""

    async def run_sync():
        from cognitex.services.skill_registry import get_skill_registry

        registry = get_skill_registry()
        try:
            count = await registry.sync_registry()
            console.print(f"[green]Registry synced — {count} skill(s) available.[/green]")
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")

    asyncio.run(run_sync())


# =============================================================================
# Memory Commands - Daily logs and curated knowledge
# =============================================================================

memory_app = typer.Typer(
    name="memory",
    help="Manage memory files (daily logs and curated knowledge)",
    no_args_is_help=True,
)
app.add_typer(memory_app, name="memory")


@memory_app.command("init")
def memory_init() -> None:
    """Initialize memory directory and files."""

    async def run_init():
        from cognitex.services.memory_files import init_memory_files, MEMORY_DIR

        await init_memory_files()

        console.print("\n[bold green]Memory system initialized![/bold green]")
        console.print(f"Location: {MEMORY_DIR}")
        console.print("\nFiles:")
        console.print("  [cyan]MEMORY.md[/cyan] - Curated long-term memory (you edit this)")
        console.print("  [cyan]YYYY-MM-DD.md[/cyan] - Daily logs (agent writes these)")
        console.print("\nThe agent will automatically record observations to daily logs.")
        console.print("You can promote important entries to MEMORY.md for permanence.")

    asyncio.run(run_init())


@memory_app.command("today")
def memory_today() -> None:
    """Show today's memory log."""

    async def run_show():
        from datetime import date
        from cognitex.services.memory_files import init_memory_files, get_memory_file_service

        await init_memory_files()
        service = get_memory_file_service()

        log = await service.get_daily_log(date.today())

        if not log or not log.entries:
            console.print("[yellow]No entries for today yet.[/yellow]")
            return

        console.print(f"\n[bold cyan]═══ {date.today().isoformat()} ═══[/bold cyan]")
        console.print(f"{len(log.entries)} entries\n")

        for entry in log.entries:
            time_str = entry.timestamp.strftime("%H:%M")
            console.print(f"[bold]{time_str} - {entry.category}[/bold]")
            console.print(f"  {entry.content[:200]}...")
            if entry.tags:
                console.print(f"  [dim]Tags: {', '.join(entry.tags)}[/dim]")
            console.print()

    asyncio.run(run_show())


@memory_app.command("recent")
def memory_recent(
    days: int = typer.Option(7, "--days", "-d", help="Number of days to show"),
) -> None:
    """Show recent memory entries."""

    async def run_recent():
        from cognitex.services.memory_files import init_memory_files, get_memory_file_service

        await init_memory_files()
        service = get_memory_file_service()

        logs = await service.get_recent_logs(days=days)

        if not logs:
            console.print(f"[yellow]No entries in the last {days} days.[/yellow]")
            return

        total_entries = sum(len(log.entries) for log in logs)
        console.print(f"\n[bold]Recent Memory ({total_entries} entries)[/bold]\n")

        for log in logs:
            if log.entries:
                console.print(f"[cyan]── {log.date.isoformat()} ──[/cyan]")
                for entry in log.entries:
                    time_str = entry.timestamp.strftime("%H:%M")
                    content_preview = entry.content[:80].replace("\n", " ")
                    console.print(f"  {time_str} [{entry.category}] {content_preview}...")
                console.print()

    asyncio.run(run_recent())


@memory_app.command("write")
def memory_write(
    content: str = typer.Argument(..., help="Memory content to record"),
    category: str = typer.Option("User Note", "--category", "-c", help="Entry category"),
) -> None:
    """Write a memory entry to today's log."""

    async def run_write():
        from cognitex.services.memory_files import init_memory_files, get_memory_file_service

        await init_memory_files()
        service = get_memory_file_service()

        entry = await service.write_entry(
            content=content,
            category=category,
            source="user",
            sync_to_graph=True,
        )

        console.print(f"[green]Memory recorded[/green]")
        console.print(f"  ID: {entry.id}")
        console.print(f"  Category: {entry.category}")
        if entry.tags:
            console.print(f"  Tags: {', '.join(entry.tags)}")

    asyncio.run(run_write())


@memory_app.command("curated")
def memory_curated() -> None:
    """Show curated long-term memory."""

    async def run_show():
        from cognitex.services.memory_files import init_memory_files, get_memory_file_service

        await init_memory_files()
        service = get_memory_file_service()

        content = await service.get_curated_memory()

        if not content or len(content.strip()) < 50:
            console.print("[yellow]Curated memory is empty or minimal.[/yellow]")
            console.print("Edit ~/.cognitex/memory/MEMORY.md to add long-term knowledge.")
            return

        console.print("\n[bold cyan]═══ Curated Memory ═══[/bold cyan]")
        console.print(content)

    asyncio.run(run_show())


@memory_app.command("edit")
def memory_edit() -> None:
    """Edit curated memory in your editor."""
    import os
    import subprocess

    from cognitex.services.memory_files import MEMORY_DIR

    filepath = MEMORY_DIR / "MEMORY.md"

    if not filepath.exists():
        console.print("[yellow]Memory file doesn't exist. Run 'cognitex memory init' first.[/yellow]")
        raise typer.Exit(1)

    editor = os.environ.get("EDITOR", "nano")
    try:
        subprocess.run([editor, str(filepath)], check=True)
        console.print("[green]Saved curated memory[/green]")
    except subprocess.CalledProcessError:
        console.print("[red]Editor exited with error[/red]")


@memory_app.command("search")
def memory_search(
    query: str = typer.Argument(..., help="Search query"),
    days: int = typer.Option(30, "--days", "-d", help="Days to search"),
) -> None:
    """Search memory entries."""

    async def run_search():
        from cognitex.services.memory_files import init_memory_files, get_memory_file_service

        await init_memory_files()
        service = get_memory_file_service()

        results = await service.search_memories(query=query, days=days)

        if not results:
            console.print(f"[yellow]No matches for '{query}'[/yellow]")
            return

        console.print(f"\n[bold]Found {len(results)} matches[/bold]\n")

        for entry in results[:20]:
            time_str = entry.timestamp.strftime("%Y-%m-%d %H:%M")
            content_preview = entry.content[:100].replace("\n", " ")
            console.print(f"[cyan]{time_str}[/cyan] [{entry.category}]")
            console.print(f"  {content_preview}...")
            console.print()

    asyncio.run(run_search())


if __name__ == "__main__":
    app()
