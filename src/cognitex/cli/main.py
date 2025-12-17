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
                driver = get_driver()
                async with driver.session() as session:
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
                driver = get_driver()
                async with driver.session() as session:
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
            run_priority_folder_indexing,
        )

        await init_neo4j()
        await init_graph_schema()

        try:
            if not skip_metadata:
                if folder:
                    # Sync specific folder
                    console.print(f"[bold]Syncing folder: {folder}[/bold]")
                    result = await run_drive_folder_sync(folder)
                else:
                    # Sync all Drive metadata
                    console.print("[bold]Syncing Drive metadata...[/bold]")
                    result = await run_drive_metadata_sync()

                console.print(f"\n[green]Sync complete![/green]")
                console.print(f"  Total files: {result.get('total', 0)}")
                console.print(f"  Successfully synced: {result.get('success', 0)}")
                if result.get('failed', 0) > 0:
                    console.print(f"  [yellow]Failed: {result.get('failed', 0)}[/yellow]")

            # Index priority folders if requested
            if index_priority:
                console.print("\n[bold]Indexing priority folders...[/bold]")
                await init_postgres()

                try:
                    async for pg_session in get_session():
                        index_result = await run_priority_folder_indexing(
                            pg_session,
                            limit=limit,
                        )

                        console.print(f"\n[green]Indexing complete![/green]")
                        console.print(f"  Documents processed: {index_result.get('total', 0)}")
                        console.print(f"  Indexed: {index_result.get('indexed', 0)}")
                        console.print(f"  Skipped: {index_result.get('skipped', 0)}")

                        if index_result.get('by_folder'):
                            console.print("\n  By folder:")
                            for folder_name, stats in index_result['by_folder'].items():
                                console.print(f"    {folder_name}: {stats['indexed']} indexed, {stats['skipped']} skipped")

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
    webhook_url: str = typer.Option(None, "--webhook", "-w", help="Public HTTPS webhook URL"),
    pubsub_topic: str = typer.Option(None, "--pubsub", "-p", help="Google Cloud Pub/Sub topic for Gmail"),
    gmail: bool = typer.Option(True, "--gmail/--no-gmail", help="Set up Gmail watch"),
    calendar: bool = typer.Option(True, "--calendar/--no-calendar", help="Set up Calendar watch"),
    drive: bool = typer.Option(True, "--drive/--no-drive", help="Set up Drive watch"),
) -> None:
    """Set up Google push notification watches for real-time updates."""
    console.print("[bold]Setting up Google Push Notification Watches[/bold]\n")

    if not webhook_url and (calendar or drive):
        console.print("[yellow]Warning: No webhook URL provided. Calendar and Drive watches require a public HTTPS endpoint.[/yellow]")
        console.print("[dim]Use --webhook https://your-domain.com or set up ngrok for development.[/dim]\n")

    async def setup_watches():
        from cognitex.services.push_notifications import get_watch_manager

        watch_manager = get_watch_manager(webhook_url)
        results = {}

        if gmail:
            console.print("[cyan]Setting up Gmail watch...[/cyan]")
            if pubsub_topic:
                result = await watch_manager.setup_gmail_watch(pubsub_topic)
            else:
                console.print("  [yellow]Skipping Gmail - requires --pubsub topic[/yellow]")
                result = {"skipped": "No Pub/Sub topic"}
            results["Gmail"] = result

        if calendar and webhook_url:
            console.print("[cyan]Setting up Calendar watch...[/cyan]")
            result = await watch_manager.setup_calendar_watch()
            results["Calendar"] = result
        elif calendar:
            results["Calendar"] = {"skipped": "No webhook URL"}

        if drive and webhook_url:
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
                if source_event.get('start_time'):
                    console.print(f"  When: {str(source_event['start_time'])[:16]}")

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
            table.add_column("Status", style="green", width=10)
            table.add_column("Tasks", style="yellow", width=8)
            table.add_column("Target", style="magenta", width=12)

            for i, project in enumerate(project_list, 1):
                task_count = project.get('task_count', 0)
                done_count = project.get('done_count', 0)
                task_str = f"{done_count}/{task_count}"

                target = project.get('target_date')
                target_str = str(target)[:10] if target else "-"

                table.add_row(
                    str(i),
                    project['title'][:32],
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


if __name__ == "__main__":
    app()
