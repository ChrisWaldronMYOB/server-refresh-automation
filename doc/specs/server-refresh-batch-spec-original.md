# Server Refresh Batch — Slim Specification

**Status:** Draft for review  
**Date:** 2026-10-19  
**Audience:** Hosting engineers who will add the Refresh Server API and build the batch task  
**Derived from:** `server-refresh-automation.md`  
**Current client:** `refresh_server.py` (unchanged; remains supported)  
**API codebase:** `advanced-platform-hosting` (`WebServerController.Failover`, `FailoverServerConsumerService`, `WebServerParameterResolver.ResolveAmi`)  
**Data classification:** General (AMBER) for server ids, AMI ids, and batch results. This spec does not handle customer email addresses.

This specification adds an AMI-aware scheduled refresh path alongside the existing ones. An operator verifies an AMI in Development using the prerequisite runbook, then creates a rollout campaign with multiple scheduled batch groups that all use that AMI. EventBridge Scheduler starts a Step Functions Standard workflow for each group occurrence. The workflow launches bounded ECS batch tasks, which call a new XWing Refresh Server API and record per-server results in a DynamoDB registry. The task copies the useful behaviour of `refresh_server.py` (poll, health checks, retries) and does not read interactive input.

**Unchanged and still supported:**

- `PUT /api/hosting/v1/webserver/failover` (including `isRefreshServer=true` refresh via `LatestAMI`)
- `refresh_server.py` and existing refresh clients
- Emergency failover (`isRefreshServer=false`)
- The existing refresh state machine (create, install, settle, routes, delete, rename)

This work does **not** retire or modify the failover API. It does **not** retire `refresh_server.py`.

---

## 1. What this version is

Server Refresh still replaces one EC2 web server with a new instance and moves that server's sites onto it. The usual reason is a newer machine image.

There are now three operator-facing ways to start a refresh:

| Path | How it starts | AMI source |
|---|---|---|
| Failover API | `PUT …/webserver/failover?isRefreshServer=true` | Platform config (`LatestAMI` / `ResolveAmi`) |
| `refresh_server.py` | Client of the failover API above | Same as failover |
| Scheduled campaign (this spec) | EventBridge Scheduler → Step Functions → ECS tasks → `POST …/webserver/{id}/refresh` | One verified campaign AMI in DynamoDB |

What this version adds:

- A Refresh Server API that takes an AMI id on the request. That id is what the replacement instance uses. The call does not read `Configurations.AMIs.LatestAMI` for the refresh replacement.
- A campaign registry for the shared AMI, batch groups, schedules, and per-server progress.
- EventBridge Scheduler and Step Functions Standard to start and coordinate bounded ECS batch tasks.
- The task logs to stdout. ECS sends stdout to CloudWatch, and a subscription filter sends that log group to Sumo.
- A batch summary published through a replaceable notifier. The first implementation is Slack.

What stays:

- The existing refresh state machine creates the replacement, installs sites, settles CPU, updates routes, deletes the old server, and swaps names.
- Failover stays on `PUT /api/hosting/v1/webserver/failover`, unchanged. It continues to support both emergency failover and refresh via `isRefreshServer`.
- Site health checks stay on `GET /api/hosting/v1/website/health-check/{siteName}`.

---

## 2. Removed from the previous (broad automation) specification

This batch version does not include:

- SNS ingest, AMI copy, Certify, or writing `LatestAMI`
- Patch versus major classification, and the Acumatica certification gate
- AppConfig policies for AMI source, test environments, or human versus auto rollout
- Automatic promotion DEV → Staging → Production
- Plan tables, a plan UI, Detector, Tester, and Starter Lambdas
- Customer email
- An interactive retry prompt

AMI verification is a required operator prerequisite described in the [AMI verification runbook](./server-refresh-ami-verification-runbook.md). It is not a batch-task mode, and it does not write `LatestAMI` or `CertifiedAMIs`.

---

## 3. Why a new API exists (failover stays)

`PUT /api/hosting/v1/webserver/failover` takes `isRefreshServer`. `WebServerController.Failover` puts that boolean on `FailoverServerMessage`. `FailoverServerConsumerService.Handle` then chooses the refresh state machine or the emergency site-migration path.

The refresh branch still creates the replacement through `POST /api/hosting/v1/webserver`. `WebServerCreatingState` does not send an AMI id. `WebServerParameterResolver.ResolveAmi` always returns `Configurations.AMIs.LatestAMI` when that id is a key in `CertifiedAMIs`.

