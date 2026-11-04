# Auto Forward Bot: audit, research and improvement plan

Reviewed 4–5 November 2026. Scope: the Python source, SQL schema and migrations, configuration examples, runtime lifecycle, forwarding transports, authentication, scheduling, billing and referrals. No production Telegram messages were sent, subscriptions changed or live database migrations applied.

## Assessment

The bot has a useful feature set, but the original implementation had delivery and billing failure modes that contradicted the README's production-readiness claim. The biggest gains come from coordinating delivery, removing blocking I/O and making payment activation atomic. Adding more concurrent sends alone would increase duplicates and rate-limit failures.

This update implements the first reliability pass. It does **not** establish lossless delivery across crashes or prove live Telegram performance.

## Findings and changes

| Priority | Finding in the original code | Change and current status |
|---|---|---|
| Critical | The payment webhook accepted requests without a configured signature secret. | Signature checks fail closed; the payment route is disabled without the secret. |
| Critical | A webhook was marked processed before activation. A failed activation returned 500, but every later retry was skipped. | A new Postgres function commits the payment marker, subscription, user entitlement, ledger and referral credit in one transaction. Requires `migration_reliability.sql`. |
| High | The payment button trusted a paid link without checking the purchaser or plan and had no shared deduplication with the webhook. | Verify link ownership, currency, amount and captured payment with Razorpay. Both activation paths use the actual payment ID through the same database function. |
| High | Supabase and Razorpay synchronous calls ran inside async handlers. A slow command could freeze message reception for all accounts. | Database and payment network calls now run in threads; cached forwarding configuration is reused. |
| High | Listener and poller created untracked tasks per message, independently. | Bounded per-account queues with one worker each; destination fan-out is processed in batches of eight. Listener, poller and Bot API share one dispatcher and canonical message/album keys. |
| High | Posts older than 30 seconds were silently discarded; only the latest five posts were polled, newest first. | Removed the age cutoff. Recovery traverses the gap since the in-process cursor chronologically, using Telegram pagination. |
| High | The poller forwarded album items individually, while the album handler lacked duplicate suppression. | Albums share one delivery key and travel as groups. Bot API channel updates also collect album items before delivery. |
| High | Blocked text returned `None`, but the Bot API path restored the original text or copied the original media. | Shared filter semantics: `None` blocks the whole post or album; an empty replacement never restores the original. |
| High | Entity offsets used Python character counts, corrupting formatting after emoji. Hidden hyperlink destinations escaped link replacement. | Replacement offsets use UTF-16 code units; entity metadata is copied and preserved. Hidden URLs are updated too. |
| High | Filter/schedule deletion trusted arbitrary callback IDs, without owner constraints. | Role checks cover continuation steps and callbacks. Database deletion queries include the requesting admin ID. Admin actions require private chats. |
| High | Forwarding checked `/stop` but did not consistently enforce an active subscription. | Both transports and scheduled sends check entitlement. Cached permission decisions cannot outlive the stored expiry. Revocation stops the userbot. |
| High | The Bot API path treated a saved session as a running listener and could suppress its own fallback. | Routing checks the actual connected listener rather than merely finding saved credentials. |
| High | Private-channel access could fail after restart because StringSession does not persist entity access hashes. | On a source resolution miss, reload account dialogs and retry resolution. |
| Medium | A timeout could trigger another delivery path after Telegram had already accepted the first send. | Automatic retries are limited to explicit rate-limit rejection. Fallback transport is used for explicit permission/entity-resolution failures. Ambiguous errors are logged, not blindly replayed. |
| Medium | Downloads from different sends shared one directory and filenames. | Upload fallback uses a private temporary directory per operation and guaranteed cleanup. Protected content is not worked around. |
| Medium | Startup killed whatever process ID appeared in `.bot.pid`, even if the PID had been reused. | POSIX advisory locking refuses duplicate local instances without killing another process. |
| Medium | Pending Bot API updates were deliberately dropped on every start. | Polling retains pending updates. Userbot recovery remains limited to its current process lifetime. |
| Medium | Background forwarding tasks were untracked, and scheduler shutdown was missing. | Stop scheduler/producers, detach listeners, drain managed queues with a timeout and disconnect clients while the Bot API is available. |
| Medium | Local session files used default permissions, and auth disconnect was not awaited. | Session files use mode 0600 and directory mode 0700; disconnect is awaited. OTP/password messages are deleted when Telegram permits it; passwords retain intentional whitespace. |
| Medium | The supplied schema omitted session/referral fields and referral tables used by the code. Public table access was not restricted. | Reliability migration adds missing fields/tables, enables RLS and revokes anon/authenticated table access. A private service_role key is required. |
| Medium | A failed one-time scheduled send was deactivated anyway. | Keep it active after delivery failure. The existing daily-cron representation still needs the scheduling redesign below. |
| Low | Referral URLs hardcoded a particular bot username; duplicate `get_user` definitions obscured behavior. | Resolve the running bot's username; remove the duplicate definition and dead helper queries. |

