### Introduction

Server Refresh Automation is a draft proposal (21 October 2026) for operators and engineers. It replaces the manual, interactive `refresh_server.py` workflow with automatic fleet detection, an operator-reviewed schedule, unattended execution with retries and health checks, and a deliberate, low-friction way to recover if a rollout goes wrong. This synopsis describes the whole product; full engineering detail is in [server-refresh-automation.md](./server-refresh-automation.md) and, for the execution engine this version uses, [server-refresh-automation-temporal.md](./server-refresh-automation-temporal.md).

### The problem

Server Refresh replaces one EC2 web server with a new instance so sites move onto a newer machine image. Today an operator triggers it with `PUT /api/hosting/v1/webserver/failover?isRefreshServer=true`. Once that call is accepted, the existing ECS worker creates the replacement, installs sites, waits for CPU to settle, updates routes, deletes the old server, and swaps names — that part already works and isn't changing.

What's missing is everything before that call. The current AMI is a configuration value (Systems Manager Parameter Store, read by the hosting API); nothing watches it, and nothing tells an operator which servers in a fleet of up to a couple thousand are still on an older image — the `webserver` database table doesn't even record which AMI a server is running, only EC2 itself knows that. The only "one click" today is `refresh_server.py`: a hardcoded list of server ids, interactive, with no AMI check, no fleet-wide visibility, no schedule, and no recorded approval. Slack is the only notification. And because the platform can only safely refresh a handful of servers at a time, rolling out one AMI to a full fleet today means days of an engineer manually re-running that script.

### What a refresh is

| | Server Refresh | Emergency failover |
|---|---|---|
| What is created | A new EC2 instance | Nothing — sites move onto servers that already exist |
| Site placement | Eligible sites stay together on the replacement | Each site migrates independently |
| Typical use | Planned AMI / OS rollout | A server that must be abandoned |

This proposal is entirely about the first column. Emergency failover is unchanged and out of scope.

### What we would build

Four pieces, working together:

1. **AMI Ingestion.** Listens for AWS's own "new Windows AMI" notification. A candidate AMI is never trusted automatically — it must first be proven on a single Development server. That verification run is itself just a small, one-server version of the same campaign mechanism described below, so there's no separate system to build for it. Once proven, the fleet-wide list of servers that need it is produced automatically — a database query plus a live check against each server's actual running image — rather than an operator typing server ids by hand.
2. **Operator Gate UI.** Shows the AMI's verification state, the automatically detected candidate list, and lets an operator review and adjust how that work is grouped into batches and scheduled, before anything runs. The same screen also drives recovery: if a rollout finishes with failures, one action creates a ready-to-review follow-up campaign targeting exactly those servers.
3. **Campaign Runner.** The engine that actually runs a scheduled or on-demand campaign unattended — calling the refresh for each server, retrying safely, checking health, and recording the outcome. This is where the proposal in this document differs from the AWS-native alternative: it runs on **Temporal Cloud**, a workflow engine the organization already operates and has approved, rather than on a hand-built combination of a scheduler, a workflow service, and a database full of manually-written locks and retry counters. A small, purpose-built gatekeeper API — the **Campaign Control Plane** — sits between the operator UI and Temporal so the UI itself never has broad, direct access to start production work.
4. **The refresh mechanism itself.** A new, narrow API endpoint on the existing hosting platform accepts an explicit AMI id for one server at a time; the existing refresh machinery (create, install, settle, cut over, rename) runs exactly as it does today underneath it. The existing failover endpoint and `refresh_server.py` are not touched at all.

Recovery is never automatic. If a rollout fails on some servers, an operator decides whether and when to roll those specific servers back — the system only makes that decision fast to act on, not automatic.

### Decisions already settled

- Execution runs on Temporal Cloud rather than a hand-built scheduler/lock/retry scheme, because the organization already runs it and the usual cost of adopting a new workflow engine has already been paid elsewhere.
- A new AMI is never used anywhere until it has passed a one-server verification run and a human has explicitly signed off — there is no software-only approval gate, by design.
- Rollback is always an explicit, operator-initiated action. The system makes it a one-click, pre-filled action; it never decides to do it on its own.
- The existing `PUT /webserver/failover` endpoint and `refresh_server.py` are not modified in any way.
- No AppConfig or SSM configuration is written anywhere in this pipeline — the current configuration-based AMI selection stays exactly as it is for every caller that doesn't go through this new path.
- Emergency failover is not part of this product.
- Batch sizing (how many servers run at once) is an operator-adjustable setting in the UI, not a number fixed in the system.

### The product is done when an operator can

1. See, for a given environment, the automatically detected list of servers still on an old image — with no server id ever hand-typed.
2. Start a one-server verification run for a new AMI with one click, and review its pass/fail checklist before deciding to allow it into a real rollout.
3. Review and adjust how detected servers are grouped into scheduled batches before anything runs.
4. Watch a scheduled campaign run unattended — retrying safely, checking site health, and recording a clear outcome per server, without needing to babysit it.
5. Read that same outcome in Slack, the moment it happens, without checking the UI first.
6. See a new AMI's arrival, a completed verification, and any rollout that finishes with failures as Slack notifications too, so an operator never has to go looking for any of them.
7. From a campaign that finished with failures, create a ready-to-go rollback campaign — targeting exactly those servers, on the AMI they were running before — in one action.
8. Trust that nothing in this pipeline was ever auto-started or auto-rolled-back without an explicit person deciding to do so.

`refresh_server.py` can remain as a manual, unaffected fallback for as long as anyone wants it.

### Still to confirm

1. The exact default for how many servers may run at once per batch, and whether that default should differ by environment.
2. Who administers access to the Campaign Control Plane and to Temporal Cloud's own dashboard — likely the same group that already administers the hosting platform, but worth confirming explicitly.
3. Whether a failed rollout should page anyone directly, or whether the Slack notification plus the one-click rollback action is enough.
4. Whether CloudWatch/Sumo search remains the primary place engineers debug a run, with Temporal's own dashboard as a supplement, or the reverse.

### Glossary — web server statuses

These are statuses on the hosting web server record (not site statuses). A server is eligible to start a refresh only from `Created` or `FailingOverFailed`.

| Status | Who put it there | Meaning for refresh |
|---|---|---|
| `Created` | Successful create, or a successful refresh renaming the new server | Ready to start a refresh |
| `FailingOver` | The refresh API when a refresh is accepted | Refresh already in progress — do not start another |
| `FailingOverFailed` | The worker, after a failed refresh | Previous attempt failed — retry allowed |
| `Deleted` / `Deleting` | Successful cutover (old server) or delete path | Not a refresh candidate |