That shape is correct for today’s failover and `refresh_server.py` clients, and **this specification does not change it**.

The scheduled campaign path needs an operator-supplied AMI on the replacement instance without relying on `LatestAMI`. That is why a separate Refresh Server API exists. Failover is not wrong; it is a different contract.

`refresh_server.py` remains a supported client of failover. The ECS task is an additional client of the new Refresh Server API. The task does not keep the script’s interactive behaviours:

- hardcoding the base URL, AWS profile, and server ids (the task uses environment binding and loads server ids from the campaign registry)
- clearing the terminal and asking `Retry failed servers? (y/n)`
- treating a failed GET as `FailingOverFailed` after printing the error
- finishing the poll loop on the same pass that a server becomes terminal and then skipping that server's health check
- sending `enableHyperThreading`, which the replacement-create request does not apply

---

## 4. AMI verification prerequisite

AMI verification is completed before a rollout batch is started, following the [AMI verification runbook](./server-refresh-ami-verification-runbook.md). Verification uses a single Development server and the Refresh Server API to exercise the candidate AMI and refresh lifecycle. It is an operator procedure, not a batch-task mode or an automatic certification write.

Verification is successful only after the runbook checklist passes for the exact AMI id intended for rollout. The evidence includes the Development server id, refresh message id, observed replacement EC2 image id, health-check outcome, lifecycle outcome, and operator verification record. A successful verification does not start a rollout or update `LatestAMI` or `CertifiedAMIs`.

The scheduled workflow does not perform verification or retrieve external verification results. It checks the required campaign-level `amiVerified` acknowledgement before starting ECS work. This is a reminder and operator assertion, not proof that the runbook was completed.

---

## 5. Target flow

```text
Operator verifies candidate AMI in Development using the runbook
        │
        ▼
Operator creates a campaign with one verified AMI and one or more batch groups
        │
        ▼
EventBridge Scheduler starts a Step Functions Standard execution per group occurrence
        │
        ▼
Workflow validates campaign acknowledgement and starts bounded ECS batch tasks
        │
        ▼
Each batch task processes its server ids sequentially:
        Refresh Server API (server id + AMI id)
                │
                ▼
        Poll server status
                │
                ▼
        Deleted ──► health checks ──► lifecycle events ──► server result
                │
        failed, and a retry remains ──► Refresh Server API again immediately
                │
        failed, no retries remain ──► record server failure and continue
        │
        ▼
Batch and campaign execution summaries
Notifier (Slack implementation of IBatchSummaryPublisher)
CloudWatch log group ──subscription──► Sumo
```

Each batch processes its server ids in configured order, finishes or records each server's outcome before moving to the next, and continues after per-server failures. The workflow starts only the configured bounded number of batches at once. See section 7 for duplicate-server behavior across groups.

Parallel and unchanged: operators may still use `refresh_server.py` or call failover with `isRefreshServer=true` for refreshes that use platform `LatestAMI`.

---

## 6. Refresh Server API

### 6.1 Command

`POST /api/hosting/v1/webserver/{webServerId}/refresh`

```json
{ "amiId": "ami-0123456789abcdef0" }
```

The AMI id is required. The pattern is `^ami-[0-9a-f]{8,17}$`. Any other value is HTTP 400. The API does not trim, rewrite, or substitute a configured AMI.

The API calls EC2 `DescribeImages` in `ap-southeast-2`. The image must exist and be `available`. A missing, pending, failed, or deregistered image is HTTP 400. This check does not consult `LatestAMI` or `CertifiedAMIs`.

### 6.2 Status rules

These match the guards already on failover refresh starts, applied to the refresh command.

| Current status | Response |
|---|---
| Unknown id | 404 |
| `Created` or `FailingOverFailed` | 202, status set to `FailingOver`, one message enqueued |
| `FailingOver`, `Deleted`, or `Deleting` | 202 with the current record, no new message |
| Any other status | 400 |

The 202 body contains the server id, status, and queue message id.

### 6.3 Message

Add `MessageType.RefreshWebServer` and `RefreshServerMessage`:

| Field | Value |
|---|---|
| `WebServerEntity` | Server at accept time, status `FailingOver` |
| `AmiId` | The validated id from the request |

The message has no `IsRefreshServer` field and no hyper-threading field. Hyper-threading for the new instance stays on the tier configuration inside create, which is what the platform already does.

