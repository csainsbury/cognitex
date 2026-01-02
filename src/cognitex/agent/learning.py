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

            # 3. Update learned patterns cache
            await self._update_patterns_cache()
            results["patterns_updated"]["proposal"] = True
            results["patterns_updated"]["duration"] = True

            # 4. Log the update
            await log_action(
                action_type="learning_update",
                source="learning_system",
                summary=f"Policy update: {validation_results.get('validated', 0)} rules validated, "
                        f"{len(new_rule_ids)} rules extracted",
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
