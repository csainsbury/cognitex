---
name: email-triage
description: Structured triage extraction with tone neutralisation for incoming emails.
version: 1.0.0
metadata: { "cognitex": {} }
---

# Email Triage Extraction

## Purpose

Extract a structured triage decision from an email. The goal is to determine the correct action (act, delegate, track, archive) using **factual content only** — emotional language is noted but never drives urgency scoring.

## Output Schema

Return ONLY valid JSON matching this schema:

```json
{
  "triage_decision": "action|delegate|track|archive",
  "action_verb": "review|respond|schedule|approve|create|forward|research|discuss",
  "deadline": "ISO datetime or null",
  "deadline_source": "explicit|inferred|none",
  "delegation_candidate": "name or email, or null",
  "delegation_reason": "why this person should handle it, or null",
  "confidence": 0.0-1.0,
  "project_context": "related project or workstream, or null",
  "factual_summary": "tone-neutral restatement of the request",
  "emotional_markers": ["list of emotional language detected"],
  "factual_urgency": 1-5,
  "clinical_flag": false
}
```

## Tone Neutralisation Rules

Follow these rules strictly to separate emotional tone from factual urgency:

1. **Rewrite first**: Produce `factual_summary` by restating the request stripped of emotional language, hedging, and social pleasantries. Keep only: who needs what, by when, why it matters, who is affected.
2. **Score from facts only**: Determine `factual_urgency` (1-5) using ONLY the `factual_summary`. Score based on:
   - Deadline proximity (days away)
   - Business impact (revenue, compliance, legal)
   - Number of people blocked
   - Regulatory or contractual obligation
3. **Record emotions separately**: List emotional markers in `emotional_markers` (e.g., "urgent-sounding", "apologetic", "frustrated", "anxious", "enthusiastic"). These are noted for context but NEVER used in urgency scoring.
4. **Cap rule**: If the only reason for high urgency is emotional language (no factual deadline, no blocked people, no business impact), cap `factual_urgency` at 3.

### Urgency scale

- **1** — No time pressure, purely informational
- **2** — Low priority, can wait days/weeks
- **3** — Normal priority, should address within a few days
- **4** — Important, deadline within 48 hours or people blocked
- **5** — Critical, deadline today/overdue, regulatory, or multiple people blocked

## Clinical Bypass

If the email contains clinical, medical, or NHS-specific terminology (patient data, diagnoses, treatment plans, NHS numbers, clinical trial references), set:
- `clinical_flag: true`
- `triage_decision: "track"`
- `factual_summary: "Clinical content detected — deferred to clinical firewall"`
- All other fields to safe defaults

Do NOT attempt to summarise or extract detail from clinical content.

## Triage Decision Rules

- **action** — The user personally needs to do something (respond, review, approve, create). Use when the email contains a direct request aimed at the user.
- **delegate** — Someone else should handle this. Populate `delegation_candidate` with the best person and `delegation_reason` with why. Use when the email is misdirected, or someone on the team is better suited.
- **track** — Monitor but no immediate action required. Use for: FYI emails with importance, clinical content, items pending external response, status updates that need watching.
- **archive** — No action needed. Use for: newsletters, receipts, automated notifications, marketing, shipping updates, social media digests.

## Action Verbs

Choose the most specific verb:
- **review** — examine a document, PR, or proposal
- **respond** — reply to a question or request
- **schedule** — arrange a meeting or call
- **approve** — sign off on something
- **create** — produce a document, task, or deliverable
- **forward** — route to someone else (often paired with delegate)
- **research** — investigate and report back
- **discuss** — conversation needed, no single answer

## Worked Examples

### Example 1: Emotional urgency vs factual urgency

**Email**: "URGENT!!! I really need you to look at this ASAP!!! The client is going to be SO upset if we don't fix the typo on page 3 of the brochure!"

**Triage**:
```json
{
  "triage_decision": "action",
  "action_verb": "review",
  "deadline": null,
  "deadline_source": "none",
  "delegation_candidate": null,
  "delegation_reason": null,
  "confidence": 0.85,
  "project_context": "brochure",
  "factual_summary": "Fix typo on page 3 of the brochure before client sees it.",
  "emotional_markers": ["urgent-sounding", "anxious", "exclamation-heavy"],
  "factual_urgency": 3,
  "clinical_flag": false
}
```
Note: No concrete deadline, no people blocked, no regulatory issue — urgency capped at 3 despite emotional language.

### Example 2: Genuine high urgency

