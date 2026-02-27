---
name: meeting-prep
description: Prepare comprehensive context packs for upcoming meetings.
version: 1.0.0
metadata: { "cognitex": {} }
---

# Meeting Preparation

## Purpose
Prepare comprehensive context packs for upcoming meetings. Gather relevant background so the user walks in prepared.

## What to Include
- Recent email threads with attendees (last 14 days)
- Open tasks involving any attendees
- Shared documents modified recently (last 30 days)
- Previous meeting notes if available
- Key topics likely to be discussed (inferred from recent interactions)
- Unresolved questions or pending items from last interaction

## What to Prioritize
1. Unresolved action items from last interaction with attendees
2. Deadlines approaching that involve attendees
3. Recent changes to shared projects
4. Questions the user needs to ask (from their notes/tasks)
5. Commitments the user made to attendees

## What to Exclude
- Routine/automated emails (newsletters, notifications)
- Social/personal content unrelated to work
- Documents not relevant to likely meeting topics
- Very old interactions (> 60 days unless explicitly relevant)
- Tasks already completed

## Preparation Rules
1. Start with the meeting title/description to understand context
2. For each attendee, find their most recent meaningful interaction
3. Note any outstanding commitments in either direction
4. Surface documents that match meeting topic keywords
5. Include a "Questions to Ask" section if there are open items
6. Keep context pack under 500 words - be concise
7. Format with clear sections, not paragraphs
8. Lead with most important/urgent items

## Format Template
```
## Meeting: [Title]
**With:** [Attendee names]
**Time:** [When]

### Key Context
- [Most important thing to know]
- [Second most important]

### Open Items
- [ ] [Outstanding task/commitment]
- [ ] [Question to ask]

### Recent Interactions
- [Date]: [Brief summary of last touchpoint with key attendees]

### Relevant Documents
- [Doc name] - [why relevant]
```

## Examples

### 1:1 with Manager
Focus on:
- Tasks assigned by them and their status
- Questions/blockers to raise
- Updates on projects they care about
- Career/development topics if scheduled

### Client Meeting
Focus on:
- Project status and deliverables
- Outstanding requests from client
- Issues/risks to discuss
- Recent communications

### Team Standup
Focus on:
- What you did since last standup
- What you're working on today
- Blockers to raise

### Job Interview
Focus on:
- Company/role research
- Questions prepared
- Key talking points

## Timing
- T-24h: Build initial pack, identify missing info
- T-2h: Refresh with any new emails/updates
- T-15m: Quick reminder of top 3 points
- T-5m: "Whisper" mode - just the essentials
