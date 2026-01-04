"""Task, Project, and Goal service layer - business logic for task management."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog

from cognitex.db.neo4j import get_neo4j_session
from cognitex.db import graph_schema as gs

logger = structlog.get_logger()


class TaskService:
    """
    Business logic for managing tasks.

    Tasks are lightweight action items that can be standalone or linked
    to projects, goals, documents, and code.
    """

    @staticmethod
    def generate_id() -> str:
        """Generate a unique task ID."""
        return f"task_{uuid.uuid4().hex[:12]}"

    async def create(
        self,
        title: str,
        description: str | None = None,
        status: str = "pending",
        priority: str = "medium",
        due_date: str | None = None,
        effort_estimate: float | None = None,
        energy_cost: str | None = None,
        source_type: str | None = None,
        source_id: str | None = None,
        project_id: str | None = None,
        goal_id: str | None = None,
        assignee_emails: list[str] | None = None,
    ) -> dict:
        """
        Create a new task with optional relationships.

        Args:
            title: Task title
            description: Detailed description
            status: pending, in_progress, done
            priority: low, medium, high, critical
            due_date: ISO date string
            effort_estimate: Estimated hours
            energy_cost: high, medium, low
            source_type: email, event, agent, manual
            source_id: ID of source entity
            project_id: Link to project
            goal_id: Link to goal
            assignee_emails: People assigned to this task

        Returns:
            Created task dict with relationships
        """
        task_id = self.generate_id()

        async for session in get_neo4j_session():
            # Create the task node
            task = await gs.create_task(
                session,
                task_id=task_id,
                title=title,
                description=description,
                status=status,
                priority=priority,
                due_date=due_date,
                effort_estimate=effort_estimate,
                energy_cost=energy_cost,
                source_type=source_type,
                source_id=source_id,
            )

            # Link to project if specified
            if project_id:
                await gs.link_task_to_project(session, task_id, project_id)

            # Link to goal if specified
            if goal_id:
                await gs.link_task_to_goal(session, task_id, goal_id)

            # Assign people if specified
            if assignee_emails:
                for email in assignee_emails:
                    await gs.link_task_to_person(session, task_id, email, role="assignee")

            logger.info("Created task", task_id=task_id, title=title[:50])
            return task

    async def update(
        self,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        due_date: str | None = None,
        effort_estimate: float | None = None,
        energy_cost: str | None = None,
    ) -> dict | None:
        """
        Update task properties.
        Handles automatic timing recording for learning system.

        Returns:
            Updated task dict or None if not found
        """
        from cognitex.db.postgres import get_session
        from sqlalchemy import text

        # 1. Capture previous state for timing logic
        prev_task = await self.get(task_id)
        if not prev_task:
            return None

        prev_status = prev_task.get("status")

        # 2. Update Graph
        task = None
        async for session in get_neo4j_session():
            task = await gs.update_task(
                session,
                task_id=task_id,
                title=title,
                description=description,
                status=status,
                priority=priority,
                due_date=due_date,
                effort_estimate=effort_estimate,
                energy_cost=energy_cost,
            )

        if not task:
            return None

        # 3. Handle Timing Logic (PostgreSQL) for status transitions
        if status and status != prev_status:
            async for pg_session in get_session():
                # Start Timer: pending -> in_progress
                if status == "in_progress" and prev_status != "in_progress":
                    await pg_session.execute(
                        text("""
                            UPDATE tasks
                            SET started_at = NOW()
                            WHERE id = :id AND started_at IS NULL
                        """),
                        {"id": task_id},
                    )
                    await pg_session.commit()

                # Stop Timer: in_progress -> done
                elif status == "done" and prev_status != "done":
                    # Record completion time
                    await pg_session.execute(
                        text("""
                            UPDATE tasks
                            SET completed_at = NOW()
                            WHERE id = :id
                        """),
                        {"id": task_id},
                    )
                    await pg_session.commit()

                    # Calculate duration if we have a start time
                    if prev_task.get("started_at"):
                        from datetime import datetime as dt

                        start_time = prev_task["started_at"]
                        if isinstance(start_time, str):
                            start_time = dt.fromisoformat(start_time.replace("Z", "+00:00"))

                        await record_task_timing(
                            task_id=task_id,
                            started_at=start_time,
                            completed_at=dt.now(),
                            estimated_minutes=int(prev_task.get("effort_estimate") or 30),
                        )

        logger.info("Updated task", task_id=task_id, status=status)
        return task

    async def get(self, task_id: str) -> dict | None:
        """Get a task by ID with all relationships."""
        async for session in get_neo4j_session():
            return await gs.get_task(session, task_id)

    async def list(
        self,
        status: str | None = None,
        priority: str | None = None,
        project_id: str | None = None,
        goal_id: str | None = None,
        assignee_email: str | None = None,
        include_done: bool = False,
        overdue_only: bool = False,
        limit: int = 50,
    ) -> list[dict]:
        """
        List tasks with filters.

        Args:
            status: Filter by status
            priority: Filter by priority
            project_id: Only tasks in this project
            goal_id: Only tasks linked to this goal
            assignee_email: Only tasks assigned to this person
            include_done: Include completed tasks
            overdue_only: Only tasks past due date
            limit: Maximum results

        Returns:
            List of task dicts
        """
        async for session in get_neo4j_session():
            # Build query dynamically
            filters = []
            params = {"limit": limit}

            if status:
                filters.append("t.status = $status")
                params["status"] = status
            elif not include_done:
                filters.append("t.status <> 'done'")

            if priority:
                filters.append("t.priority = $priority")
                params["priority"] = priority

            if overdue_only:
                filters.append("t.due_date < datetime()")
                filters.append("t.status <> 'done'")

            # Build the base query
            match_clause = "MATCH (t:Task)"

            if project_id:
                match_clause = """
                MATCH (t:Task)-[:PART_OF|BELONGS_TO]->(p:Project {id: $project_id})
                """
                params["project_id"] = project_id

            if goal_id:
                match_clause = """
                MATCH (t:Task)-[:ACHIEVES]->(g:Goal {id: $goal_id})
                """
                params["goal_id"] = goal_id

            if assignee_email:
                match_clause = """
                MATCH (t:Task)-[:ASSIGNED_TO]->(person:Person {email: $email})
                """
                params["email"] = assignee_email

            where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

            # Use COLLECT to gather multiple projects/goals into arrays
            # This prevents duplicate rows when a task has multiple project links
            query = f"""
            {match_clause}
            {where_clause}
            OPTIONAL MATCH (t)-[pr:PART_OF|BELONGS_TO]->(proj:Project)
            OPTIONAL MATCH (t)-[:ACHIEVES]->(goal:Goal)
            OPTIONAL MATCH (t)-[:ASSIGNED_TO]->(person:Person)
            WITH t,
                 COLLECT(DISTINCT {{id: proj.id, title: proj.title, created_by: pr.created_by}}) as projects,
                 COLLECT(DISTINCT {{id: goal.id, title: goal.title}}) as goals,
                 COLLECT(DISTINCT person.email) as people
            RETURN {{
                id: t.id,
                title: t.title,
                description: t.description,
                status: t.status,
                priority: t.priority,
                due: toString(t.due_date),
                effort_estimate: t.effort_estimate,
                energy_cost: t.energy_cost,
                source_type: t.source_type,
                source_id: t.source_id,
                created_at: toString(t.created_at),
                updated_at: toString(t.updated_at),
                started_at: toString(t.started_at),
                projects: [p IN projects WHERE p.id IS NOT NULL],
                goals: [g IN goals WHERE g.id IS NOT NULL],
                people: [e IN people WHERE e IS NOT NULL],
                project: HEAD([p IN projects WHERE p.id IS NOT NULL]).title,
                project_id: HEAD([p IN projects WHERE p.id IS NOT NULL]).id,
                goal: HEAD([g IN goals WHERE g.id IS NOT NULL]).title,
                goal_id: HEAD([g IN goals WHERE g.id IS NOT NULL]).id,
                subtasks: t.subtasks
            }} as task
            ORDER BY
                CASE t.priority
                    WHEN 'critical' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                END,
                CASE WHEN t.due_date IS NULL THEN 1 ELSE 0 END,
                t.due_date ASC
            LIMIT $limit
            """

            result = await session.run(query, params)
            data = await result.data()

            # Parse subtasks JSON for each task
            import json
            tasks_list = []
            for r in data:
                task = r["task"]
                subtasks_raw = task.get("subtasks")
                if subtasks_raw:
                    try:
                        task["subtasks"] = json.loads(subtasks_raw) if isinstance(subtasks_raw, str) else subtasks_raw
                    except (json.JSONDecodeError, TypeError):
                        task["subtasks"] = []
                else:
                    task["subtasks"] = []
                tasks_list.append(task)

            return tasks_list

    async def complete(self, task_id: str) -> dict | None:
        """Mark a task as done."""
        return await self.update(task_id, status="done")

    async def link_to_project(self, task_id: str, project_id: str) -> bool:
        """Link a task to a project."""
        async for session in get_neo4j_session():
            return await gs.link_task_to_project(session, task_id, project_id)

    async def link_to_goal(self, task_id: str, goal_id: str) -> bool:
        """Link a task to a goal."""
        async for session in get_neo4j_session():
            return await gs.link_task_to_goal(session, task_id, goal_id)

    async def link_to_document(self, task_id: str, drive_id: str) -> bool:
        """Link a task to a Drive document."""
        async for session in get_neo4j_session():
            return await gs.link_task_to_document(session, task_id, drive_id)

    async def link_to_codefile(self, task_id: str, codefile_id: str) -> bool:
        """Link a task to a code file."""
        async for session in get_neo4j_session():
            return await gs.link_task_to_codefile(session, task_id, codefile_id)

    async def set_blocked_by(self, task_id: str, blocking_task_id: str) -> bool:
        """Mark a task as blocked by another task."""
        async for session in get_neo4j_session():
            return await gs.link_task_blocked_by(session, task_id, blocking_task_id)

    async def delete(self, task_id: str) -> bool:
        """Delete a task."""
        async for session in get_neo4j_session():
            return await gs.delete_task(session, task_id)


class ProjectService:
    """
    Business logic for managing projects.

    Projects are rich entities with many connections - they group tasks,
    link to goals, repositories, documents, and people.
    """

    @staticmethod
    def generate_id() -> str:
        """Generate a unique project ID."""
        return f"proj_{uuid.uuid4().hex[:12]}"

    async def create(
        self,
        title: str,
        description: str | None = None,
        status: str = "active",
        target_date: str | None = None,
        goal_id: str | None = None,
        owner_email: str | None = None,
        member_emails: list[str] | None = None,
        repository_ids: list[str] | None = None,
        local_path: str | None = None,
    ) -> dict:
        """
        Create a new project with optional relationships.

        Args:
            title: Project title
            description: Project description
            status: planning, active, paused, completed, archived
            target_date: Target completion date
            goal_id: Link to parent goal
            owner_email: Project owner's email
            member_emails: Team member emails
            repository_ids: Linked repository IDs
            local_path: Local filesystem path for auto-linking coding sessions

        Returns:
            Created project dict
        """
        project_id = self.generate_id()

        async for session in get_neo4j_session():
            project = await gs.create_project(
                session,
                project_id=project_id,
                title=title,
                description=description,
                status=status,
                target_date=target_date,
                local_path=local_path,
            )

            # Link to goal if specified
            if goal_id:
                await gs.link_project_to_goal(session, project_id, goal_id)

            # Link owner
            if owner_email:
                await gs.link_project_to_person(session, project_id, owner_email, role="owner")

            # Link members
            if member_emails:
                for email in member_emails:
                    await gs.link_project_to_person(session, project_id, email, role="member")

            # Link repositories
            if repository_ids:
                for repo_id in repository_ids:
                    await gs.link_project_to_repository(session, project_id, repo_id)

            logger.info("Created project", project_id=project_id, title=title[:50])
            return project

    async def update(
        self,
        project_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        target_date: str | None = None,
        goal_id: str | None = None,
        local_path: str | None = None,
    ) -> dict | None:
        """Update project properties and goal link."""
        async for session in get_neo4j_session():
            project = await gs.update_project(
                session,
                project_id=project_id,
                title=title,
                description=description,
                status=status,
                target_date=target_date,
                local_path=local_path,
            )

            if project:
                # Update goal link if specified (empty string = unlink, None = no change)
                if goal_id is not None:
                    # First remove any existing goal link
                    await session.run("""
                        MATCH (p:Project {id: $project_id})-[r:PART_OF]->(:Goal)
                        DELETE r
                    """, {"project_id": project_id})

                    # Then add new link if goal_id is not empty
                    if goal_id:
                        await gs.link_project_to_goal(session, project_id, goal_id)

                logger.info("Updated project", project_id=project_id)
            return project

    async def get(self, project_id: str) -> dict | None:
        """Get a project by ID with all relationships."""
        async for session in get_neo4j_session():
            return await gs.get_project(session, project_id)

    async def list(
        self,
        status: str | None = None,
        goal_id: str | None = None,
        member_email: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
    ) -> list[dict]:
        """
        List projects with filters.

        Args:
            status: Filter by status
            goal_id: Only projects linked to this goal
            member_email: Only projects this person is part of
            include_archived: Include archived projects
            limit: Maximum results

        Returns:
            List of project dicts with task counts
        """
        async for session in get_neo4j_session():
            return await gs.get_projects(
                session,
                status=status,
                include_archived=include_archived,
                limit=limit,
            )

    async def get_tasks(self, project_id: str, include_done: bool = False) -> list[dict]:
        """Get all tasks in a project."""
        task_service = TaskService()
        return await task_service.list(project_id=project_id, include_done=include_done)

    async def link_to_goal(self, project_id: str, goal_id: str) -> bool:
        """Link a project to a goal."""
        async for session in get_neo4j_session():
            return await gs.link_project_to_goal(session, project_id, goal_id)

    async def link_to_repository(self, project_id: str, repository_id: str) -> bool:
        """Link a project to a repository."""
        async for session in get_neo4j_session():
            return await gs.link_project_to_repository(session, project_id, repository_id)

    async def link_to_document(self, project_id: str, drive_id: str) -> bool:
        """Link a project to a Drive document."""
        async for session in get_neo4j_session():
            return await gs.link_project_to_document(session, project_id, drive_id)

    async def link_related(self, project_id_1: str, project_id_2: str) -> bool:
        """Link two related projects."""
        async for session in get_neo4j_session():
            return await gs.link_projects_related(session, project_id_1, project_id_2)

    async def add_member(self, project_id: str, email: str, role: str = "member") -> bool:
        """Add a person to a project."""
        async for session in get_neo4j_session():
            return await gs.link_project_to_person(session, project_id, email, role=role)

    async def archive(self, project_id: str) -> dict | None:
        """Archive a project."""
        return await self.update(project_id, status="archived")

    async def delete(self, project_id: str) -> bool:
        """Delete a project and unlink all relationships."""
        async for session in get_neo4j_session():
            return await gs.delete_project(session, project_id)


class GoalService:
    """
    Business logic for managing goals.

    Goals are high-level objectives with timeframes. They can have
    child goals and link to projects that work toward achieving them.
    """

    @staticmethod
    def generate_id() -> str:
        """Generate a unique goal ID."""
        return f"goal_{uuid.uuid4().hex[:12]}"

    async def create(
        self,
        title: str,
        description: str | None = None,
        timeframe: str | None = None,
        status: str = "active",
        parent_goal_id: str | None = None,
    ) -> dict:
        """
        Create a new goal.

        Args:
            title: Goal title
            description: Goal description
            timeframe: quarterly, yearly, multi_year
            status: active, achieved, abandoned
            parent_goal_id: ID of parent goal for hierarchy

        Returns:
            Created goal dict
        """
        goal_id = self.generate_id()

        async for session in get_neo4j_session():
            goal = await gs.create_goal(
                session,
                goal_id=goal_id,
                title=title,
                description=description,
                timeframe=timeframe,
                status=status,
            )

            # Link to parent goal if specified
            if parent_goal_id:
                await gs.link_goal_parent(session, goal_id, parent_goal_id)

            logger.info("Created goal", goal_id=goal_id, title=title[:50])
            return goal

    async def update(
        self,
        goal_id: str,
        title: str | None = None,
        description: str | None = None,
        timeframe: str | None = None,
        status: str | None = None,
    ) -> dict | None:
        """Update goal properties."""
        async for session in get_neo4j_session():
            goal = await gs.update_goal(
                session,
                goal_id=goal_id,
                title=title,
                description=description,
                timeframe=timeframe,
                status=status,
            )

            if goal:
                logger.info("Updated goal", goal_id=goal_id)
            return goal

    async def get(self, goal_id: str) -> dict | None:
        """Get a goal by ID with all relationships."""
        async for session in get_neo4j_session():
            return await gs.get_goal(session, goal_id)

    async def list(
        self,
        status: str | None = None,
        timeframe: str | None = None,
        include_achieved: bool = False,
        limit: int = 50,
    ) -> list[dict]:
        """
        List goals with filters.

        Args:
            status: Filter by status
            timeframe: Filter by timeframe
            include_achieved: Include achieved goals
            limit: Maximum results

        Returns:
            List of goal dicts
        """
        async for session in get_neo4j_session():
            return await gs.get_goals(
                session,
                status=status,
                timeframe=timeframe,
                include_achieved=include_achieved,
                limit=limit,
            )

    async def get_projects(self, goal_id: str) -> list[dict]:
        """Get all projects contributing to a goal."""
        async for session in get_neo4j_session():
            query = """
            MATCH (p:Project)-[:CONTRIBUTES_TO]->(g:Goal {id: $goal_id})
            OPTIONAL MATCH (t:Task)-[:PART_OF]->(p)
            WITH p, count(t) as task_count,
                 sum(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END) as done_count
            RETURN p {
                .*,
                target_date: toString(p.target_date),
                created_at: toString(p.created_at),
                task_count: task_count,
                done_count: done_count
            } as project
            ORDER BY p.created_at DESC
            """

            result = await session.run(query, {"goal_id": goal_id})
            data = await result.data()
            return [r["project"] for r in data]

    async def get_tasks(self, goal_id: str, include_done: bool = False) -> list[dict]:
        """Get all tasks directly linked to a goal."""
        task_service = TaskService()
        return await task_service.list(goal_id=goal_id, include_done=include_done)

    async def set_parent(self, goal_id: str, parent_goal_id: str) -> bool:
        """Set a goal's parent goal."""
        async for session in get_neo4j_session():
            return await gs.link_goal_parent(session, goal_id, parent_goal_id)

    async def achieve(self, goal_id: str) -> dict | None:
        """Mark a goal as achieved."""
        return await self.update(goal_id, status="achieved")

    async def abandon(self, goal_id: str) -> dict | None:
        """Mark a goal as abandoned."""
        return await self.update(goal_id, status="abandoned")

    async def delete(self, goal_id: str) -> bool:
        """Delete a goal and unlink all relationships."""
        async for session in get_neo4j_session():
            return await gs.delete_goal(session, goal_id)


