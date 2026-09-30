# Deep Research: How Electrical Subcontractors Receive, Triage, and Bid Work

**Purpose.** Ground the ITB triage tool in how bidding actually works for a union electrical
subcontractor in Western Pennsylvania. Everything the product scores, extracts, or summarizes
should trace back to a step in this document.

**Date.** 2026-09-29. Sources at the end. Where a claim is an inference rather than a sourced
fact, it is marked *(inference)*.

---

## 1. The company: Ferry Electric Company

| Fact | Value | Source |
|---|---|---|
| Location | 250 Curry Hollow Road, Pittsburgh, PA 15236 | ferryelectric.com |
| Founded | 1926, third-generation family ownership, centennial in 2026 | ferryelectric.com |
| Size | ~$21.9M revenue, ~29 employees (office headcount; field labor is dispatched from the union hall and fluctuates) | ZoomInfo / LeadIQ; *(inference on field labor)* |
| Affiliation | Member, Western Pennsylvania Chapter of NECA. NECA contractors in Pittsburgh are signatory to IBEW Local 5. | ELECTRI / NECA listing; *(inference on Local 5)* |
| Territory | Pittsburgh, Southwestern PA, northern West Virginia | ferryelectric.com/about-us |
| Markets (verbatim from /markets) | Commercial & Office; Healthcare; Higher Education; High Tech / Research / Data Center; Light Industrial / Utility; Multi Family / Hotel / Mixed Use / Retail; Religious | ferryelectric.com/markets |
| Stated concentrations | Corporate offices, high-tech facilities, colleges and universities, religious organizations | ferryelectric.com/about-us |
| Systems installed | Power, lighting, voice/data, fire alarm, back-up power (generators/UPS), renewable energy (solar, wind, hydro), power quality | ferryelectric.com |
| Delivery methods | Hard bid, design-build, collaborative delivery; BIM and prefabrication in-house | ferryelectric.com/methods |
| Project range | "Multimillion-dollar contracts to small service calls" | ferryelectric.com |
| Distinctions | Multiple MBA (Master Builders' Association) safety commendations; Pittsburgh's first LEED CI Gold and first LEED Platinum projects | ferryelectric.com |

**What this means for the tool.**
- Ferry is a mid-size union sub. Its sweet spot is *(inference)* roughly $250K to $5M of electrical
  scope on commercial, institutional and light industrial buildings. A $40M hospital tower's
  electrical package ($8M+) is probably above bonding/labor comfort; a $15K tenant fit-out is
  service work, not estimating work. These bands must be configurable, not hard-coded.
- Union signatory status is a hard filter: ITBs that require or strongly imply open-shop pricing
  (merit shop GC, residential tract work) are a poor fit regardless of type.
- Territory is a drive-time question. Curry Hollow Road is in the South Hills. Morgantown WV is
  ~75 minutes; Erie is ~2 hours and effectively out of territory. Prevailing-wage public work in
  the region (PA Prevailing Wage Act applies to public projects over $25,000; Davis-Bacon on
  federally funded work) is *compatible* with union labor rates and therefore a neutral-to-positive
  signal for Ferry, unlike for a merit-shop sub.
- Ferry's sustainability and renewable history makes solar / battery / EV-charging scopes a
  differentiator worth boosting.

## 2. Who sends ITBs, and through what channel

An estimator's inbox receives bid solicitations from five kinds of senders:

1. **General contractors and construction managers** (the dominant source). In Pittsburgh the
   relevant GC/CM population includes PJ Dick, Rycon, Mascaro, Massaro, Landau, Jendoco, Burchick,
   Volpatt, Turner (Pittsburgh office), Continental Building Company, Sota Construction, A. Martini,
   dck worldwide, Franjo, Allegheny Construction Group, TEDCO, and Shannon Construction, among
   others. PJ Dick was the region's largest contractor by 2023 billings ($1.56B), with Rycon
   ($1.13B) and Mascaro ($444M) also in the top ten.
2. **Owners and institutions directly**, for prime electrical contracts under Pennsylvania's
   Separations Act (public bodies must bid electrical, plumbing, HVAC and general work as separate
   prime contracts). Universities (Pitt, CMU, Duquesne, PASSHE schools), school districts, hospital
   systems (UPMC, AHN), and municipalities issue these. They arrive via PennBID, the PA eMarketplace,
   university procurement portals, or plain email from a facilities project manager.
3. **Bid-management platforms acting on behalf of GCs**: BuildingConnected (Autodesk),
   Procore Bidding, iSqFt / ConstructConnect Bid Center, PlanHub, SmartBid, Pantera, Dodge,
   Downtobid, and BidMail-style email blasts.
4. **Lead services** (Dodge, ConstructConnect, BidClerk) that resell public project notices.
   These are leads, not invitations; they are lower-priority in v1.
5. **Repeat customers** asking for budgets or T&M quotes on small work. These often arrive as
   informal emails with no due date at all.

### 2.1 Platform notification anatomy

| Platform | From address / signature | What the email contains | What requires login |
|---|---|---|---|
| BuildingConnected | `team@buildingconnected.com`; sender display name is the GC's estimator | Project name, GC name, "View this RFP" / "View Bid Form" link, snippets of location, bid due date and description when available. Bid Board shows project number, name, bid due date, location, job walk, RFI due, expected start/end, project size, architect, description. Distinguishes "invitation to bid" from "request for budget." | Files, full scope, bid form. Addenda arrive as "message converted to addendum" notifications to bid followers. |
| Procore Bidding | GC's Procore project; primary contact named on invitation | Bid package title, invitation from primary contact, due date; optional pre-bid RFI deadline; "View in Procore" link. Subs are asked to declare Will Bid / Will Not Bid early. Updates go out as "correspondence." | Documents, bid form. |
| iSqFt / ConstructConnect | ConstructConnect messaging system unless GC pays for a custom From address | Project, GC, due date, link | Plan room |
| PlanHub / SmartBid / Pantera | Platform address | Similar; PlanHub in particular blasts broadly and produces many low-relevance invites | Plan room |
| Direct GC email | GC estimator's own address | Free-form. Often a PDF "Invitation to Bid" letter attached, a link to a Dropbox/Box/ShareFile/Egnyte plan folder, and the bid form. Frequently CCs several people, so every addendum and schedule change fans out as duplicate emails. | Sometimes nothing |
| Public owner | Procurement office | Legal advertisement text: project title, contract number, prime contracts being let, pre-bid meeting (often mandatory), bid opening date/time/place, bond requirements, plan deposit, prevailing wage statement | Plan holder registration |

### 2.2 The email lifecycle of one bid

A single opportunity generates a *thread* of messages over two to six weeks, not one email:

1. Initial invitation (possibly duplicated across two channels, e.g., a BuildingConnected invite
   *and* a personal email from the GC's estimator).
2. Reminder to respond with intent ("Please indicate if you will be bidding").
3. Pre-bid meeting notice or reminder (sometimes mandatory; missing it disqualifies the bid).
4. Addendum 1 ... n (each may change scope, dates, or alternates; a bid that misses the last
   addendum is a bid on the wrong job and can be rejected on public work).
5. RFI responses / clarifications.
6. Bid due date extension (common; often issued 24 to 72 hours before the original date).
7. "Bids due tomorrow" reminder.
8. Post-bid: scope review request, "best and final," award or regret notice.

Practitioner reports put inbound ITB volume at roughly 40 invitations per week for a specialty sub
across platforms and email, most of them poor fits, and 100+ ITB-related emails per day for busy
subs when duplicates and updates are counted. Subs report spending 10 to 15 hours per week
forwarding invites, building tracking spreadsheets, and chasing addenda.

## 3. The bid/no-bid decision

The estimating department's scarcest resource is estimator hours. An electrical takeoff on a
$1M to $3M project takes 20 to 60 hours. Bidding everything means bidding badly; the goal of
triage is to spend those hours on the bids most likely to be won *and* profitable.

Published bid/no-bid frameworks for specialty subs converge on the same factors. Ranked by how
often they appear and how decisive practitioners say they are:

| Factor | Why it matters | How it shows up in an ITB | Data the tool can get |
|---|---|---|---|
| **GC / owner relationship** | "The single highest-leverage variable." A GC that pays on time, levels bids fairly, and has awarded you work before is worth more than 2% margin. Conversely, a GC that only uses your number to check its favorite sub wastes the takeoff. | Sender identity | GC directory: relationship tier, historical invites vs. awards, payment behavior, notes |
| **Trade / scope fit** | Is the electrical package the kind of work you do? Lighting + power + fire alarm on a mid-rise office: yes. Traffic signals, substations, residential wiring: no. | Scope description, spec sections listed, project type | Extracted project type, Division 26/27/28 items, keywords |
| **Project size** | Too small: overhead eats margin. Too large: bonding capacity, cash flow, and labor loading risk. Electrical is typically 8 to 15% of building cost (higher for data centers, labs, hospitals; lower for warehouses). | Stated project value, square footage, building type | Estimated electrical value = project value × type-specific electrical share, or explicit statement |
| **Location** | Drive time drives labor cost (travel pay in the IBEW agreement beyond certain zones), supervision, and productivity. | Address | Geocoded distance from Curry Hollow Road |
| **Timing and capacity** | Days until bid due vs. estimator workload; number of bids already due that week; construction start vs. field labor availability. | Due date, pre-bid date, start date | Bid calendar; capacity settings |
| **Bid type and competition** | Hard bid vs. budget/GMP vs. design-build vs. negotiated. Public hard bids draw 4 to 8 electrical bidders; a negotiated design-assist job with a friendly CM may have 2. | "Request for budget", "ITB", "RFP", "design-build" | Extracted bid type; public/private flag |
| **Contract terms and risk** | Retainage, pay-when-paid, liquidated damages, bond requirement, prevailing wage, mandatory pre-bid, unusual insurance | ITB letter and front-end specs | Extracted flags |
| **Strategic value** | New market entry, key-account relationship, showcase project (LEED, renewable) | Owner name, project description | Configurable boosts |
| **Plans availability** | Can you actually get the documents and is the set complete enough to price? | Link, "drawings to follow" | Presence of document link; completeness flag |

A weighted scorecard with a minimum threshold is the standard recommendation. Practitioners warn
that "enthusiasm should not outrun evidence" and that weights must be tuned per firm.

## 4. The estimating workflow after "bid"

Once a job is accepted, the estimator's process (which the v1 tool supports but does not perform):

1. **Register the bid**: log in the bid calendar with due date/time, pre-bid, RFI deadline, and
   addenda-received log. Declare intent to the GC (Will Bid) to keep receiving addenda.
2. **Document control**: download the full set; index drawings (E-sheets, one-lines, panel
   schedules, lighting fixture schedule, site plans, architectural RCPs), specs (Div 26/27/28 plus
   Div 01 general requirements and Div 00 front end), and every addendum.
3. **Scope definition**: read the ITB scope letter; identify inclusions, exclusions, owner-furnished
   equipment, work by others (e.g., low-voltage by owner's vendor, temp power, excavation),
   alternates, unit prices, allowances.
4. **RFIs**: submit questions before the RFI deadline to defend the scope.
5. **Vendor quotes**: request lighting fixture packages, gear (switchboards, panels, transformers),
   generator/UPS, fire alarm, and low-voltage quotes. Lead time matters: gear quotes take days.
6. **Takeoff**: count devices, fixtures, feeders, branch circuits, conduit, wire, using Accubid,
   ConEst, McCormick, or Trimble Estimation, with Bluebeam or on-screen takeoff.
7. **Pricing**: apply labor units (NECA Manual of Labor Units), union wage/fringe rates, material
   pricing, subcontractors, equipment, overhead, profit, bond.
8. **Bid review**: senior estimator reviews totals, unit checks (cost per sq ft, per fixture,
   labor hours per $), alternates, exclusions, addenda acknowledgment.
9. **Submission**: proposal with scope letter, clarifications, exclusions, addenda acknowledged;
   uploaded to the platform or emailed before the deadline (public bids: sealed, at a place and
   time, with bid bond).
10. **Post-bid**: scope review with GC, de-scoping meeting, best-and-final, award or regret.
    Feed the result back into GC history.

## 5. Scope vocabulary the extractor must recognize

Division 26 Electrical: 26 05 (common work: conduit, wire, boxes, grounding, hangers, identification),
26 09 (lighting controls), 26 10 (medium voltage), 26 20 (low-voltage distribution: switchboards,
panelboards, transformers, wiring devices, motor controls), 26 30 (generators, UPS, batteries,
solar PV), 26 40 (lightning protection, surge), 26 50 (interior, exterior, emergency lighting).

Division 27 Communications: 27 10 structured cabling, 27 20 data, 27 30 voice, 27 40 audio-video,
27 50 distributed systems (nurse call, paging, clocks, DAS).

Division 28 Electronic Safety and Security: 28 10 access control and intrusion, 28 20 video
surveillance, 28 30 (older numbering) / 28 46 fire detection and alarm.

Frequently associated scopes and phrases: "temporary power," "site lighting," "EV charging,"
"generator and ATS," "fire alarm by others," "low voltage by owner," "lighting fixture package
furnished by owner," "design-build electrical," "design-assist," "BIM coordination required,"
"prefabrication encouraged," "prevailing wage," "PLA (project labor agreement)," "responsible
contractor ordinance," "MBE/WBE participation goals," "bid bond 10%," "P&P bond required,"
"liquidated damages," "retainage 10% reduced to 5% at 50% complete."

## 6. Dates and deadlines that matter

| Date | Typical form | Consequence of missing |
|---|---|---|
| Bid due | "Bids due Thursday, October 16 at 2:00 PM EST" (often the GC's own internal date, 1 to 3 days before the owner's) | Bid not accepted |
| Pre-bid meeting / job walk | "Mandatory pre-bid Tuesday 10/7 at 10 AM at the site" | Disqualification if mandatory; lost site knowledge otherwise |
| RFI / questions deadline | "Questions due 10/9 by noon" | Scope ambiguity priced as risk |
| Addendum issue dates | "Addendum No. 2 issued 10/13" | Wrong-scope bid |
| Intent-to-bid response | "Please respond by Friday" | Dropped from distribution; stop receiving addenda |
| Anticipated start / duration | "Construction start January 2027, 14 months" | Labor loading conflicts |
| Bid validity | "Hold pricing 60/90 days" | Material escalation risk |

Time zones are almost always Eastern; ITBs frequently omit the year, the time, or both, and
platform emails render dates in the GC's locale. Date extraction must therefore record a
confidence and preserve the source text.

## 7. What "good" looks like for the morning summary

Interviews and practitioner write-ups describe the estimator's ideal morning as a five-minute
read that answers, in order:

1. What is *new* since yesterday, ranked by fit, with enough scope to decide in one read.
2. What is due this week, and what is at risk (no decision yet, no documents downloaded, pre-bid
   tomorrow).
3. What *changed* on jobs we are bidding: addenda, date moves, new RFIs answered.
4. What needs a decision from me specifically.

Anything longer gets skimmed; anything without a clear "why this score" is distrusted.

## 8. Competitive landscape (for positioning, not parity)

- **Downtobid Bid Board** (also sold through Dodge): connects to the inbox, AI-parses invites,
  adds relevant projects to a board/calendar with deadlines and GC info, sends reminders. This is
  the closest analog. It is general-purpose across trades and is not tuned for one firm's GC
  history, union constraints, or capacity.
- **BuildingConnected Bid Board Pro**, **Procore**, **ConstructConnect Bid Center**: each tracks
  only invitations that flow through its own platform.
- **BidIntell, ConstructionBids.ai, Contracts Connected**: bid/no-bid checklists and scorecards,
  mostly static templates.
- **Basisboard** (acquired by Downtobid): email-to-board automation for subs.

Differentiation for a single-firm tool: scoring calibrated to Ferry's markets, GC relationships and
capacity; unifying *all* channels including direct email and public advertisements; a feedback
loop from bid outcomes; and a morning digest rather than another dashboard to log into.

## 9. Assumptions to validate with Ferry's estimators

1. Which mailbox(es) receive ITBs today (shared estimating@ vs. individual estimators), and the
   email platform (Microsoft 365 assumed).
2. Which platforms Ferry has accounts on, and whether platform logins can be shared with the tool
   for document retrieval (v2).
3. The actual size band and the GC tier list.
4. Number of estimators, current bid volume per week, and current hit rate.
5. Whether Ferry bids public prime electrical work (Separations Act) or focuses on GC-subcontract
   work. This determines how much weight public-notice ingestion deserves.
6. Whether a morning email is the right surface or a Teams post is preferred.

## Sources

- Ferry Electric Company: https://www.ferryelectric.com/ , /markets , /about-us
- ZoomInfo company profile: https://www.zoominfo.com/c/ferry-electric-co/42077744
- LeadIQ profile: https://leadiq.com/c/ferry-electric-company/5a1d832324000024005ddef2
- ELECTRI International contributor page (NECA membership): https://www.electri.org/electri_contributors/ferry-electric-company-james-j-ferry-ii
- BuildingConnected, "I received an invitation to bid. What should I do?": https://support.buildingconnected.com/hc/en-us/articles/360021597733
- BuildingConnected, "Bid notifications (Bid Board)": https://support.buildingconnected.com/hc/en-us/articles/360020892813
- BuildingConnected, "Best practices for owners setting up projects": https://support.buildingconnected.com/hc/en-us/articles/360025488134
- Procore, "Submit a Bid": https://v2.support.procore.com/product-manuals/planroom-company/tutorials/submit-a-bid
- Procore, "Create a Bid Package": https://support.procore.com/products/online/user-guide/project-level/bidding/tutorials/create-a-bid-package
- Procore, "Can I send a bid update email to every vendor/subcontractor?": https://support.procore.com/faq/can-i-send-a-bid-update-email-to-every-vendor-subcontractor
- ConstructConnect iSqFt: https://www.constructconnect.com/products/isqft
- Downtobid Bid Board (via Dodge): https://www.construction.com/bid-board/
- Downtobid, "How estimating emails kill specialty subs": https://downtobid.com/blog/how-estimating-emails-kill-specialty-subs
- Downtobid, "Best Bid Board Software for Subcontractors 2025": https://downtobid.com/blog/best-bid-board-software-subcontractors-2025
- Buildr, "Invitation to Bid Software: A Guide for 2026": https://buildr.com/blog/invitation-to-bid-software/
- BidIntell, "Bid No Bid Checklist for Specialty Subcontractors": https://bidintell.ai/bid-no-bid-checklist
- MeltPlan, "How GCs Decide Whether to Bid on a Project": https://www.meltplan.com/blogs/how-gcs-decide-whether-to-bid-on-a-project-the-bid-no-bid-decision-framework
- ConstructionBids.ai, "Bid/No-Bid Risk Scorecard 2026": https://constructionbids.ai/kits/bid-no-bid-risk-scorecard
- Contracts Connected, "Bid / No-Bid Decision Matrix for Construction": https://contractsconnected.com/templates/bid-no-bid-decision-matrix-construction
- Procore, "Bid or No Bid?": https://www.procore.com/library/bid-or-no-bid
- Procore, "Estimating for Electrical Contractors": https://www.procore.com/library/estimating-electrical-contractors
- Drawer.ai, "Electrical Estimating Guide": https://drawer.ai/blog/electrical-estimating-guide-the-complete-process-for-commercial-contractors
- Red Rhino, "Electrical Scope of Work": https://hardhatis.com/electrical-scope-of-work-estimating/
- McCormick Systems, estimating checklist: https://www.mccormicksys.com/blog/am-i-missing-anything-an-estimating-checklist-for-electrical-plumbing-mechanical-contractors/
- Anvilfield electrical estimating field guide: https://anvilfield.com/field-guides/electrical/electrical-estimating-takeoff-bidding/
- EC&M, "Mastering Advanced Bidding Strategies in Electrical Contracting": https://www.ecmweb.com/construction/estimating/article/55340373/mastering-advanced-bidding-strategies-in-electrical-contracting
- CSI MasterFormat divisions: https://www.archtoolbox.com/masterformat-divisions/ and https://en.wikipedia.org/wiki/MasterFormat
- Pittsburgh Business Times 2024 contractor list (via CentiMark reprint): https://centimark.com/images/PDF%20download%20-%20Waivers/PBT/2024%20Contruction%20Contractor%20-%20Pittsburgh%20Business%20Times.pdf
- Master Builders' Association of Western PA member directory: https://members.mbawpa.org/directory/Search/division-01-general-contractor-427573
