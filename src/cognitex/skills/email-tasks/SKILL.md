# Email Task Extraction

## Purpose
Extract actionable tasks from emails while avoiding false positives. Focus on real work that needs doing, not informational content.

## What IS a Task
- Explicit requests: "Can you...", "Please...", "I need...", "Could you..."
- Deadlines mentioned: "By Friday", "Before the meeting", "End of day"
- Questions requiring research or work to answer (not simple questions)
- Commitments the user made: "I'll send you...", "Let me check...", "I'll follow up"
- Action items from meeting summaries
- Review requests: "Please review", "Take a look at", "Give me your thoughts"
- Approval requests: "Need your sign-off", "Can you approve"

## What is NOT a Task
- FYI/informational content with no expected action
- Social pleasantries and greetings
- Questions answerable with a quick reply (not research)
- CC'd emails where action is clearly for someone else
- Newsletters, marketing emails, automated notifications
- Receipts, confirmations, shipping updates
- Calendar invites (these become events, not tasks)
- Spam or promotional content
- Updates that are purely status reports

## Extraction Rules
1. One task per distinct action - don't combine multiple asks into one task
2. Include due date if mentioned or clearly inferrable ("tomorrow" = tomorrow's date)
3. Link to sender as REQUESTED_BY relationship
4. Set priority based on urgency signals:
   - "urgent", "ASAP", "immediately" → high
   - "when you get a chance", "no rush" → low
   - otherwise → medium
5. If unclear whether it's a task, err toward NOT extracting
6. Commitments the user made (in their sent emails) are tasks for them
7. Don't extract tasks from emails older than 30 days unless explicitly referenced
8. Extract max 3 tasks per email - if more, focus on the most important

## Examples

### Email: "Hi, can you review the proposal and send feedback by Thursday?"
Tasks:
- [ ] Review proposal (due: Thursday)
- [ ] Send feedback on proposal (due: Thursday, blocked by: review)

### Email: "FYI - the meeting has been moved to 3pm"
Tasks: None (informational only)

### Email: "Thanks for the update! Quick question - do we have budget for this?"
Tasks:
- [ ] Check and respond about budget availability

### Email: "I've attached the quarterly report. The board meeting is next Tuesday and I'll need your analysis beforehand."
Tasks:
- [ ] Analyze quarterly report (due: Monday - day before board meeting)

### Email: "Great chatting yesterday! Let's grab coffee sometime."
Tasks: None (social, no specific action)

### Email: "Action items from today's standup: Sarah to update docs, Mike to fix the bug, Chris to review PR #123"
Tasks:
- [ ] Review PR #123

(Only extract tasks assigned to the user, not others)

### Email: "Just checking in - any update on the timeline?"
Tasks:
- [ ] Respond with timeline update

### Email: Newsletter with "Top 10 productivity tips"
Tasks: None (newsletter content)

## Context Signals
- If the sender is the user's manager, weight toward extracting tasks
- If the email is a reply to user's email asking for something, likely contains deliverable
- Meeting recap emails often have clear action items - extract those
- Forwarded emails: check if the forward note has an action request
