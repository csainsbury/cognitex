---
name: commitment-extraction
description: Extract commitments and promises from the user's sent emails.
version: 1.0.0
metadata: { "cognitex": {} }
---

# Commitment Extraction

## Purpose

Extract commitments and promises from the user's sent emails. A commitment is
anything the user has promised to do for someone else. The goal is to build a
ledger of outstanding obligations so nothing falls through the cracks.

## What IS a Commitment

- Explicit promises: "I'll send you the report", "I will review it by Friday"
- Implicit promises: "Let me check on that", "I'll get back to you"
- Deadline-bound undertakings: "I can have it to you by next week"
- Volunteered action items: "I'll take that one", "Leave it with me"
- Conditional promises: "Once I hear back from legal, I'll forward it to you"

## What is NOT a Commitment

- Social pleasantries: "Let's catch up sometime", "We should grab coffee"
- Informational statements: "The report is attached", "Here's the update"
- Commitments by others: "Sarah will send the data" (not the user's promise)
- Vague aspirations: "I'd love to help with that someday"
- Past actions: "I sent the file yesterday" (already done)
- Questions: "Should I send the report?" (not yet a promise)

## Output Schema

Return ONLY valid JSON matching this schema:

```json
{
  "commitments": [
    {
      "action": "what was promised",
      "deadline": "ISO date or null",
      "deadline_source": "explicit|inferred|none",
      "recipient": "name or email",
      "cognitive_load": "high|medium|low",
      "confidence": 0.0-1.0
    }
  ]
}
```

## Extraction Rules

1. **One commitment per distinct promise** — if the user promises three things in one email, return three entries.
2. **Include deadline if mentioned or inferable** — "by Friday" is explicit, "before the board meeting next Tuesday" is inferred. If no deadline, set to null with `deadline_source: "none"`.
3. **Cognitive load from scope** — "high" for multi-step deliverables (write a report, build a prototype), "medium" for standard tasks (review a document, send a file), "low" for quick actions (forward an email, confirm attendance).
4. **Confidence reflects certainty** — 0.9+ for explicit "I will" statements, 0.6-0.8 for implicit promises, below 0.5 for borderline cases.
5. **Recipient from context** — use the email's To field if commitment is to the direct recipient, or extract the name if promising to a third party mentioned in the email.

## Worked Examples

### Example 1: Explicit promise with deadline

**Email to**: sarah@example.com
**Subject**: Q1 Report

**Body**: "Hi Sarah, I'll send you the quarterly report by end of day Friday. Let me know if you need anything else."

**Output**:
```json
{
  "commitments": [
    {
      "action": "Send quarterly report to Sarah",
      "deadline": "2026-02-27",
      "deadline_source": "explicit",
      "recipient": "sarah@example.com",
      "cognitive_load": "medium",
      "confidence": 0.95
    }
  ]
}
```

### Example 2: Implicit promise

**Email to**: james@example.com
**Subject**: Re: Budget question

**Body**: "Good question — let me check with finance and get back to you."

**Output**:
```json
{
  "commitments": [
    {
      "action": "Check with finance about budget question and respond to James",
      "deadline": null,
      "deadline_source": "none",
      "recipient": "james@example.com",
      "cognitive_load": "medium",
      "confidence": 0.75
    }
  ]
}
```

### Example 3: Social pleasantry (NOT extracted)

**Email to**: alex@example.com
**Subject**: Re: Catch up

**Body**: "Great seeing you at the conference! We should definitely grab lunch sometime. Take care!"

**Output**:
```json
{
  "commitments": []
}
```

### Example 4: Commitment by someone else (NOT extracted)

**Email to**: team@example.com
**Subject**: Re: Project update

**Body**: "Thanks for the update. Sarah mentioned she'll have the designs ready by Wednesday. Looking forward to seeing them."

**Output**:
```json
{
  "commitments": []
}
```

### Example 5: Multiple commitments

**Email to**: manager@example.com
**Subject**: Re: Sprint planning

**Body**: "I'll take the API refactoring ticket and the database migration. I should have the API work done by Thursday, and I'll start on the migration next week. I'll also review Tom's PR before the end of today."

**Output**:
```json
{
  "commitments": [
    {
      "action": "Complete API refactoring ticket",
      "deadline": "2026-02-26",
      "deadline_source": "explicit",
      "recipient": "manager@example.com",
      "cognitive_load": "high",
      "confidence": 0.9
    },
    {
      "action": "Complete database migration",
      "deadline": null,
      "deadline_source": "inferred",
      "recipient": "manager@example.com",
      "cognitive_load": "high",
      "confidence": 0.85
    },
    {
      "action": "Review Tom's PR",
      "deadline": "2026-02-24",
      "deadline_source": "explicit",
      "recipient": "manager@example.com",
      "cognitive_load": "low",
      "confidence": 0.9
    }
  ]
}
```

### Example 6: No commitments

**Email to**: vendor@example.com
**Subject**: Re: Invoice

**Body**: "Thanks for sending the invoice. The payment was processed yesterday and should appear in your account within 2-3 business days."

**Output**:
```json
{
  "commitments": []
}
```