A new `RefreshServerConsumerService` runs the refresh branch: select eligible sites, reuse `Copy-{name}-{id}` only when it is already running the requested AMI, otherwise remove a stale same-named copy and create a replacement on the requested AMI, then install, settle, cut over, delete, and rename. It records `StartToHandleRefreshServer` when handling begins. It does not call the emergency per-site migration path.

The AMI must be carried through the complete replacement path without being resolved again:

| Step | AMI handling |
|---|---|
| `WebServerController.Refresh` | Reads the required `amiId` from the request and validates it before enqueueing work. |
| `RefreshServerMessage` | Stores the validated value in `AmiId`; the message is sent as `MessageType.RefreshWebServer`. |
| `RefreshServerConsumerService` | Reads `AmiId` from the message and supplies it to the refresh state machine. |
| `WebServerCreatingState` | When it creates a replacement, includes that same `AmiId` in the internal replacement-create operation. |
| Replacement instance creation | `WebServerConsumerService` uses the propagated value as `RunInstancesRequest.ImageId`. |

The refresh replacement must not call `ResolveAmi` or substitute `Configurations.AMIs.LatestAMI` or `CertifiedAMIs`. This is distinct from the existing failover refresh branch, which continues to resolve its AMI from configuration.

Matching the copy's name is not sufficient to reuse it. The consumer must establish the AMI id of an existing `Copy-{name}-{id}` and compare it with the request's `AmiId`. If they match, reuse it. If they differ, delete the stale copy before creating a replacement with the requested AMI. If the existing copy's AMI cannot be determined, do not reuse it; fail the refresh explicitly rather than risk cutting over to an instance on the wrong image. The requested AMI therefore determines the image of every replacement used by this refresh.

Ordinary `POST /api/hosting/v1/webserver` keeps today's behaviour: no caller-supplied AMI, `ResolveAmi` only. A create request that includes `amiId` is HTTP 400. The refresh worker creates the replacement through an internal refresh-replacement operation that requires `amiId`. That operation is the only create path that accepts a caller-supplied AMI.

### 6.4 Failover endpoint (unchanged)

`PUT /api/hosting/v1/webserver/failover` is **not modified** by this specification.

- Emergency failover continues as today.
- Refresh via `isRefreshServer=true` continues as today (AMI from `ResolveAmi` / `LatestAMI`).
- `refresh_server.py` and other existing clients continue to work against this endpoint.

The ECS batch task does not call failover. It calls only the Refresh Server API in section 6.1.

---

## 7. Campaign registry and scheduling

DynamoDB is the authoritative registry for rollout configuration and progress. A campaign represents one rollout of one verified AMI in one environment. Multiple named batch groups can belong to the campaign and have independent schedules, so operators may add a later group without first listing every server in the campaign.

| Record | Required fields and purpose |
|---|---|
| Campaign | `campaignId`, `environment`, immutable `amiId`, `amiVerified`, verification evidence reference, enabled state, and timestamps. Verification is required before any group starts. Changing the AMI creates a new campaign and requires verification again. |
| Batch group | `campaignId`, `groupId`, enabled state, schedule expression, IANA time zone, batch definitions, configurable `maxServersPerBatch`, retry limit, polling interval, server timeout, and maximum concurrent batch tasks. |
| Batch definition | `batchId`, ordered `serverIds`; each batch has a positive number of distinct server IDs no greater than its group's `maxServersPerBatch`. Server IDs may repeat in different groups of the same campaign. |
| Campaign/server progress | Keyed by `campaignId` and `serverId`; stores AMI, status, attempt count, current execution and lease, latest message id, timestamps, and last result. |
| Execution record | Unique scheduled occurrence id, campaign/group ids, start/end times, per-batch summaries, and infrastructure error details. |
| Environment/server lock | Conditional lease keyed by environment and server id, with campaign, group, execution owner, and expiry. Prevents concurrent refreshes across all scheduled campaigns in that environment. |

The implementation may use one DynamoDB table with partition/sort keys and conditional writes. Campaign and group definitions remain while configured, including while paused. Archive or delete a campaign explicitly; do not expire successful per-server records while later groups may still run. Retain execution records for an agreed audit period (90 days is the initial target). Locks are renewed while work is active and released at terminal outcomes. DynamoDB TTL is eventual cleanup only; an expired lease must be checked against owner liveness before takeover.

