# Prospect outreach plan — 11 November 2026

## Funnel and operating targets

Prospects → contacted → replied → meeting/visit → drawings received → quoted → won/lost.
Count distinct decision-makers contacted, not follow-up messages. Target set by Sai
on 11 Nov 2026: contact 300-500 named decision-makers (plan on ~400) to land about two
orders at his indicative GBP 25,000/order: GBP 50,000 revenue and approximately GBP
10,000 margin. Published "2-5%" cold-outreach figures are mostly reply rates, not
order rates, so 100 contacts is not expected to be enough. These are planning
assumptions, not promises; measure each stage rate before scaling.

Pace is set by mailbox capacity: at most 30 sends/day per mailbox including follow-ups,
and about three emails per contact. sai@ikeng.online alone supports ~10 new contacts
per working day (~8 weeks to 400); once sj@ikeng.online is warmed up (5, 10, 20, 30/day
over four weeks) the two together support ~20/day (~5-6 weeks). LinkedIn adds two
manual tasks per contact (~20 people/day, ~40 minutes for Sai), within Sales Navigator's
weekly invitation limits. Start with a 25-contact pilot, check bounces and replies,
then scale.

Measure delivered contacts, replies, bounces, objections, meetings, drawings,
quotes, wins/loss reasons, GBP won and actual project margin. Review cohorts and
segments weekly, along with overdue tasks and stalled projects. Account stage
counts and contact conversion rates use separate denominators.

## Segments and grounded messages

Architects first: residential, hospitality, interiors and FF&E leads. Offer to
price one item from a live drawing: joinery, kitchens, wardrobes, doors/windows
and loose furniture through partner factories in China for UK projects. Discuss
samples, factory checks and applicable test documentation; never promise blanket
certification. Ask larger practices for their interiors/FF&E lead.

Showrooms/dealers: bespoke items or a brief stocked brands cannot meet. UK brands:
second-source components or a line with samples and factory checks. Distributors:
own-label runs with quantities and applicable reports agreed for the product.
Manufacturer-owned showrooms stay low priority unless sourced evidence identifies
a reseller/dealer, UK brand or distributor. Do not infer nationality from a domain.
Pitch source: `data/prospects/clerkenwell-walk-list-curated.html`.

Introducer fees are agreed in writing and always disclosed to the end client.
Templates never expose supplier identities, supplier costs or margins, and make
no invented project, quality or certification claims.

## Sequence

Architects/interior designers: day 0 email plus manual LinkedIn view/connect;
day 3 manual LinkedIn message; day 5 brief email asking for one drawing;
day 10 visit/call; day 14 short final email. Dealers use the same offsets with
bespoke/resale wording. UK brands/distributors use parts/sourcing wording and
calls where a visit is impractical. Connection notes are under 300 characters.
Group visits by street/area. Record visit outcome and sourced business-card
details, then offer a separately approved same-day great-to-meet-you sequence.

Stop every outstanding step and task on any reply, bounce or opt-out, including
out-of-office. Not-now and ambiguous replies need Sai. Never automatically resume
or chase indefinitely. Interested project enquiries become existing customer/
enquiry records and link back to the account. Sai decides won/lost using existing
enquiry closure requirements; accepted quotes alone do not prove a win.

## Compliance

Cold email only to verified corporate subscribers. Flag and skip sole traders,
partnerships and unknown types. Review that the particular address is a corporate
subscription too: company registration alone does not prove this for personal
addresses. No purchased lists. Every import records provenance. CSV files are
manual inputs; the system never extracts contacts from Sales Navigator.

Sai records a legitimate-interest assessment before approval: relevant UK sourcing
purpose, necessity of limited contact and balance against professional contacts'
rights/expectations. Every email identifies IK Eng Ltd, its address, reason for
contact, source, privacy contact and reply-to-opt-out instruction. Honour objections
immediately. Review stale personal records periodically, retaining minimal email/
domain suppression identifiers permanently to prevent reimport restarting contact.
No endpoint empties the suppression list. Disclose introducer fees to end clients.

ICO sources checked 11 November 2026:
https://ico.org.uk/for-organisations/direct-marketing-and-privacy-and-electronic-communications/business-to-business-marketing/
and https://ico.org.uk/for-organisations/direct-marketing-and-privacy-and-electronic-communications/sending-direct-marketing-choosing-your-lawful-basis/ .
The implementation conservatively skips all partnerships and unknown subscribers.
Manual callers must check TPS/CTPS and objections before dialling.

## Deliverability and safety

30 sends/mailbox/UK calendar day across all sends visible in Zoho Sent. Warm sj
at five/day in week one, ten in week two, twenty in week three, thirty thereafter;
configure an explicit start date. Leave at least 90 seconds after any mailbox send.
Plain text, no pixels or shorteners. sai uses RFQ_ZOHO_*; sj uses OUTREACH_ZOHO_*.
Sending and scheduling default off. Approval covers templates and exact contact
list; any change clears it. Render only sourced stored merge fields; a missing
field drops the sentence/line rather than guessing. AI remains off and no OpenAI
calls are needed (credit exhausted).

Poll before sending. Use shared send lock, durable unique ledger claims, Sent
checks before and provider-id verification after each send. Reserve before network
I/O. Failed/ambiguous sends pause for review and are never blindly retried. Stop
batch on first delivery failure. Missing Sent evidence blocks sending. Repeated
runs and process restarts must not duplicate sends/tasks.

## LinkedIn and daily routine

No LinkedIn or Sales Navigator automation of any kind: no scraping, browser bots
or unofficial APIs. Create tasks with person, profile/search link, drafted note
and message. Sai opens LinkedIn, sends manually, marks done or pastes the reply.

Opt-in Railway worker, weekdays from 08:30 Europe/London: poll replies then run a
bounded due batch. Safe repeated ticks drain the queue within spacing/caps; do not
sleep inside requests. Manual Run now uses the same service/ledger. Single replica,
persistent SQLite volume, same limitations as existing workers. Document environment
setup; no deployment or production access during implementation.

## Review milestones

1. Commit this plan alone after the full fake-based suite.
2. Additive schema, injectable service, imports and safety tests.
3. Authenticated jobs/API, CLI, opt-in worker and deployment notes.
4. Theme-matched Outreach workspace, Today tasks, approvals and metrics.

Apollo lookup must use only free people search. Reveal is a separate per-batch
credit-cost-confirmed action; never silently reuse a finder that reveals emails.
Implementation and tests use fakes, with no real mail or paid provider calls.