## Research informing the changes

- Telegram applies per-chat and overall Bot API limits. Therefore, bounded fan-out and handling server-directed waits are more useful than maximizing simultaneous requests. Bot API limits are not a universal rate budget for Telethon user accounts. [Telegram Bots FAQ](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this).
- Telethon supports automatic reconnection, update dispatch options and FloodWait handling. Its session string carries credentials, while its in-memory entity cache is separate. Those details informed the connection checks, dialog refresh and strict session-file permissions. [TelegramClient reference](https://docs.telethon.dev/en/stable/modules/client.html), [sessions](https://docs.telethon.dev/en/stable/concepts/sessions.html), [asyncio guidance](https://docs.telethon.dev/en/stable/concepts/asyncio.html). The installed Telethon version remains 1.38.1; tests use that version.
- ConversationHandler relies on processing updates sequentially. This is why command processing stays sequential while network I/O is offloaded and forwarding uses managed workers. [python-telegram-bot ConversationHandler](https://docs.python-telegram-bot.org/en/stable/telegram.ext.conversationhandler.html).
- Telegram entity positions use UTF-16 offsets. Copying Python string indexes into those fields fails for non-BMP emoji. [Bot API MessageEntity](https://core.telegram.org/bots/api#messageentity).
- Razorpay documents signature validation and duplicate/reordered webhook deliveries. Our implementation inference is to enforce payment-ID idempotency together with entitlement changes in a database transaction. [Razorpay webhook validation](https://razorpay.com/docs/webhooks/validate-test/).
- Supabase recommends RLS and restricted grants for exposed tables. Session-bearing and billing tables now have a migration implementing those restrictions. [Supabase RLS](https://supabase.com/docs/guides/database/postgres/row-level-security).
- The product sells digital access inside Telegram. Telegram requires Stars for digital goods/services, including subscriptions. The current INR/USDT purchase flow needs a product/payment migration to Stars; this update hardens existing payment handling without inventing a new Stars price or changing the listed prices. [Telegram digital payments](https://core.telegram.org/bots/payments-stars).

## Best next improvements

| Order | Improvement | Why it matters | Acceptance check |
|---|---|---|---|
| 1 | Persistent delivery ledger and queue, keyed by account, source, message/album and target | Recover after process crashes; retry only destinations that definitely failed; expose ambiguous outcomes for review. Persist source cursors too. | Kill the worker mid-send and restart. Confirm no successful destination is replayed and unresolved outcomes remain visible. |
| 2 | Independent destination queues plus adaptive per-account/per-target pacing | A slow destination currently holds the next post for that account. Isolate it and coordinate Telegram cooldowns across its sends. | Rate-limit one target; other targets continue in order without an unbounded backlog. |
| 3 | Replace one-time cron schedules with persisted absolute timestamps and per-target delivery records | A failed one-time job currently retries at the next daily cron; partial success may repeat a successful target. Restarting cannot reconstruct a precise one-time date from a TIME column alone. | Test pause/resume, partial failure, midnight, restart and downtime at the scheduled time. |
| 4 | Telegram Stars billing, `/paysupport`, refund support and explicit subscription-renewal behavior | Align in-app digital subscriptions with Telegram's payment model and improve purchase support. | Confirm one successful Stars payment activates once, failed pre-checkout never activates, and refund handling is auditable. |
| 5 | Delivery history, reconnect alerts and a permission/setup checker | Users need to distinguish a disconnected source from a target missing posting rights. `/mystatus` now exposes basic queue/listener information; the next step is actionable per-target history. | Remove posting permission and verify the UI identifies the affected target without exposing credentials. |
| 6 | Encrypted session storage with a managed key, auth attempt expiry and an explicit unlink/revoke flow | Files and database sessions still contain usable credentials. At-rest encryption needs a real key-management/rotation plan. | Restore encrypted sessions after restart; confirm revocation removes every persisted copy and logs contain no credentials. |
| 7 | Multiple source-to-target routes, edit/delete synchronization and filter preview | These are useful product additions once delivery state exists. Avoid loops and map original IDs to destination IDs. | Preview changes without posting; test cross-route loops, edited captions and deleted source posts. |

Further cleanup: paginate all admin/report/bootstrap queries beyond Supabase's row limit, move long broadcasts out of the command update loop, escape user-supplied HTML/Markdown consistently, and replace the referral balance-adjustment shortcut with an auditable payout ledger. Cache expiry uses a short TTL; out-of-band database edits can take up to 30 seconds to be observed.

## Validation performed

- Python 3.14.7, with the project's pinned PTB 22.7, Supabase 2.30.0 and Telethon 1.38.1.
- 40 offline regression tests: ordering, queue backpressure, duplicate suppression across both transports, account isolation, album grouping and gap polling, stale messages, filters/UTF-16 formatting, hidden links, ownership, permissions, rate limits, ambiguous timeouts, auth cleanup, session permissions, payment validation/retry behavior and application construction.
- Ruff undefined-name/unused-import checks, Python compilation and `pip check`.
- PostgreSQL and PL/pgSQL syntax parsed using pglast. This is syntax validation, not execution against a database.
- Synthetic queue experiment: 10,000 submitted events representing 5,000 posts produced exactly 5,000 mock deliveries and suppressed 5,000 duplicates, with zero queued jobs/failures at completion. One local run took 0.2833 seconds. This measures queue overhead only; it does not measure Telegram throughput or real forwarding latency. Reproduce with `scripts/benchmark_delivery.py`.

## Deployment and staging checks

1. Back up the database and use an isolated staging bot/channels first. Install dependencies into a virtual environment.
2. Apply missing SQL files in README order, ending with `migration_reliability.sql`. The atomic activation function is required by both automated INR activation paths. Use a private service_role key; an anon key is deliberately denied access after this migration.
3. Before enabling billing on an existing database, reconcile legacy `processed_webhooks` rows. Earlier code could insert a marker without activating a subscription, or activate without recording the payment ID on its subscription row. The new function flags these historical inconsistencies rather than blindly charging or granting again.
4. Configure the Razorpay webhook secret if that existing integration is used. Keep current Python plan prices and SQL validation prices synchronized. Do not run multiple polling replicas or use the same user session simultaneously on different hosts.
5. In staging, verify text, photo, video, document, sticker, voice, albums and custom emoji; blocked words/APKs; caption/hidden-link replacements; public/private source reception; target permission failures; `/stop` and `/work`; expiry and manual revocation; graceful restart and a forced crash.
6. Use Razorpay test mode to verify captured payments, duplicate button/webhook events, concurrent renewals and a deliberately failed database transaction. Verify rollback and replay against actual Postgres before deploying the migration to production.
7. Measure source timestamp → per-target acknowledgement at normal and burst traffic, including p50/p95/p99 latency, queue depth and failure counts. No production latency or speedup is claimed by this audit.

Known limits: in-memory queue/dedup history/cursors, finite 10,000-job duplicate history per account, an initial userbot cursor at the latest source post, time-based album collection, and ambiguous network outcomes. Posts from before a fresh userbot startup are not replayed. The public `/health` endpoint is liveness only, not proof of end-to-end delivery. Some Bot API conversions can lose advanced entity/media features; validate your actual content types in staging.