`maxServersPerBatch` is an operator-configurable group setting. It controls the number of server IDs processed sequentially by one ECS batch task; it is separate from maximum concurrent batch tasks, which controls how many batches the workflow runs at once. Validate each batch against this setting and against operational guardrails for task duration and available capacity. This specification does not prescribe a fixed numeric maximum; guardrails must be established from measured task behavior and deployment constraints.

### 7.1 Schedule and launch

Each enabled batch group has an EventBridge Scheduler schedule. Use cron for chosen wall-clock times or rate for fixed intervals; configure an explicit IANA time zone and define daylight-saving behavior. Scheduler starts a Step Functions Standard execution with `campaignId`, `groupId`, and a unique scheduled occurrence id. Standard Workflows orchestrate long-running executions without keeping a Lambda alive.

The workflow validates that campaign/group are enabled, environments agree, the campaign's `amiVerified` is true, and the occurrence is not already being processed. It then starts ECS batch tasks with a configured concurrency bound. Each ECS task receives only campaign/group/batch/execution identifiers and loads AMI, server list, and policy from DynamoDB; it does not receive an arbitrary AMI or server list in container overrides.

Operators verify the AMI first, create a campaign with that AMI and `amiVerified: true`, add one or more batch groups with their server lists and schedules, then enable the schedules. A later group can be added to the same campaign and uses shared server progress. If `amiVerified` is false, the workflow emits a reminder and starts no ECS work. Campaign/group edits must use approved administration tooling and conditional writes.

### 7.2 Duplicate-server and retry behavior

Repeated server IDs across batch groups in the same campaign are allowed and deduplicated by campaign/server progress:

| State when a group reaches a server | Behavior |
|---|---|
| `succeeded` for this campaign and AMI | Do not refresh again. Record `already_updated` in this batch and count it complete. |
| `in_progress` | Do not start another refresh. Observe the existing execution up to the normal server timeout and report its terminal result. If still running at timeout, record `still_in_progress` and leave its owner's lease intact. |
| `failed` and safely refreshable | Retry immediately under this group's retry limit. |
| `failed` after source reached `Deleted` | Report the terminal failure; do not call refresh again. |
| No campaign/server progress | Atomically claim the environment/server lease, then start the refresh. |

Acquire the global environment/server lease with a conditional DynamoDB write before any refresh call. Renew it while processing and release it at terminal outcome. Another group encountering an active lease observes that owner rather than issuing a duplicate refresh. This protects scheduled jobs; manual failover/refresh paths rely on API status guards unless separately integrated with the lock.

Retry immediately after a transport failure, HTTP 5xx, or `FailingOverFailed`, up to `retryLimit` additional attempts. A server reaching `Deleted` but failing health or lifecycle checks is recorded failed and is not retried in that execution because its source is deleted. Later occurrences report its previous failure and retry only if another refresh can safely start.

### 7.3 Execution state, batch completion, and outcomes

Each ECS task processes every server in its list, including after individual refresh failures. It writes one `ServerResult` per server and one `BatchSummary`; summaries and progress are stored in DynamoDB and logs go to stdout, CloudWatch, Sumo, and the notifier.

Per-server result values are `succeeded`, `failed`, `already_updated`, or `still_in_progress`, with attempts, final status, message id, and failure reason. A batch task exits successfully after processing every server to a recorded outcome; server refresh failures are business results and do not fail the task or Step Functions execution. Infrastructure/orchestration failures that prevent reliable completion fail the task/workflow and are alerted separately. Later schedules observe in-progress work and retry only prior failures that remain safely refreshable.

Before refresh work begins, the workflow conditionally creates its execution record. For each server, the ECS task conditionally claims the environment/server lease before calling the API. Duplicate scheduled occurrence ids do not start a second execution.

The group execution is `completed` when every server in its configured batches has a recorded outcome, including per-server failures. Infrastructure failures are recorded separately and may leave an execution requiring recovery; the next scheduled occurrence reconciles registry state before retrying work.

`amiVerified` is stored on the campaign and must be true before any scheduled group can start. If false, the workflow emits a clear reminder using the runbook and starts no ECS work. The operator sets it to true only after completing the runbook for the campaign's exact `amiId`. Changing the AMI requires a new campaign and a new verification. This is an operator acknowledgement, not independent proof that verification passed.

The batch and campaign execution summaries in DynamoDB are the checkable results. JSON logs are also emitted to stdout for CloudWatch and Sumo. Results contain no credentials or token-bearing URLs. A prior `failed` result is retried only if the source server remains in a Refresh-API-accepted state; a failure after `Deleted` remains terminal.

