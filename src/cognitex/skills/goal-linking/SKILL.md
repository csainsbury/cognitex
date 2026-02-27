---
name: goal-linking
description: Connect tasks to projects and goals for context and prioritization.
version: 1.0.0
metadata: { "cognitex": {} }
---

# Goal Linking

## Purpose
Ensure tasks are connected to projects and goals for context. Helps with prioritization and tracking progress toward objectives.

## Linking Rules
1. Every task should have a project (create "Misc" if truly orphaned)
2. Projects should connect to goals when relationship is clear
3. Don't force connections - orphaned is better than wrong link
4. Prefer explicit links over inferred ones
5. One task can support multiple goals (many-to-many)

## How to Infer Task → Project Links
- **Sender's organization** → Company/client project if exists
- **Keywords in subject** → Topic matching to project names
- **Mentioned project names** → Direct link
- **Email thread** → Same project as earlier tasks in thread
- **Shared attendees** → Project those people work on

## How to Infer Project → Goal Links
- **Time context** (Q1, H2, "this year") → Goal timeframe matching
- **Keywords overlap** → Goal and project share key terms
- **People involved** → Goal owners work on project
- **Explicit statements** → "This supports our goal to..."

## What NOT to Link
- Tasks clearly unrelated to any active goal
- Projects that are maintenance/operational (no goal)
- Speculative connections with < 50% confidence
- Goals from previous periods (archived)

## Confidence Levels
- **High (auto-link)**: Explicit mention, direct request from goal owner
- **Medium (suggest)**: Keyword match, same people, similar timeframe
- **Low (skip)**: Only tangential connection, different contexts

## Examples

### Task: "Review Q1 marketing budget"
- Project: Marketing (keyword match)
- Goal: "Grow revenue 20%" (marketing supports this)
- Confidence: High (explicit Q1 reference, budget = planning)

### Task: "Fix login bug"
- Project: Product/Engineering
- Goal: None (bug fix is operational, not goal-driving)
- Confidence: N/A - don't force goal link

### Task: "Prepare investor deck"
- Project: Fundraising
- Goal: "Close Series A" (direct support)
- Confidence: High (explicit fundraising task)

### Task: "Update team wiki"
- Project: Internal Ops
- Goal: None or "Improve team efficiency" if exists
- Confidence: Low - only link if goal explicitly exists

## Hierarchy
```
Goal (quarterly/yearly objective)
  └── Project (ongoing initiative)
        └── Task (specific work item)
```

## Special Cases
- **Cross-project tasks**: Link to primary project, note others in description
- **Recurring tasks**: Link to project, not goal (goals are time-bound)
- **Learning/development tasks**: Link to career growth goal if exists
- **Admin tasks**: Usually no goal link needed
