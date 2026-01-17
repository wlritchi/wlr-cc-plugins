# A2A Message Examples

Reference examples for common message patterns. See SKILL.md for message format specification.

## Request with Expected Reply

```markdown
---
from: backend-api
to: devspace-manager
timestamp: 2026-01-16T11:00:00Z
subject: Need Redis instance
expects-reply: true
---

I'm implementing the caching layer and need a Redis instance in the devspace.

Requirements:
- Redis 7.x
- Accessible at a predictable hostname
- Persistence enabled

Let me know when it's ready or if there are any issues.
```

## Notification (No Reply Needed)

```markdown
---
from: backend-api
to: frontend-app
timestamp: 2026-01-16T14:30:00Z
subject: API schema change
expects-reply: false
---

Heads up: I've updated the `/api/users` endpoint response format.

Changes:
- `created_at` is now ISO 8601 (was Unix timestamp)
- Added `updated_at` field
- `email` is now optional (can be null)

The OpenAPI spec in `api-service/openapi.yaml` is updated. Let me know if you have questions.
```

## Acknowledgment

```markdown
---
from: devspace-manager
to: backend-api
timestamp: 2026-01-16T11:15:00Z
subject: Re: Need Redis instance
expects-reply: false
---

Got it. Working on spinning up Redis now. Should be ready in ~10 minutes.

I'll send another message when it's available with connection details.
```

## Follow-up

```markdown
---
from: backend-api
to: devspace-manager
timestamp: 2026-01-16T12:00:00Z
subject: Following up on Redis request
expects-reply: true
---

Following up on my earlier request for a Redis instance. Haven't heard back in about an hour.

Are you still working on it, or did something come up? Let me know if you need anything from me.
```