---

## 8. ECS task

### 8.1 Runtime

| Item | Value |
|---|---|
| Language | C# |
| Target framework | `net10.0` |
| Language version | C# 14 |
| Nullable | enabled |
| Warnings | errors |
| Process | Generic host worker, one batch task, then exit |
| Logs | `ILogger` JSON formatter to stdout and stderr only |

The Primitives API and the existing background worker stay on their current `net9.0` target. This specification does not upgrade those projects. The new task is a separate project, `ServerRefresh.BatchTask`, in this repository. It talks to XWing only over HTTPS.

### 8.2 Hosting API targets

| `environment` | Base URL |
|---|---|
| `development` | `https://platform.dev.myob-edops.com` |
| `staging` | `https://platform.staging.myob-edops.com` |
| `production` | `https://platform.myob-edops.com` |

The task signs with AWS SigV4, service `execute-api`, region `ap-southeast-2`, using the task role's temporary credentials. The base URL comes from the matched environment. Campaign data cannot supply a URL.

The task role may invoke only:

- `POST /api/hosting/v1/webserver/{webServerId}/refresh`
- `GET /api/hosting/v1/webserver/{webServerId}`
- `GET /api/hosting/v1/website/health-check/{siteName}`
- `GET /api/hosting/v1/history`

It cannot call failover, create, or delete, and it cannot terminate EC2 instances. The API role performs `DescribeImages` and the worker performs the instance replacement.

### 8.3 Per-server loop

For each id:

1. Log `ServerStarted` with the attempt number. Attempt 1 is the first try.
2. `POST` the refresh command with the campaign's `amiId`.
3. Store the message id from a 202 that enqueued work.
4. Poll `GET /webserver/{id}?getFailoverInfo=true` at `pollIntervalSeconds`.
5. Log a progress line when status, migrated count, or failed count changes, and one heartbeat at least every 5 minutes while the server is still running.
6. Stop polling at `Deleted`, `FailingOverFailed`, or `serverTimeoutMinutes`.

A good result is all of the following:

- Source status is `Deleted`.
- Every site returned for that server passes `GET /website/health-check/{siteName}?isAlert=false&isSimpleCheck=false`. An empty site list passes this check.
- The lifecycle check in section 8.4 passes.

Health checks run after the server reaches `Deleted`, including when that server is the last id in the batch. Sites are checked in batches of 10, with at most 5 calls in flight. Each site result is one JSON log line. A failed health check fails the server. It does not roll the server back. The source id is `Deleted`, so the task does not send another refresh for that id.

### 8.4 Lifecycle check

After `Deleted`, the task reads history for the refresh message id returned at accept:

`GET /api/hosting/v1/history?messageId={messageId}`

Those rows use resource `RefreshWebServer`. The create event may point its reference id at the replacement server, so a query by the source server id is not the check. The message id is.

The refresh is lifecycle-valid when that message's rows contain:

| Event | When it is required |
|---|---|
| `StartToHandleRefreshServer` | Always |
| `CreateServerForRefreshingServer` | When this attempt created a replacement, including when it first deleted a same-named copy because that copy used a different AMI. Reusing a copy already running the requested AMI does not need a second create event on this message id. |
| `CreateSitesOnOneServer` | When the pre-terminal poll showed at least one eligible site |
| `WaitingForCpuSettle` and `CpuSettled` | When the pre-terminal poll showed at least one active site |
| `UpdateServerStatus` | Always |
| `DeleteServer` | Always |
| `Succeed` | Always |

`FailoverFailed`, `DeleteServerFailed`, and `HardFailed` on this message id fail the lifecycle check.

The task captures the site snapshot on the first successful poll after accept. A site is eligible when its status is not a progress website status and its install is not `DeletedForFailover`, the same rule as `StatusHelper.IsProgressWebsiteStatus` in the worker. An active site is an eligible site with `isActive` true. That snapshot decides which conditional events are required. The message-id query already excludes older refreshes of the same server.

### 8.5 Retries

`retryLimit` is the number of extra attempts after the first.

| `retryLimit` | Maximum attempts |
|---|---|
| 0 | 1 |
| 1 | 2 |
| 2 | 3 |
| 3 | 4 |

The task retries only when another refresh call can start work:

- the `POST` failed because of a transport error or an HTTP 5xx, or
- the terminal status is `FailingOverFailed`