**Email**: "Hi, the board presentation is tomorrow at 9am and the financial projections on slide 12 are using last quarter's numbers. Can you update them before end of day?"

**Triage**:
```json
{
  "triage_decision": "action",
  "action_verb": "review",
  "deadline": "today end of day",
  "deadline_source": "explicit",
  "delegation_candidate": null,
  "delegation_reason": null,
  "confidence": 0.95,
  "project_context": "board presentation",
  "factual_summary": "Update financial projections on slide 12 with current quarter numbers before board presentation tomorrow 9am.",
  "emotional_markers": [],
  "factual_urgency": 5,
  "clinical_flag": false
}
```

### Example 3: Delegation

**Email**: "Hey, can your team update the API docs? The endpoints changed in last week's release and customers are hitting errors."

**Triage**:
```json
{
  "triage_decision": "delegate",
  "action_verb": "create",
  "deadline": null,
  "deadline_source": "none",
  "delegation_candidate": null,
  "delegation_reason": "Request is for the team, not the user specifically. Best delegated to whoever owns API documentation.",
  "confidence": 0.8,
  "project_context": "API documentation",
  "factual_summary": "API docs need updating to reflect endpoint changes from last week's release. Customers hitting errors.",
  "emotional_markers": [],
  "factual_urgency": 4,
  "clinical_flag": false
}
```

### Example 4: Clinical bypass

**Email**: "Patient NHS number 123-456-7890 has been referred for the MRI scan. Please review the clinical notes attached."

**Triage**:
```json
{
  "triage_decision": "track",
  "action_verb": "review",
  "deadline": null,
  "deadline_source": "none",
  "delegation_candidate": null,
  "delegation_reason": null,
  "confidence": 1.0,
  "project_context": null,
  "factual_summary": "Clinical content detected — deferred to clinical firewall",
  "emotional_markers": [],
  "factual_urgency": 1,
  "clinical_flag": true
}
```

### Example 5: Newsletter / archive

**Email**: "Your weekly digest from TechCrunch — Top stories: AI funding hits record high, New startup launches..."

**Triage**:
```json
{
  "triage_decision": "archive",
  "action_verb": "review",
  "deadline": null,
  "deadline_source": "none",
  "delegation_candidate": null,
  "delegation_reason": null,
  "confidence": 0.95,
  "project_context": null,
  "factual_summary": "Weekly TechCrunch newsletter digest.",
  "emotional_markers": [],
  "factual_urgency": 1,
  "clinical_flag": false
}
```

### Example 6: Track (FYI with importance)

**Email**: "FYI — the infrastructure team is migrating the production database this weekend. There may be brief downtime Saturday 2am-4am. No action needed from your side."

**Triage**:
```json
{
  "triage_decision": "track",
  "action_verb": "review",
  "deadline": null,
  "deadline_source": "none",
  "delegation_candidate": null,
  "delegation_reason": null,
  "confidence": 0.9,
  "project_context": "infrastructure migration",
  "factual_summary": "Production database migration this weekend with potential downtime Saturday 2am-4am. No action required.",
  "emotional_markers": [],
  "factual_urgency": 2,
  "clinical_flag": false
}
```

### Example 7: Apologetic but genuine request

**Email**: "So sorry to bother you, I know you're super busy, but would it be possible at all to maybe get your thoughts on the Q3 hiring plan? No rush at all, whenever you get a chance! Really sorry again!"

**Triage**:
```json
{
  "triage_decision": "action",
  "action_verb": "review",
  "deadline": null,
  "deadline_source": "none",
  "delegation_candidate": null,
  "delegation_reason": null,
  "confidence": 0.85,
  "project_context": "Q3 hiring plan",
  "factual_summary": "Review and provide feedback on Q3 hiring plan.",
  "emotional_markers": ["apologetic", "hedging", "self-deprecating"],
  "factual_urgency": 2,
  "clinical_flag": false
}
```

### Example 8: Inferred deadline

**Email**: "The vendor contract expires on March 15th. We need to decide whether to renew or switch providers."

**Triage**:
```json
{
  "triage_decision": "action",
  "action_verb": "approve",
  "deadline": "2026-03-15",
  "deadline_source": "inferred",
  "delegation_candidate": null,
  "delegation_reason": null,
  "confidence": 0.85,
  "project_context": "vendor contract renewal",
  "factual_summary": "Decision needed on vendor contract renewal vs switching providers before March 15th expiry.",
  "emotional_markers": [],
  "factual_urgency": 4,
  "clinical_flag": false
}
```
