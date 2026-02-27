---
name: memory-curation
description: Distill weekly observations into durable MEMORY.md updates for the agent's operational memory.
version: 1.0.0
metadata: { "cognitex": {} }
---

# Memory Curation

## Purpose

Distill weekly observations from daily memory logs and consolidated summaries
into durable MEMORY.md updates. The goal is to promote the most valuable
insights into the agent's long-term operational memory — the file injected
into every agent prompt — while discarding noise.

## What IS Worth Promoting

- Discovered preferences: communication style, scheduling habits, tool choices
- Recurring interaction patterns: how the user works with specific people or teams
- Key decisions with rationale: architectural choices, policy changes, strategic pivots
- Corrections: things the agent got wrong that should be remembered to avoid repeating
- Relationship dynamics: who the user collaborates with closely, tensions, dependencies
- Workflow habits: when the user is most productive, preferred task sequencing

## What is NOT Worth Promoting

- One-off trivial events: "synced 3 emails", "checked calendar"
- Routine operational noise: heartbeat logs, poll results, system checks
- Transient states: "user seemed tired today" (unless part of a recurring pattern)
- Duplicates: insights already captured in MEMORY.md
- Raw metrics: exact counts, timestamps, IDs (summarise instead)
- Events captured by other systems: tasks in the task tracker, emails in Gmail
- Clinical or health-related content: never promote personal health observations

## Distillation Rules

1. **One insight per bullet** — each proposed update should be a single, clear observation.
2. **Date-stamp entries** — prefix with `[YYYY-MM-DD]` so staleness is visible.
3. **Place in correct section** — match to User Preferences, Important Relationships, Recurring Patterns, Corrections, or Key Decisions.
4. **Merge don't duplicate** — if an existing entry covers the same ground, update it rather than adding a new one. Set `merge_with_existing` to the text of the entry to replace.
5. **Cap at 5–8 per week** — be selective. Quality over quantity.
6. **Flag confidence** — use 0.0–1.0 scale. Below 0.6 means "tentative, may need more evidence".
7. **Never promote clinical content** — any health, medical, or therapy-related observations must be excluded entirely.

## Output Schema

Return ONLY valid JSON matching this schema:

```json
{
  "proposed_updates": [
    {
      "section": "User Preferences|Important Relationships|Recurring Patterns|Corrections|Key Decisions",
      "content": "the insight",
      "confidence": 0.0-1.0,
      "source_dates": ["2026-02-20"],
      "merge_with_existing": null
    }
  ],
  "discarded_count": 42,
  "summary": "One-sentence summary of what was distilled this week"
}
```

Fields:
- `section`: One of the five canonical sections in MEMORY.md.
- `content`: The insight text. Should be concise (one sentence or short phrase).
- `confidence`: How certain this insight is. 0.9+ for clear patterns, 0.6–0.8 for emerging patterns, below 0.6 for tentative.
- `source_dates`: Which daily logs contributed to this insight.
- `merge_with_existing`: Set to the exact text of an existing MEMORY.md entry if this update should replace/merge with it. null for new entries.
- `discarded_count`: How many raw observations were reviewed but not promoted.
- `summary`: One-sentence summary of the week's distillation.

## Worked Examples

### Example 1: Rich week with multiple insights

**Weekly logs include**: User responded to 3 emails from Sarah within minutes, deferred all emails from vendor-x until after 4pm on multiple days, switched from VS Code to Cursor on Wednesday and used it for the rest of the week.

**Output**:
```json
{
  "proposed_updates": [
    {
      "section": "Important Relationships",
      "content": "Sarah is a high-priority contact — user consistently responds within minutes",
      "confidence": 0.85,
      "source_dates": ["2026-02-17", "2026-02-19", "2026-02-21"],
      "merge_with_existing": null
    },
    {
      "section": "Recurring Patterns",
      "content": "Emails from vendor-x are consistently deferred to late afternoon — treat as low-priority",
      "confidence": 0.8,
      "source_dates": ["2026-02-17", "2026-02-18", "2026-02-20"],
      "merge_with_existing": null
    },
    {
      "section": "User Preferences",
      "content": "Switched primary editor from VS Code to Cursor mid-week — monitor for permanence",
      "confidence": 0.6,
      "source_dates": ["2026-02-19"],
      "merge_with_existing": null
    }
  ],
  "discarded_count": 47,
  "summary": "Identified Sarah as high-priority contact, vendor-x deferral pattern, and possible editor switch to Cursor"
}
```

### Example 2: Quiet week with one update

**Weekly logs include**: Mostly routine sync operations, one instance where user corrected agent's meeting time zone assumption.

**Output**:
```json
{
  "proposed_updates": [
    {
      "section": "Corrections",
      "content": "User's recurring meetings with the London team are in GMT, not BST — always use Europe/London timezone",
      "confidence": 0.95,
      "source_dates": ["2026-02-18"],
      "merge_with_existing": null
    }
  ],
  "discarded_count": 23,
  "summary": "Quiet week — captured timezone correction for London team meetings"
}
```

### Example 3: Merging with an existing entry

**Current MEMORY.md** contains: `- [2026-02-10] User prefers morning deep work blocks (9-11am)`

**Weekly logs show**: User also blocked 2-3pm for deep work on three days this week.

**Output**:
```json
{
  "proposed_updates": [
    {
      "section": "Recurring Patterns",
      "content": "User prefers two deep work blocks: mornings (9-11am) and early afternoon (2-3pm)",
      "confidence": 0.85,
      "source_dates": ["2026-02-17", "2026-02-19", "2026-02-21"],
      "merge_with_existing": "User prefers morning deep work blocks (9-11am)"
    }
  ],
  "discarded_count": 31,
  "summary": "Updated deep work pattern to include afternoon block alongside existing morning block"
}
```
