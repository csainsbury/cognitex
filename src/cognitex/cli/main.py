"""CLI entry point using Typer."""

import asyncio
import logging

import structlog
import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="cognitex",
    help="Personal agent system for cognitive load management",
    no_args_is_help=True,
)
console = Console()


@app.callback()
def main_callback(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show debug output"),
) -> None:
    """Configure logging for CLI commands."""
    log_level = logging.DEBUG if verbose else logging.WARNING

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
    )


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
        from cognitex.services.tasks import get_project_service

        await init_neo4j()

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

            table = Table(title=f"Projects ({len(project_list)})")
            table.add_column("ID", style="cyan", width=16)
            table.add_column("Title", style="white", width=30)
            table.add_column("Status", style="green", width=10)
            table.add_column("Tasks", style="yellow", width=10)
            table.add_column("Target", style="magenta", width=12)

            for project in project_list:
                task_count = project.get('task_count', 0)
                done_count = project.get('done_count', 0)
                task_str = f"{done_count}/{task_count}"

                target = project.get('target_date')
                target_str = str(target)[:10] if target else "-"

                table.add_row(
                    project['id'],
                    project['title'][:30],
                    project.get('status', 'active'),
                    task_str,
                    target_str,
                )

            console.print(table)

        finally:
            await close_neo4j()

    asyncio.run(list_projects())


@app.command("project-show")
def project_show(
    project_id: str = typer.Argument(..., help="Project ID to show"),
    with_tasks: bool = typer.Option(False, "--tasks", "-t", help="Show project tasks"),
) -> None:
    """Show detailed project information."""
    async def show_project():
        from cognitex.db.neo4j import init_neo4j, close_neo4j
        from cognitex.services.tasks import get_project_service

        await init_neo4j()

        try:
            project_service = get_project_service()
            project = await project_service.get(project_id)

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
                tasks = await project_service.get_tasks(project_id, include_done=True)
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
            await close_neo4j()

    asyncio.run(show_project())


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


if __name__ == "__main__":
    app()