It waits until the status is terminal before the next `POST`. A status of `FailingOver` means a second `POST` would return 202 and enqueue nothing, so that is not a retry.

It does not retry when status is `Deleted` and health or lifecycle failed. Those outcomes are final for that server id because the source has been deleted and the API cannot safely start another replacement.

Each attempt is logged with `attempt` and `maxAttempts`. The task does not display interactive prompts or read from stdin. If a previous server result is `failed`, retry it immediately subject to the current group's `retryLimit`. A server that is still in progress is observed rather than refreshed again.

For a previous failure, retry only if the source server is still in a state accepted by the Refresh API and no replacement has already been cut over. A prior failure after `Deleted` is terminal: later groups report it without calling refresh. This rule also applies when a failure is first encountered in the current execution.

### 8.6 Logging

Every line is one JSON object on stdout. Fields: `timestamp`, `level`, `eventType`, `campaignId`, `groupId`, `batchId`, `executionId`, `environment`, `amiId`, `serverId`, `attempt`, `messageId`, `status`, `result`, `message`.

`executionId` uniquely identifies one scheduled occurrence. Omit empty fields rather than writing secrets. Do not log the SigV4 authorization header, session tokens, webhook URLs, or health-check URLs that carry query tokens.

Do not clear the console, write ANSI control sequences, or draw a refreshed table. CloudWatch and Sumo need an append-only stream.

### 8.7 Batch summary

When a batch finishes, the task writes one `BatchSummary` log event and stores the same object in DynamoDB. The group workflow aggregates batch summaries into a group execution summary.

```json
{
  "eventType": "BatchSummary",
  "campaignId": "weekly-2026-10",
  "groupId": "production-wave-a",
  "batchId": "batch-001",
  "executionId": "scheduled-occurrence-uuid",
  "environment": "development",
  "amiId": "ami-0123456789abcdef0",
  "startedAt": "2026-10-19T01:00:00Z",
  "finishedAt": "2026-10-19T02:10:00Z",
  "durationSeconds": 4200,
  "succeeded": 1,
  "failed": 0,
  "alreadyUpdated": 0,
  "stillInProgress": 0,
  "servers": [
    {
      "serverId": 322,
      "result": "succeeded",
      "attempts": 1,
      "finalStatus": "Deleted",
      "healthPassed": true,
      "lifecyclePassed": true,
      "messageId": "queue-message-id",
      "failureReason": null
    }
  ]
}
```

`result` is `succeeded`, `failed`, `already_updated`, or `still_in_progress`. `failureReason` is a short stable code: `RefreshRejected`, `RefreshFailed`, `TimedOut`, `HealthCheckFailed`, `LifecycleInvalid`, `TransportError`, `LockConflict`.

Exit codes:

| Code | Who sets it | Meaning |
|---|---|---|
| 0 | Task | Batch finished and every server has a recorded outcome. Individual server failures are reported in the summary and do not fail the task |
| 1 | Runtime / OS | Unexpected failure — crash, unhandled exception, or process kill. The task does not assign this code for a normal business outcome. Reserved so intentional results do not collide with an abnormal exit |
| 2 | Task/workflow | Infrastructure or orchestration failure prevented reliable completion; individual server refresh failures do not use this code |
| 3 | Workflow | Campaign is not verified/enabled or registry/configuration validation failed; no ECS task is started |

### 8.8 Notifier

```csharp
public interface IBatchSummaryPublisher
{
    Task PublishAsync(BatchSummary summary, CancellationToken cancellationToken);
}
```

`SlackBatchSummaryPublisher` is the first implementation. It posts the summary text to the refresh channel. The webhook URL is read from AWS Secrets Manager at startup. The secret name is task configuration, not a field in campaign data.

A publisher failure is logged and retried with bounded backoff, at most 3 times. The server results and the exit code do not change because Slack was unavailable. The stdout `BatchSummary` line and DynamoDB result record are already written.

Replacing Slack means a new implementation of `IBatchSummaryPublisher` and a different secret. Callers of `PublishAsync` stay on `BatchSummary`.

---

## 9. CloudWatch and Sumo

The task definition uses the `awslogs` driver and no other log driver.

| Environment | Log group |
|---|---|
| Development | `/xwing/server-refresh/development` |
| Staging | `/xwing/server-refresh/staging` |
| Production | `/xwing/server-refresh/production` |

