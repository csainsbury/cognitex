"""Phase 4: Central Learning System Coordination.

This module provides a unified interface for all learning capabilities:
- Proposal pattern learning (1.1)
- Deadline completion analysis (1.2)
- Deferral prediction (1.3)
- Duration calibration (2.1)
- Preference rule validation (4.1)
- Feedback loop closure (4.2)

The LearningSystem class coordinates periodic updates and provides
a single entry point for learning-related queries.
"""

from datetime import datetime
from typing import Any

import structlog

from cognitex.agent.action_log import (
    get_proposal_patterns,
    get_proposal_recommendation,
    get_proposal_stats,
)
from cognitex.agent.decision_memory import get_decision_memory, init_decision_memory
from cognitex.agent.state_model import (
    DeferralRisk,
    get_high_risk_tasks,
)
from cognitex.services.tasks import (
    get_calibration_summary,
    get_duration_calibration,
)

logger = structlog.get_logger()


class LearningSystem:
    """
    Central coordination for all learning and adaptation.

    Provides:
    - Unified learning stats retrieval
    - Periodic policy updates
    - Pattern analysis across all learning domains
    - Actionable insights generation
    """

    async def get_learning_summary(self) -> dict:
        """
        Get a comprehensive summary of all learned patterns.

        Returns:
            Dict with stats from all learning domains and actionable insights
        """
        summary = {
            "timestamp": datetime.now().isoformat(),
            "proposals": {},
            "duration": {},
            "deferrals": {},
            "rules": {},
            "insights": [],
        }

        try:
            # Proposal learning
            proposal_stats = await get_proposal_stats()
            proposal_patterns = await get_proposal_patterns(min_samples=2)
            summary["proposals"] = {
                "stats": proposal_stats,
                "patterns": proposal_patterns,
            }

            # Duration calibration
            duration_summary = await get_calibration_summary()
            summary["duration"] = duration_summary

            # Deferral prediction
            high_risk = await get_high_risk_tasks(min_risk=0.5, limit=5)
            summary["deferrals"] = {
                "high_risk_count": len(high_risk),
                "high_risk_tasks": high_risk,
            }

            # Preference rules
            try:
                dm = get_decision_memory()
                rule_stats = await dm.rules.get_rule_stats()
                rules_by_lifecycle = await dm.rules.get_rules_by_lifecycle()
                summary["rules"] = {
                    "stats": rule_stats,
                    "by_lifecycle": {
                        k: len(v) for k, v in rules_by_lifecycle.items()
                    },
                }
            except RuntimeError:
                # Decision memory not initialized
                summary["rules"] = {"stats": {}, "by_lifecycle": {}}

            # Generate insights
            summary["insights"] = await self._generate_insights(summary)

        except Exception as e:
            logger.warning("Failed to get learning summary", error=str(e))

        return summary

    async def _generate_insights(self, summary: dict) -> list[str]:
        """Generate actionable insights from learning data."""
        insights = []

        # Proposal insights
        proposal_stats = summary.get("proposals", {}).get("stats", {})
        approval_rate = proposal_stats.get("approval_rate", 50)
        if approval_rate < 40 and proposal_stats.get("total", 0) >= 5:
            insights.append(
                f"Proposal approval rate is {approval_rate:.0f}%. "
                f"Consider more specific proposals or different priorities."
            )
        elif approval_rate > 80 and proposal_stats.get("total", 0) >= 5:
            insights.append(
                f"High proposal approval rate ({approval_rate:.0f}%). "
                f"Consider enabling auto-approval for well-accepted categories."
            )

        # Duration insights
        duration = summary.get("duration", {})
        overall_pace = duration.get("overall", {}).get("overall_pace_factor")
        if overall_pace and overall_pace > 1.3:
            insights.append(
                f"You typically take {int((overall_pace-1)*100)}% longer than estimated. "
                f"Consider adjusting estimates or building in more buffer."
            )
        elif overall_pace and overall_pace < 0.8:
            insights.append(
                f"You typically finish {int((1-overall_pace)*100)}% faster than estimated. "
                f"Your estimates may be conservative."
            )

        # Deferral insights
        deferrals = summary.get("deferrals", {})
        high_risk_count = deferrals.get("high_risk_count", 0)
        if high_risk_count > 0:
            insights.append(
                f"{high_risk_count} tasks have high deferral risk. "
                f"Consider breaking them down or adding MVS."
            )

        # Rule insights
        rules = summary.get("rules", {}).get("stats", {})
        validated = rules.get("validated", 0)
        deprecated = rules.get("deprecated", 0)
        if validated > 0:
            insights.append(
                f"{validated} preference rules have been validated through use."
            )
        if deprecated > 0:
            insights.append(
                f"{deprecated} rules were deprecated due to low success rate."
            )

        return insights

    async def run_policy_update(self) -> dict:
        """
        Run a full policy update cycle.

        This:
        1. Validates preference rules
        2. Extracts new rules from patterns
        3. Updates learned patterns cache
        4. Logs the update

        Returns:
            Dict with update results
        """
        from cognitex.agent.action_log import log_action

        results = {
            "timestamp": datetime.now().isoformat(),
            "rules_validated": {},
            "rules_extracted": 0,
            "feedback_rules_extracted": 0,
            "patterns_updated": {},
        }

        try:
            # 1. Validate preference rules
            dm = get_decision_memory()
            validation_results = await dm.rules.validate_rules()
            results["rules_validated"] = validation_results

            # 2. Extract new rules from patterns
            new_rule_ids = await dm.extract_rules_from_patterns(min_occurrences=3)
            results["rules_extracted"] = len(new_rule_ids)

            # 2b. Extract rules from user feedback (free-text feedback learning)
            try:
                from cognitex.agent.feedback_learning import extract_rules_from_feedback
                feedback_rule_ids = await extract_rules_from_feedback(
                    min_occurrences=3,
                    days_back=30,
                )
                results["feedback_rules_extracted"] = len(feedback_rule_ids)
            except Exception as e:
                logger.warning("Failed to extract rules from feedback", error=str(e))

            # 3. Update learned patterns cache
            await self._update_patterns_cache()
            results["patterns_updated"]["proposal"] = True
            results["patterns_updated"]["duration"] = True

            # 4. Log the update
            await log_action(
                action_type="learning_update",
                source="learning_system",
                summary=f"Policy update: {validation_results.get('validated', 0)} rules validated, "
                        f"{len(new_rule_ids)} pattern rules + {results['feedback_rules_extracted']} feedback rules extracted",
                details=results,
            )

            logger.info("Policy update complete", **results)

        except Exception as e:
            logger.error("Policy update failed", error=str(e))
            results["error"] = str(e)

        return results

    async def _update_patterns_cache(self) -> None:
        """Update the learned_patterns cache table."""
        from cognitex.db.postgres import get_session
        from sqlalchemy import text
        import json

        async for session in get_session():
            # Cache proposal patterns
            proposal_patterns = await get_proposal_patterns()
            await session.execute(text("""
                INSERT INTO learned_patterns (id, pattern_type, pattern_key, pattern_data, sample_size, last_updated)
                VALUES ('prop_overall', 'proposal_acceptance', 'overall', :data, :samples, NOW())
                ON CONFLICT (pattern_type, pattern_key)
                DO UPDATE SET pattern_data = :data, sample_size = :samples, last_updated = NOW()
            """), {
                "data": json.dumps(proposal_patterns.get("overall", {})),
                "samples": proposal_patterns.get("overall", {}).get("decided", 0),
            })

            # Cache duration calibration
            duration_cal = await get_duration_calibration()
            for project_id, cal in duration_cal.items():
                await session.execute(text("""
                    INSERT INTO learned_patterns (id, pattern_type, pattern_key, pattern_data, sample_size, confidence, last_updated)
                    VALUES (:id, 'duration', :key, :data, :samples, :confidence, NOW())
                    ON CONFLICT (pattern_type, pattern_key)
                    DO UPDATE SET pattern_data = :data, sample_size = :samples, confidence = :confidence, last_updated = NOW()
                """), {
                    "id": f"dur_{project_id[:20]}",
                    "key": project_id,
                    "data": json.dumps(cal),
                    "samples": cal.get("sample_size", 0),
                    "confidence": 1.0 / cal.get("variability", 1.0) if cal.get("variability") else 0.5,
                })

            await session.commit()
            break

    async def get_recommendation_for_task_creation(
        self,
        project_id: str | None = None,
        priority: str = "medium",
        estimated_minutes: int | None = None,
    ) -> dict:
        """
        Get recommendation for creating/proposing a task.

        Combines insights from:
        - Proposal acceptance patterns
        - Duration calibration
        - Current context

        Returns:
            Dict with recommendations for proposal strategy
        """
        recommendation = {
            "should_propose": True,
            "auto_approve": False,
            "calibrated_estimate": estimated_minutes,
            "insights": [],
        }

        # Check proposal patterns
        proposal_rec = await get_proposal_recommendation(project_id, priority)
        recommendation["should_propose"] = proposal_rec["should_propose"]
        recommendation["auto_approve"] = proposal_rec.get("auto_approve", False)
        if proposal_rec.get("reason"):
            recommendation["insights"].append(proposal_rec["reason"])

        # Calibrate estimate if provided
        if estimated_minutes and project_id:
            from cognitex.services.tasks import calibrate_estimate
            calibration = await calibrate_estimate(estimated_minutes, project_id)
            if calibration["calibrated"] != estimated_minutes:
                recommendation["calibrated_estimate"] = calibration["calibrated"]
                recommendation["insights"].append(
                    f"Adjusted estimate from {estimated_minutes}m to {calibration['calibrated']}m "
                    f"based on historical pace ({calibration['source']})"
                )

        return recommendation

    async def assess_task_risk(self, task: dict) -> dict:
        """
        Assess the risk profile for a task.

        Returns:
            Dict with deferral risk, duration risk, and recommendations
        """
        assessment = {
            "deferral_risk": None,
            "duration_adjustment": None,
            "recommendations": [],
        }

        # Deferral risk
        deferral_risk = await DeferralRisk.calculate(task)
        assessment["deferral_risk"] = {
            "score": round(deferral_risk.score, 2),
            "factors": deferral_risk.factors,
            "intervention": deferral_risk.recommended_intervention,
        }

        if deferral_risk.score >= 0.7:
            assessment["recommendations"].append(
                f"High deferral risk ({deferral_risk.score:.0%}). "
                f"Recommended: {deferral_risk.recommended_intervention or 'add MVS'}"
            )
        elif deferral_risk.score >= 0.5:
            assessment["recommendations"].append(
                f"Moderate deferral risk ({deferral_risk.score:.0%}). "
                f"Consider: {deferral_risk.recommended_intervention or 'setting a deadline'}"
            )

        # Duration adjustment
        estimated = task.get("estimated_minutes")
        project_id = task.get("project_id")
        if estimated and project_id:
            from cognitex.services.tasks import calibrate_estimate
            calibration = await calibrate_estimate(estimated, project_id)
            if calibration["pace_factor"] != 1.0:
                assessment["duration_adjustment"] = calibration
                if calibration["pace_factor"] > 1.2:
                    assessment["recommendations"].append(
                        f"Tasks in this project typically take {int((calibration['pace_factor']-1)*100)}% longer. "
                        f"Consider {calibration['calibrated']}m instead of {estimated}m."
                    )

        return assessment


