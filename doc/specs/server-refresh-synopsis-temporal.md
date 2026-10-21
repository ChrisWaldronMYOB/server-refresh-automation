# Server Refresh Automation — Temporal Proposal (Synopsis)

**Date:** 2026-10-21
**Audience:** Anyone deciding whether to build the orchestration layer on Temporal Cloud or on AWS-native services — not just engineers.
**Full detail:** [server-refresh-automation-temporal.md](./server-refresh-automation-temporal.md) (this proposal, in engineering detail) and [server-refresh-automation.md](./server-refresh-automation.md) (the AWS-native baseline it's being compared against).

### The problem, briefly

Refreshing XWING's web server fleet onto a new machine image is today a manual, interactive process: an engineer hand-edits a list of server ids in a script and watches it run. There's no automatic detection of which servers need a new image, no scheduling, no safe way to recover if a rollout goes wrong, and no visibility beyond that one engineer's terminal.

The fix, regardless of which engine runs it underneath, has four parts: automatically detect which servers need refreshing, let an operator review and schedule that work instead of hand-typing server ids, run it unattended with retries and health checks, and give the operator an easy, deliberate way to recover if a rollout fails. All four parts are fully designed in `server-refresh-automation.md` and **don't change based on this document** — what's different here is only *what actually runs the scheduling and retry logic underneath*.

### The one real question this document answers

Something has to decide when to run a batch, retry a failure, make sure the same server never gets refreshed twice by accident, and remember exactly where it left off if a process crashes mid-run. The baseline spec builds that "something" by hand, out of AWS building blocks: a scheduler, a workflow service, and a database full of locks, retry counters, and dedup records that a developer has to design and get right.

This proposal uses **Temporal Cloud** instead — a workflow engine built specifically to solve that exact class of problem — which the organization already runs and has approved. Normally, adopting a new workflow engine is itself a significant cost (a new vendor, a new security review, a new thing to operate). Here that cost has already been paid by someone else. The question is just whether to use what's already available instead of re-building a smaller version of it by hand.

### Why that's worth doing

Picture the background worker crashing halfway through refreshing five servers today. Something has to notice, figure out which of the five are actually still in progress, and safely resume without refreshing any of them twice. In the baseline design, that "something" is a lease table in a database, with manual expiry and renewal logic a developer wrote and has to keep correct. Under Temporal, that is the engine's job — it is what the product is built to do, not a feature our own code has to get right.

The same is true for retrying a failed call, making sure a scheduled job never double-starts, and giving someone a clear, searchable record of exactly what happened to one specific server at one specific attempt. Each of those is currently a small, easy-to-get-subtly-wrong piece of hand-written plumbing. Under Temporal, each is close to a built-in feature.

One concrete, user-visible improvement falls out of this for free: today, even a single operator-triggered "verify this new AMI" run has to wait for a scheduler to notice it — up to a few minutes of dead time before anything happens. Under Temporal, it starts the instant the operator clicks the button.

### What doesn't change

The AMI-detection logic, the operator-facing UI, the new XWING Refresh API, the rollback workflow, and the Slack notifications are **identical** either way — none of that depends on which engine runs the schedule underneath. An operator using the UI wouldn't be able to tell which version of the backend they're using, except that one is slightly faster to start an on-demand check.

### What's new: a small gatekeeper service

Adopting Temporal adds one new piece worth calling out: a small, purpose-built API — the **Campaign Control Plane** — sitting between the operator UI and Temporal. Rather than the UI being handed broad, direct access to start production work, it talks to this narrow gatekeeper instead, which can only do four specific things (start a verification check, create or change a schedule, cancel a schedule, check a run's status). That same gatekeeper is where the single most important safety rule in this whole product — *never run against an AMI that hasn't been verified* — is enforced as a hard check that no bug or future shortcut elsewhere in the system can bypass. It can also be tested entirely on its own, without the UI running at all.

### Trade-offs, plainly

**What's gained:** fewer hand-built places for a subtle bug to hide, a smaller, simpler configuration model, instant on-demand checks, and a second, independent debugging view (Temporal's own dashboard) on top of the logging this product already has.

**What it costs:** one new always-on service to run (the piece of code that actually executes the scheduling logic), the small gatekeeper API described above, and some ramp-up time for whoever builds it to learn Temporal's programming model. None of that is a new vendor relationship or a new security review — that part is already done.

### At a glance

| | Baseline (AWS-native) | This proposal (Temporal) |
|---|---|---|
| What decides when to run a batch | A custom scheduler polling every few minutes | Temporal, directly |
| Retry / don't-double-refresh logic | Hand-built database locks and counters | Built into the engine |
| Starting an on-demand check | Up to a few minutes' delay | Instant |
| New things to run | None — reuses existing AWS services | A worker process, and the Campaign Control Plane |
| Debugging a stuck run | Logs and database records | Same, plus Temporal's own dashboard |
| Detection, UI, API, rollback, notifications | Identical | Identical |

### Bottom line

Given Temporal Cloud is already available and approved, the main reason to hold off on it — the cost and risk of adopting a new workflow engine — doesn't apply here. The remaining cost is a normal, one-time engineering integration, not a new organizational dependency. We'd recommend building the orchestration layer on Temporal rather than the hand-built AWS-primitives version, unless there's a reason specific to this team's experience or capacity that argues otherwise.