Retention is 90 days. A subscription filter on each group delivers the stream to Sumo. Sumo searches use `eventType = BatchSummary` for the batch result and `eventType = ServerResult` for each server. The task writes a `ServerResult` line for every id when that id's loop finishes, with the same server object that later appears in `BatchSummary.servers`.

Operators check a run in three places: the CloudWatch log stream, the Sumo search for `batchId`, and the DynamoDB execution and campaign/server result records.

---

## 10. Requirements

| ID | Requirement |
|---|---|
| FR-API-01 | `POST /webserver/{webServerId}/refresh` shall accept a body `amiId` and shall enqueue `RefreshWebServer` only for status `Created` or `FailingOverFailed` |
| FR-API-02 | `amiId` shall be propagated from the Refresh request through `RefreshServerMessage`, `RefreshServerConsumerService`, and `WebServerCreatingState` to `WebServerConsumerService`, which shall use it as `RunInstancesRequest.ImageId` for every replacement it creates. An existing same-named copy may be reused only if its AMI id matches the requested id; if it differs, the stale copy shall be deleted and a replacement created with the requested AMI. If the existing copy's AMI cannot be determined, the consumer shall fail rather than reuse it. The refresh path shall not read `LatestAMI` or `CertifiedAMIs` |
| FR-API-03 | An `amiId` that fails the pattern or `DescribeImages` availability check shall be HTTP 400 and shall enqueue nothing |
| FR-API-04 | Ordinary server create shall keep resolving AMI from configuration. A caller-supplied `amiId` on that route shall be HTTP 400 |
| FR-API-05 | The failover endpoint shall remain unchanged. Refresh via `isRefreshServer` and emergency failover shall continue to work as today |
| FR-API-06 | Refresh shall record `StartToHandleRefreshServer` and the existing refresh lifecycle events |
| FR-BATCH-01 | An enabled campaign shall use one verified AMI in one environment and may contain multiple named, independently scheduled batch groups |
| FR-BATCH-02 | Operators shall configure `maxServersPerBatch` for each batch group. Each batch shall contain a positive number of distinct server IDs no greater than that configured value. The value shall be validated against operational task-duration and capacity guardrails; this specification does not impose a fixed numeric maximum. Repeated IDs across groups in one campaign shall use shared campaign/server progress and shall not cause duplicate refreshes |
| FR-BATCH-10 | Operators shall verify an AMI in Development using the [AMI verification runbook](./server-refresh-ami-verification-runbook.md) before enabling a campaign |
| FR-BATCH-11 | The workflow shall require campaign-level `amiVerified: true`; otherwise it shall emit a verification reminder and start no ECS work |
| FR-BATCH-12 | The ECS task shall remain non-interactive and shall not read stdin |
| FR-BATCH-03 | The workflow shall use Step Functions Standard to start ECS tasks with a configured maximum concurrency. Each task shall process its server IDs sequentially |
| FR-BATCH-04 | The task shall retry eligible transport failures, HTTP 5xx responses, or `FailingOverFailed` immediately, up to `retryLimit` additional attempts; prior failures shall be retried only when the source remains safely refreshable and under the current group's limit |
| FR-BATCH-05 | A good server result shall be `Deleted`, all post-cutover health checks passed, and the lifecycle check passed |
| FR-BATCH-06 | The task shall write JSON logs to stdout, a `ServerResult` line per server, and one `BatchSummary` line |
| FR-BATCH-07 | The task shall store per-server progress and batch summaries in the DynamoDB campaign registry |
| FR-BATCH-08 | The task shall publish `BatchSummary` through `IBatchSummaryPublisher`. The Slack implementation shall be replaceable without changing the summary schema |
| FR-BATCH-09 | A server already succeeded for the campaign AMI shall be reported `already_updated`; a server in progress shall be observed up to normal timeout; no second refresh shall start while a valid lease exists |
| FR-BATCH-13 | Per-server refresh failures shall be reported in batch results and shall not fail the ECS batch task or Step Functions execution if all servers have recorded outcomes |
| FR-BATCH-14 | EventBridge scheduled occurrences shall be idempotent. DynamoDB conditional leases shall prevent concurrent scheduled refreshes of the same environment/server |
| NFR-01 | The task shall target `net10.0` and C# 14. The API change shall stay on the API's current target framework |
| NFR-02 | Hosting calls shall use SigV4 (`execute-api`, `ap-southeast-2`) and the task role's temporary credentials |
| NFR-03 | ECS and workflow roles shall have least-privilege access to the configured DynamoDB registry items, the four hosting routes in section 8.2, and required ECS/Step Functions operations; EventBridge may start only the designated state machine |
| NFR-04 | Logs, registry records, and notifier payloads shall exclude credentials, webhook URLs, and token-bearing URLs |
| NFR-05 | Invalid configuration, including a batch exceeding `maxServersPerBatch` or its operational guardrails, shall fail closed before refresh. A publisher failure shall not hide server results or fail an otherwise completed execution |
| NFR-06 | The refresh log group shall be encrypted and subscribed to Sumo |
| NFR-07 | Registry campaign configuration shall be environment-scoped and conditional updates shall prevent lost updates or double claims. Lock leases shall be renewed while active and recoverable after owner failure |