# Singleton
_learning_system: LearningSystem | None = None


def get_learning_system() -> LearningSystem:
    """Get the learning system singleton."""
    global _learning_system
    if _learning_system is None:
        _learning_system = LearningSystem()
    return _learning_system


async def init_learning_system() -> LearningSystem:
    """Initialize the learning system and its dependencies."""
    # Ensure Phase 4 schema exists
    from cognitex.db.phase4_schema import init_phase4_schema
    await init_phase4_schema()

    # Initialize decision memory if not already done
    try:
        get_decision_memory()
    except RuntimeError:
        await init_decision_memory()

    logger.info("Learning system initialized")
    return get_learning_system()


# =============================================================================
# State Observation Recording (Phase 5)
# =============================================================================

async def record_task_outcome(
    task_id: str,
    task_title: str,
    completed: bool,
    mode: str | None = None,
    fatigue_level: float | None = None,
    focus_score: float | None = None,
    energy_cost: str | None = None,
    task_friction: int | None = None,
) -> None:
    """Record a task outcome with state context for learning.

    This feeds data into the temporal energy model and state-aware
    recommendation system.

    Args:
        task_id: The task ID
        task_title: Task title for reference
        completed: True if task was completed, False if deferred/abandoned
        mode: Current operating mode when task was attempted
        fatigue_level: Current fatigue level (0-1)
        focus_score: Current focus score (0-1)
        energy_cost: Task energy cost ('high', 'medium', 'low')
        task_friction: Task friction level (0-5)
    """
    from cognitex.db.postgres import get_session
    from cognitex.db.redis import get_redis
    from cognitex.agent.state_model import get_temporal_model
    from sqlalchemy import text

    now = datetime.now()
    hour = now.hour
    day_of_week = now.weekday()  # 0=Monday, 6=Sunday

    # Check if we're in post-clinical recovery
    redis = get_redis()
    clinical_recovery = await redis.get("cognitex:clinical_recovery_until")
    post_clinical = clinical_recovery is not None
    minutes_since_clinical = None

    if post_clinical and clinical_recovery:
        try:
            recovery_until = datetime.fromisoformat(clinical_recovery)
            # We're IN recovery, so clinical session was recently
            # Estimate: assume clinical ended at start of day minus hours passed
            minutes_since_clinical = hour * 60  # Rough estimate
        except (ValueError, TypeError):
            pass

    # Determine outcome string
    outcome = "completed" if completed else "deferred"

    try:
        async for session in get_session():
            await session.execute(text("""
                INSERT INTO state_observations (
                    task_id, task_title, outcome, mode, fatigue_level,
                    focus_score, hour_of_day, day_of_week, post_clinical,
                    minutes_since_clinical, energy_cost, task_friction, observed_at
                ) VALUES (
                    :task_id, :task_title, :outcome, :mode, :fatigue_level,
                    :focus_score, :hour, :day_of_week, :post_clinical,
                    :minutes_since_clinical, :energy_cost, :task_friction, :observed_at
                )
            """), {
                "task_id": task_id,
                "task_title": task_title[:200] if task_title else None,
                "outcome": outcome,
                "mode": mode,
                "fatigue_level": fatigue_level,
                "focus_score": focus_score,
                "hour": hour,
                "day_of_week": day_of_week,
                "post_clinical": post_clinical,
                "minutes_since_clinical": minutes_since_clinical,
                "energy_cost": energy_cost,
                "task_friction": task_friction,
                "observed_at": now,
            })
            await session.commit()
            break

        # Update temporal energy model based on observation
        temporal = get_temporal_model()
        difficulty = energy_cost or "medium"
        await temporal.update_from_observation(
            hour=hour,
            task_completed=completed,
            task_difficulty=difficulty,
            post_clinical=post_clinical,
        )

        logger.debug(
            "Recorded task outcome observation",
            task_id=task_id,
            outcome=outcome,
            hour=hour,
            mode=mode,
            post_clinical=post_clinical,
        )
    except Exception as e:
        logger.warning("Failed to record task outcome", error=str(e))