class RepositoryService:
    """
    Business logic for managing GitHub repositories.

    Repositories link to projects and contain code files that can
    be referenced by tasks.
    """

    @staticmethod
    def generate_id() -> str:
        """Generate a unique repository ID."""
        return f"repo_{uuid.uuid4().hex[:12]}"

    async def create(
        self,
        name: str,
        full_name: str,
        url: str,
        description: str | None = None,
        primary_language: str | None = None,
        default_branch: str = "main",
    ) -> dict:
        """
        Create a new repository entry.

        Args:
            name: Repository name (e.g., "cognitex")
            full_name: Full name (e.g., "user/cognitex")
            url: GitHub URL
            description: Repository description
            primary_language: Main programming language
            default_branch: Default branch name

        Returns:
            Created repository dict
        """
        repo_id = self.generate_id()

        async for session in get_neo4j_session():
            repo = await gs.create_repository(
                session,
                repo_id=repo_id,
                name=name,
                full_name=full_name,
                url=url,
                description=description,
                primary_language=primary_language,
                default_branch=default_branch,
            )

            logger.info("Created repository", repo_id=repo_id, full_name=full_name)
            return repo

    async def get(self, repo_id: str = None, full_name: str = None) -> dict | None:
        """Get a repository by ID or full name."""
        async for session in get_neo4j_session():
            return await gs.get_repository(session, repo_id=repo_id, full_name=full_name)

    async def list(self, limit: int = 50) -> list[dict]:
        """List all repositories."""
        async for session in get_neo4j_session():
            return await gs.get_repositories(session, limit=limit)

    async def get_or_create(
        self,
        name: str,
        full_name: str,
        url: str,
        **kwargs,
    ) -> dict:
        """Get existing repository or create new one."""
        existing = await self.get(full_name=full_name)
        if existing:
            return existing
        return await self.create(name=name, full_name=full_name, url=url, **kwargs)

    async def delete(self, repo_id: str) -> bool:
        """Delete a repository."""
        async for session in get_neo4j_session():
            return await gs.delete_repository(session, repo_id)


