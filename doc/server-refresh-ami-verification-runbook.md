# Server Refresh AMI Verification Runbook

**Purpose:** Verify a candidate AMI in Development before enabling a server-refresh campaign.  
**Applies to:** The operator-supplied AMI refresh API described in `server-refresh-batch-spec.md`.  
**Outcome:** A recorded pass/fail decision for one exact AMI id. Verification does not update `LatestAMI` or `CertifiedAMIs`, and it does not start a campaign.

## Verification checklist

Do not enable a campaign until every required item below passes for the exact AMI id that will be used by that campaign.

- [ ] Confirm the candidate AMI id and target region (`ap-southeast-2`).
- [ ] Confirm the candidate AMI is `available` and is the intended image.
- [ ] Select a Development web server in `Created` status. Prefer a fresh server created for this verification; confirm it is safe to replace.
- [ ] Record the candidate AMI id, Development server id, operator, and start time.
- [ ] Submit one refresh request for that server with the candidate `amiId`; record the returned queue message id.
- [ ] Wait for the source server to reach `Deleted`. If it reaches `FailingOverFailed`, times out, or otherwise fails, stop and do not mark the AMI verified.
- [ ] Identify the replacement instance created for this refresh and confirm its EC2 `ImageId` is exactly the candidate AMI id.
- [ ] Review the history for the refresh message id. Confirm required events from section 8.4 of the specification are present and that `FailoverFailed`, `DeleteServerFailed`, and `HardFailed` are absent.
- [ ] Run the post-cutover health check for every site on the server. Confirm all checks pass.
- [ ] Complete any applicable Development smoke or acceptance checks for the services hosted on the replacement.
- [ ] Save the evidence and record the result as **verified** only if every check passed.

## Procedure

### 1. Prepare the candidate and Development server

1. Confirm the AMI id is correct and the image is available in `ap-southeast-2`. The Refresh API also checks availability, but this pre-check catches a wrong or unusable candidate before starting the server replacement.
2. Choose a Development web server with status `Created`. Prefer a newly created server to avoid stale `Copy-{name}-{id}` state or previous refresh failures. Do not use a server unless replacing it is safe.
3. Record the AMI id, source server id, operator, and UTC start time in the team's normal change or release record.

### 2. Start one Development refresh

Call the Development Refresh Server API once for the selected server:

```http
POST /api/hosting/v1/webserver/{webServerId}/refresh
Content-Type: application/json

{ "amiId": "ami-0123456789abcdef0" }
```

Use the actual candidate AMI id and Development server id. The request must target the Development API. Record the accepted response's queue message id. Do not start a rollout batch as a substitute for this prerequisite.

### 3. Observe replacement and verify the image

1. Poll the Development web-server endpoint until the source server reaches `Deleted`, `FailingOverFailed`, or the operational timeout. A failed or timed-out refresh is a failed verification.
2. Use the message id to inspect refresh history, as described in section 8.4 of the specification. Confirm the lifecycle events required for this refresh and confirm no failure events are recorded.
3. Identify the replacement instance created by this refresh from the lifecycle history and hosting/EC2 records.
4. Inspect the replacement instance in EC2 and compare its `ImageId` with the candidate AMI id. The ids must match exactly. A successful status or health check alone does not prove that the intended AMI was used.

### 4. Check service health

1. After cutover, run the platform health check for every site on the refreshed server using the API and query options specified in section 8.3 of the specification.
2. Run the applicable Development smoke or acceptance checks for the services hosted on that server.
3. Record each result. Any failed health or smoke check means the AMI is not verified for rollout.

### 5. Record the decision

Keep a verification record containing at least:

| Evidence | Value to record |
|---|---|
| Candidate AMI id | Exact `ami-...` id |
| Environment and source server | `development` and source server id |
| Operator and timestamps | Who performed the check and when it started/finished |
| Refresh message id | Queue message id returned by the API |
| Replacement image | Observed EC2 `ImageId`; must equal the candidate |
| Refresh outcome | Source reached `Deleted`; lifecycle checks passed |
| Health and smoke checks | Results for all applicable checks |
| Decision | `verified` or `not verified`, with a short reason for a failure |

Only after the decision is `verified` may the operator create or enable a campaign for that same AMI. If the AMI id changes, create a new campaign and repeat this runbook. A successful verification does not automatically start a rollout or certify/promote the AMI.

## Campaign acknowledgement

Campaigns store an `amiVerified` acknowledgement in the DynamoDB registry. Set it to `true` only after completing this runbook for the campaign's exact `amiId`, and record the verification evidence reference with the campaign. If it is false, the scheduled workflow emits a reminder and starts no ECS work. This acknowledgement is an operator assertion, not independent evidence that the checks passed. A campaign's AMI is immutable; if the AMI id changes, create a new campaign and repeat verification.

## Stop conditions

Do not create or enable the campaign if any of the following is true:

- The candidate AMI is unavailable, incorrect, or differs from the AMI used by the replacement.
- The refresh does not reach `Deleted` or required lifecycle history is missing or contains a failure event.
- Any site health check or applicable smoke check fails.
- Required evidence cannot be collected or the result is uncertain.

Investigate the failure and repeat verification with a suitable Development server after the cause has been addressed.