async def get_state_observations_summary(days: int = 7) -> dict:
    """Get summary statistics from state observations.

    Args:
        days: Number of days to analyze

    Returns:
        Dict with completion rates by hour, mode, and energy level
    """
    from cognitex.db.postgres import get_session
    from sqlalchemy import text

    summary = {
        "by_hour": {},
        "by_mode": {},
        "by_energy_cost": {},
        "post_clinical_impact": {},
        "total_observations": 0,
    }

    try:
        async for session in get_session():
            # Completion rate by hour
            result = await session.execute(text("""
                SELECT
                    hour_of_day,
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE outcome = 'completed') as completed
                FROM state_observations
                WHERE observed_at > NOW() - INTERVAL ':days days'
                GROUP BY hour_of_day
                ORDER BY hour_of_day
            """).bindparams(days=days))

            for row in result.fetchall():
                rate = row.completed / row.total if row.total > 0 else 0
                summary["by_hour"][row.hour_of_day] = {
                    "total": row.total,
                    "completed": row.completed,
                    "rate": round(rate, 2),
                }

            # Completion rate by mode
            result = await session.execute(text("""
                SELECT
                    mode,
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE outcome = 'completed') as completed
                FROM state_observations
                WHERE observed_at > NOW() - INTERVAL ':days days'
                  AND mode IS NOT NULL
                GROUP BY mode
            """).bindparams(days=days))

            for row in result.fetchall():
                rate = row.completed / row.total if row.total > 0 else 0
                summary["by_mode"][row.mode] = {
                    "total": row.total,
                    "completed": row.completed,
                    "rate": round(rate, 2),
                }

            # Post-clinical impact
            result = await session.execute(text("""
                SELECT
                    post_clinical,
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE outcome = 'completed') as completed
                FROM state_observations
                WHERE observed_at > NOW() - INTERVAL ':days days'
                GROUP BY post_clinical
            """).bindparams(days=days))

            for row in result.fetchall():
                rate = row.completed / row.total if row.total > 0 else 0
                key = "post_clinical" if row.post_clinical else "normal"
                summary["post_clinical_impact"][key] = {
                    "total": row.total,
                    "completed": row.completed,
                    "rate": round(rate, 2),
                }

            # Total observations
            result = await session.execute(text("""
                SELECT COUNT(*) as count
                FROM state_observations
                WHERE observed_at > NOW() - INTERVAL ':days days'
            """).bindparams(days=days))
            row = result.fetchone()
            summary["total_observations"] = row.count if row else 0

            break

    except Exception as e:
        logger.warning("Failed to get state observations summary", error=str(e))

    return summary