class CodeFileService:
    """
    Business logic for managing code files within repositories.

    Code files can be linked to tasks and have import relationships
    to other files for understanding code structure.
    """

    @staticmethod
    def generate_id() -> str:
        """Generate a unique code file ID."""
        return f"code_{uuid.uuid4().hex[:12]}"

    async def create(
        self,
        path: str,
        name: str,
        repository_id: str,
        language: str | None = None,
        summary: str | None = None,
        last_modified: str | None = None,
    ) -> dict:
        """
        Create a new code file entry.

        Args:
            path: File path within repository
            name: File name
            repository_id: Parent repository ID
            language: Programming language
            summary: AI-generated summary of file purpose
            last_modified: ISO timestamp of last modification

        Returns:
            Created code file dict
        """
        codefile_id = self.generate_id()

        async for session in get_neo4j_session():
            codefile = await gs.create_codefile(
                session,
                codefile_id=codefile_id,
                path=path,
                name=name,
                repository_id=repository_id,
                language=language,
                summary=summary,
                last_modified=last_modified,
            )

            logger.info("Created code file", codefile_id=codefile_id, path=path)
            return codefile

    async def get(self, codefile_id: str) -> dict | None:
        """Get a code file by ID."""
        async for session in get_neo4j_session():
            query = """
            MATCH (cf:CodeFile {id: $id})
            OPTIONAL MATCH (cf)-[:CONTAINED_IN]->(r:Repository)
            RETURN cf {
                .*,
                last_modified: toString(cf.last_modified),
                created_at: toString(cf.created_at),
                repository: r.full_name,
                repository_id: r.id
            } as codefile
            """
            result = await session.run(query, {"id": codefile_id})
            record = await result.single()
            return record["codefile"] if record else None

    async def list_in_repository(
        self,
        repository_id: str,
        language: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """List code files in a repository."""
        async for session in get_neo4j_session():
            return await gs.get_codefiles(
                session,
                repository_id=repository_id,
                language=language,
                limit=limit,
            )

    async def link_import(self, importer_id: str, imported_id: str) -> bool:
        """Create an import relationship between files."""
        async for session in get_neo4j_session():
            return await gs.link_codefiles_import(session, importer_id, imported_id)

    async def get_importers(self, codefile_id: str) -> list[dict]:
        """Get files that import this file."""
        async for session in get_neo4j_session():
            query = """
            MATCH (importer:CodeFile)-[:IMPORTS]->(cf:CodeFile {id: $id})
            RETURN importer {
                .*,
                last_modified: toString(importer.last_modified)
            } as file
            """
            result = await session.run(query, {"id": codefile_id})
            data = await result.data()
            return [r["file"] for r in data]

    async def get_imports(self, codefile_id: str) -> list[dict]:
        """Get files that this file imports."""
        async for session in get_neo4j_session():
            query = """
            MATCH (cf:CodeFile {id: $id})-[:IMPORTS]->(imported:CodeFile)
            RETURN imported {
                .*,
                last_modified: toString(imported.last_modified)
            } as file
            """
            result = await session.run(query, {"id": codefile_id})
            data = await result.data()
            return [r["file"] for r in data]


# Singleton instances
_task_service: TaskService | None = None
_project_service: ProjectService | None = None
_goal_service: GoalService | None = None
_repository_service: RepositoryService | None = None
_codefile_service: CodeFileService | None = None


def get_task_service() -> TaskService:
    """Get the task service singleton."""
    global _task_service
    if _task_service is None:
        _task_service = TaskService()
    return _task_service


def get_project_service() -> ProjectService:
    """Get the project service singleton."""
    global _project_service
    if _project_service is None:
        _project_service = ProjectService()
    return _project_service


def get_goal_service() -> GoalService:
    """Get the goal service singleton."""
    global _goal_service
    if _goal_service is None:
        _goal_service = GoalService()
    return _goal_service


def get_repository_service() -> RepositoryService:
    """Get the repository service singleton."""
    global _repository_service
    if _repository_service is None:
        _repository_service = RepositoryService()
    return _repository_service


def get_codefile_service() -> CodeFileService:
    """Get the code file service singleton."""
    global _codefile_service
    if _codefile_service is None:
        _codefile_service = CodeFileService()
    return _codefile_service


# =============================================================================
# Phase 4: Duration Calibration (2.1)
# =============================================================================

async def record_task_timing(
    task_id: str,
    started_at: datetime,
    completed_at: datetime,
    estimated_minutes: int | None = None,
    interruption_count: int = 0,
    context: str | None = None,
) -> str:
    """
    Record actual task timing for duration calibration.

    Args:
        task_id: The task ID
        started_at: When work started
        completed_at: When work completed
        estimated_minutes: Original estimate if known
        interruption_count: Number of interruptions during the task
        context: Time context ('morning', 'afternoon', 'evening', 'fragmented')

    Returns:
        Timing record ID
    """
    from cognitex.db.postgres import get_session
    from sqlalchemy import text

    timing_id = f"timing_{uuid.uuid4().hex[:12]}"
    actual_minutes = int((completed_at - started_at).total_seconds() / 60)

    # Get project_id from task
    project_id = None
    async for neo_session in get_neo4j_session():
        result = await neo_session.run("""
            MATCH (t:Task {id: $task_id})
            OPTIONAL MATCH (t)-[:BELONGS_TO|PART_OF]->(p:Project)
            RETURN t.id as task_id, p.id as project_id
        """, {"task_id": task_id})
        data = await result.single()
        if data:
            project_id = data.get("project_id")
        break

    async for session in get_session():
        await session.execute(text("""
            INSERT INTO task_timing (
                id, task_id, estimated_minutes, actual_minutes,
                started_at, completed_at, interruption_count, context, project_id
            )
            VALUES (
                :id, :task_id, :estimated_minutes, :actual_minutes,
                :started_at, :completed_at, :interruption_count, :context, :project_id
            )
        """), {
            "id": timing_id,
            "task_id": task_id,
            "estimated_minutes": estimated_minutes,
            "actual_minutes": actual_minutes,
            "started_at": started_at,
            "completed_at": completed_at,
            "interruption_count": interruption_count,
            "context": context,
            "project_id": project_id,
        })
        await session.commit()
        break

    logger.debug(
        "Recorded task timing",
        task_id=task_id,
        actual_minutes=actual_minutes,
        estimated_minutes=estimated_minutes,
    )
    return timing_id


async def get_duration_calibration(min_samples: int = 3) -> dict:
    """
    Calculate personal pace factors by project.

    Returns:
        Dict with project_id as key and calibration data as value:
        - pace_factor: multiplier to apply (1.0 = accurate, 1.5 = 50% longer)
        - sample_size: number of data points
        - variability: standard deviation of the pace factor
    """
    from cognitex.db.postgres import get_session
    from sqlalchemy import text

    calibration = {}

    async for session in get_session():
        result = await session.execute(text("""
            SELECT
                project_id,
                AVG(actual_minutes::float / NULLIF(estimated_minutes, 0)) as pace_factor,
                COUNT(*) as sample_size,
                STDDEV(actual_minutes::float / NULLIF(estimated_minutes, 0)) as variability,
                AVG(actual_minutes) as avg_actual,
                AVG(estimated_minutes) as avg_estimated
            FROM task_timing
            WHERE estimated_minutes > 0 AND actual_minutes > 0
            GROUP BY project_id
            HAVING COUNT(*) >= :min_samples
        """), {"min_samples": min_samples})

        for row in result.fetchall():
            calibration[row.project_id or "unassigned"] = {
                "pace_factor": round(row.pace_factor, 2) if row.pace_factor else 1.0,
                "sample_size": row.sample_size,
                "variability": round(row.variability, 2) if row.variability else 0,
                "avg_actual": round(row.avg_actual, 0) if row.avg_actual else 0,
                "avg_estimated": round(row.avg_estimated, 0) if row.avg_estimated else 0,
            }
        break

    return calibration


async def calibrate_estimate(
    estimated_minutes: int,
    project_id: str | None = None,
) -> dict:
    """
    Adjust a task estimate based on personal pace.

    Args:
        estimated_minutes: Original estimate
        project_id: Optional project context

    Returns:
        Dict with 'original', 'calibrated', 'pace_factor', 'confidence'
    """
    calibration = await get_duration_calibration()

    result = {
        "original": estimated_minutes,
        "calibrated": estimated_minutes,
        "pace_factor": 1.0,
        "confidence": "low",
        "source": "default",
    }

    # Check project-specific calibration
    if project_id and project_id in calibration:
        cal = calibration[project_id]
        result["pace_factor"] = cal["pace_factor"]
        result["calibrated"] = int(estimated_minutes * cal["pace_factor"])
        result["source"] = f"project ({cal['sample_size']} samples)"

        # Confidence based on sample size and variability
        if cal["sample_size"] >= 10 and cal["variability"] < 0.3:
            result["confidence"] = "high"
        elif cal["sample_size"] >= 5:
            result["confidence"] = "medium"
        else:
            result["confidence"] = "low"

    # Fall back to overall calibration
    elif "unassigned" in calibration:
        cal = calibration["unassigned"]
        result["pace_factor"] = cal["pace_factor"]
        result["calibrated"] = int(estimated_minutes * cal["pace_factor"])
        result["source"] = f"overall ({cal['sample_size']} samples)"
        result["confidence"] = "low"

    return result


async def get_calibration_summary() -> dict:
    """
    Get a summary of duration calibration across all projects.

    Returns:
        Dict with overall stats and per-project breakdown
    """
    from cognitex.db.postgres import get_session
    from sqlalchemy import text

    summary = {
        "overall": {},
        "by_project": {},
        "insights": [],
    }

    async for session in get_session():
        # Overall stats
        result = await session.execute(text("""
            SELECT
                COUNT(*) as total_records,
                AVG(actual_minutes::float / NULLIF(estimated_minutes, 0)) as overall_pace,
                COUNT(DISTINCT project_id) as projects_tracked,
                SUM(actual_minutes) as total_minutes_tracked
            FROM task_timing
            WHERE estimated_minutes > 0 AND actual_minutes > 0
        """))
        row = result.fetchone()
        if row:
            summary["overall"] = {
                "total_records": row.total_records or 0,
                "overall_pace_factor": round(row.overall_pace, 2) if row.overall_pace else 1.0,
                "projects_tracked": row.projects_tracked or 0,
                "total_hours_tracked": round((row.total_minutes_tracked or 0) / 60, 1),
            }

        # Generate insights
        calibration = await get_duration_calibration()
        for project_id, cal in calibration.items():
            summary["by_project"][project_id] = cal

            if cal["pace_factor"] > 1.3:
                summary["insights"].append(
                    f"Tasks in '{project_id}' take {int((cal['pace_factor']-1)*100)}% longer than estimated"
                )
            elif cal["pace_factor"] < 0.8:
                summary["insights"].append(
                    f"Tasks in '{project_id}' complete {int((1-cal['pace_factor'])*100)}% faster than estimated"
                )
        break

    return summary
