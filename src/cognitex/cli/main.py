"""CLI entry point using Typer."""

import asyncio

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="cognitex",
    help="Personal agent system for cognitive load management",
    no_args_is_help=True,
)
console = Console()


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
) -> None:
    """List tasks from the graph."""
    from cognitex.services.google_auth import check_credentials_status

    creds_status = check_credentials_status()
    if not creds_status["credentials_valid"]:
        console.print("[red]Not authenticated. Run 'cognitex auth' first.[/red]")
        raise typer.Exit(1)

    async def show_tasks():
        from cognitex.db.neo4j import init_neo4j, close_neo4j, get_neo4j_session
        from cognitex.db.graph_schema import get_tasks

        await init_neo4j()

        try:
            async for session in get_neo4j_session():
                task_list = await get_tasks(session, status=status, limit=limit)

                if not task_list:
                    console.print("[yellow]No tasks found.[/yellow]")
                    return

                table = Table(title=f"Tasks ({len(task_list)})")
                table.add_column("Status", style="cyan", width=10)
                table.add_column("Energy", style="yellow", width=6)
                table.add_column("Title", style="white")
                table.add_column("From", style="dim", width=25)
                table.add_column("Due", style="magenta", width=12)

                for task in task_list:
                    status_icon = {
                        "pending": "[yellow]○[/yellow]",
                        "in_progress": "[blue]◐[/blue]",
                        "done": "[green]●[/green]",
                    }.get(task.get("status", "pending"), "○")

                    energy = task.get("energy_cost", 3)
                    energy_color = "green" if energy <= 3 else "yellow" if energy <= 6 else "red"

                    due = task.get("due")
                    due_str = str(due)[:10] if due else "-"

                    table.add_row(
                        status_icon,
                        f"[{energy_color}]{energy}/10[/{energy_color}]",
                        task.get("title", "Untitled")[:50],
                        (task.get("from_email") or "-")[:25],
                        due_str,
                    )

                console.print(table)

        finally:
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


if __name__ == "__main__":
    app()
