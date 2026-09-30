# Brainstorm: ITB Triage for Ferry Electric

**Format note.** This session ran without a live PM in the room, so it is written as the record
a sparring partner would leave behind: the framing, the ideas that were pushed on, the positions
taken, and what got parked. Research backing is in `docs/research/bidding-process.md`.

---

## 1. Frame

**Starting point.** A half-formed idea with a clear shape: read incoming invitations to bid,
score fit by project type, size and GC, and give estimators a short morning summary of scope and
bid dates.

**The clarifying question I would have asked first:** *Is the pain "we miss good bids" or "we
waste hours on bad bids"?* They lead to different products. The first wants recall and calendar
discipline. The second wants a ruthless filter. The research says specialty subs suffer both, but
the second is where money is lost: an estimator who quotes six jobs badly instead of two well is
the failure mode the literature names most often. **Position: build the filter first, and make the
calendar a by-product of the filter.**

**What we already know.**
- Ferry is a ~$22M union electrical sub, seven named markets, Southwestern PA plus WV.
- Inbound volume for a sub like this is on the order of 40 invitations a week across
  BuildingConnected, Procore, iSqFt, PlanHub, and direct GC email, plus reminders, addenda and
  date changes that multiply each invitation into a thread of 5 to 10 messages.
- The decision the estimators actually make is bid / pass / maybe-later, and the inputs are:
  who is the GC, what kind of building, how big, where, when is it due, what's weird about it.

**What a good outcome looks like.** The chief estimator opens one email at 6:30 AM, reads for
five minutes, and knows: what came in, which two or three deserve a takeoff, what is due this
week, and what changed overnight. Nothing important is discovered by accident in a shared inbox.

## 2. Diverge

Ideas generated, including the ones that lost:

1. **Inbox-to-scored-list.** Connect the estimating mailbox, classify every message, extract
   fields, score, digest. The obvious idea, and it is the core.
2. **GC memory.** Track every GC's invite count, our bid count, our win count, and how they
   treat us (pay speed, scope-leveling behavior, "bid shopping" reputation). Make GC fit a
   *learned* number over time rather than a static tier. This turns the tool from a filter into an
   asset that compounds.
3. **Capacity-aware scoring.** A great job due the same day as two other great jobs is worse
   than it looks. Score against the bid calendar and estimator load, not in isolation.
4. **Addenda and date-change tracker.** Attach every follow-up message to its opportunity and
   surface deltas ("due date moved from 10/16 to 10/21; Addendum 2 adds site lighting"). This is
   where public-bid disqualifications happen.
5. **Auto-reply intent.** Let the estimator's "Pass" click send the GC a polite decline, and
   "Bid" send a will-bid response, so Ferry keeps receiving addenda and keeps goodwill.
6. **Plan-set peek.** Pull the E-sheet index and fixture schedule from the drawing set to
   estimate scope size before a human opens Bluebeam. Powerful, but it depends on portal logins
   and large PDFs. Parked for v2.
7. **Outcome feedback loop.** Record submitted / won / lost / price, and use it to re-tune
   scoring weights and GC scores. Essential for the "compounds over time" story.
8. **Public-notice ingestion.** Scrape PennBID, PA eMarketplace, and university procurement pages
   for Separations Act prime electrical bids. Valuable if Ferry bids prime work; unknown.
9. **Teams / Slack delivery** instead of, or in addition to, email.
10. **Bid calendar feed** (ICS) into Outlook so due dates and pre-bids show on everyone's calendar.
11. **A full bid board UI.** Kanban of opportunities by status. Tempting, and every competitor
    has one. It is also another place to log into, which is exactly what the estimators do not want.

## 3. Provoke

**"Why not just buy Downtobid Bid Board?"** It does inbox parsing and a board. Three honest
answers: (a) it is a generic board, not a fit scorer tuned to one firm's GC history, union
constraint, and territory; (b) it is a destination, not a delivery, and the ask here is a morning
summary; (c) a firm-specific tool can hold the GC memory and outcome data that a SaaS board will
not let you shape. The risk in the other direction is real too: if this tool becomes a worse
generic bid board, it loses. **Keep it opinionated: score, summarize, deliver. Do not build a
board in v1.**

**"Is the LLM the product?"** No. The LLM reads unstructured email and PDF into a schema. The
product is the scoring model and the daily ritual. If the extraction were done by a human intern,
the value proposition would be identical. Design consequence: scoring must be deterministic and
explainable, with the LLM confined to extraction and summarization. Estimators will not trust a
number they cannot argue with.

**"Can scoring be right on day one?"** No. Weights will start as the chief estimator's opinion
encoded in a config file. The tool earns trust by showing its reasons ("GC tier A, higher-ed,
est. $1.8M electrical, 22 minutes away, due in 12 days") and by letting the estimator override.
Overrides and outcomes are the training data for tuning. **Ship with a scoring profile editor
and an audit trail, not with a model.**

**"What if extraction is wrong about the due date?"** That is the one error that costs a bid.
Mitigations: keep the source sentence next to every extracted date; mark low-confidence dates;
never silently update a date on an existing opportunity without flagging the change in the digest;
prefer the earliest plausible due date when two conflict.

**"Shared inbox or personal inboxes?"** Unknown. Design for a shared estimating mailbox as the
primary source and a forward-to address as the escape hatch, so any estimator can push a personal
email into the system with one forward.

**"Who owns the config?"** The chief estimator. If it takes an engineer to change a GC tier or a
size band, the tool will drift from reality within a quarter.

## 4. Converge

**Strongest direction.** *A scored, deduplicated opportunity feed delivered as a morning digest,
with one-click bid/pass decisions that feed GC memory and scoring weights.* That is ideas 1, 2,
3, 4, 7 and 10, with 5 as a fast follow.

Why this one: it targets the estimator-hours problem directly, it needs no new habit beyond
reading one email, and every decision made in the digest makes the next digest better.

**Riskiest assumption.** That the extraction plus a rule-based score is good enough that
estimators keep reading the digest after week two. If scores feel random, they revert to the
inbox. Mitigation is explainability plus a two-week calibration period where the chief estimator
scores a backlog of past ITBs by hand and we compare.

**Second riskiest.** That enough signal exists in the *email* to score without opening the plan
set. Direct GC emails can be as thin as "See attached ITB. Bids due 10/16." The ITB PDF usually
has the rest, so PDF attachments are in scope for v1; drawing sets are not.

**Parked (worth revisiting).**
- Plan-set peek (idea 6): v2, once portal access is sorted.
- Public-notice scraping (idea 8): confirm whether Ferry bids prime work first.
- Full bid board (idea 11): only if the digest proves insufficient for tracking status.
- Vendor-quote reminders (gear lead time): a natural extension once bids are tracked.

**Suggested next step.** Collect 60 to 90 days of real ITB emails from Ferry's mailbox, have the
chief estimator label each with bid/pass and a fit rating, and use that set both to tune the
scoring profile and as the regression fixture for extraction. Everything in the specs assumes this
labeled corpus exists by the end of the first sprint.

## 5. Working name

`bidtriage`. Internal, unglamorous, and describes what it does.