---

## 11. Acceptance

1. A failover request with `isRefreshServer=true` still refreshes a server using platform `LatestAMI` / `ResolveAmi`, unchanged from today.
2. A refresh request with a valid available AMI returns 202. The refresh reuses an existing `Copy-{name}-{id}` only when its AMI matches the requested AMI. If the existing copy uses a different AMI, the stale copy is deleted and a replacement is created with the requested AMI, including when configuration `LatestAMI` is a different id. If the existing copy's AMI cannot be determined, the refresh fails without reusing it.
3. A refresh request with a malformed or unavailable AMI returns 400 and does not create an instance.
4. An operator can complete the separate AMI verification procedure and checklist in the [AMI verification runbook](./server-refresh-ami-verification-runbook.md) before starting a rollout.
5. A campaign with `amiVerified` false emits a reminder and starts no ECS work; true permits normal schedule validation and start.
6. Within one campaign, a repeated successful server is reported `already_updated`, an in-progress server is observed without a second refresh, and a prior failure is retried immediately only when the source remains safely refreshable and within the current group's configured retry limit.
7. A `Deleted` server with a failed health check is `failed` and is not sent to the refresh API again.
8. The process never reads stdin. A full run's stdout is JSON only.
9. A batch task records outcomes for every server and exits successfully when every server has an outcome, even if some server refreshes failed. Infrastructure failures fail the workflow and alert separately.
10. An operator can change a group's `maxServersPerBatch`; valid batches at or below the configured value are accepted, and batches exceeding it or the established operational guardrails are rejected before any refresh call.
11. `refresh_server.py` continues to call failover and is not retired or broken by this change.

---

## 12. Out of scope

- Choosing or copying the AMI. Campaign configuration supplies an AMI that already exists in `ap-southeast-2`.
- Changing `LatestAMI` for ordinary server create or for failover-based refresh.
- Changing the failover API or retiring `refresh_server.py`.
- A custom campaign-management UI. Campaign/group administration uses approved AWS administration tooling.
- Automated AMI verification or verification-result lookup by the rollout task.
- Customer notification.
- Running the refresh state machine inside the batch task. The task is a client. The existing worker performs the replacement.

---

## 13. Security notes

**Assumptions:** internal operators and internal task roles only. EntraID is unchanged for any human use of the hosting API. The batch task uses its IAM role. Batch contents are General (AMBER). Failover and `refresh_server.py` retain their existing auth and contracts.

**What this design requires**

- Validate campaign AMI, environment, schedule, configured `maxServersPerBatch`, batch sizes, server IDs, retry bounds, and concurrency against established operational guardrails. Reject invalid configuration rather than substituting defaults.
- Keep caller-supplied AMI ids off the public create route. Only the refresh-replacement operation accepts one, and only after `DescribeImages` succeeds.
- Leave the failover endpoint unchanged so existing refresh and emergency clients keep working.
- Give EventBridge permission to start only the designated state machine. Scope workflow/ECS roles to the required state machine, task definition, campaign registry records, and the four hosting routes. No instance terminate, unrestricted registry access, or long-lived access keys.
- Select the hosting base URL from the ECS task's environment configuration. Never read a URL from campaign data.
- Store the Slack webhook in Secrets Manager. Do not put it in campaign data, the image, or a log line.
- Encrypt the log group. Ship it to Sumo through a subscription filter. The summary schema stays stable so a later notifier can replace Slack without a second logging path.

**Still to configure in the environment:** EventBridge schedules and invocation roles, Step Functions Standard state machines, campaign registry tables and indexes, ECS task definitions per environment (`TARGET_ENVIRONMENT`, base URL, and secret name), IAM roles and API Gateway authorization for the four routes, lock recovery/alerting, the three log groups and Sumo subscriptions, and the Slack webhook secret.
