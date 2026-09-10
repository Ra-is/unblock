# Operations

## Deployment and rollback

Run `scripts/deploy.py` with the explicit profile and region. Dependencies are locked in `uv.lock`; Lambda artifacts are content-addressed in the bootstrap bucket. CloudFormation updates reuse resources. To roll application code back, update `ArtifactKey` to a previously deployed release key using the same application template and other parameters. Do not delete the stack as a rollback strategy.

## Observe

CloudWatch log groups: `/aws/lambda/unblock-dev-api`, `-worker`, `-outbox`, and `-inbound` (14 days). The inbound function logs an error type only; it never logs addresses, subjects, bodies or attachments. Worker failures log exception type and message ID without documents, prompts or tokens. CloudWatch alarms track both worker dead letters and stream-dispatch dead letters. **No email/SNS alert destination is configured yet**; an operator must connect their chosen destination before production use.

## Failed work

1. Check case agent status and audit timeline.
2. Inspect worker error types and queue depth. Check Bedrock model access, service quotas, throttling and IAM before retrying.
3. A failed case can be queued again from the UI. Saved extractions and request fingerprints avoid repeating finished work. Older queued jobs are ignored once superseded.
4. SQS retries failed work up to the redrive limit. The worker queue preserves order within a case.
5. For dispatch failures, inspect the stream failure queue, retrieve the original job from DynamoDB, and publish that job with its original job ID and tenant/case message group. Do not replay arbitrary external JSON into the queue.
6. Inspect jobs left running after Lambda termination. The seven-minute running lease exceeds the five-minute Lambda timeout; after it expires a user may queue a replacement. Queued jobs have a fifteen-minute lease. A replacement supersedes the previous job ID, so delayed retries skip obsolete jobs. Establish the underlying failure before repeated retries.

## Mail

Only one SES receipt rule set can be active per region per account. `scripts/deploy.py` activates this stack's rule set and prints the name of any rule set it replaces; check that output if another workload receives mail in the same region. Inbound mail is accepted only for the configured subdomain, so the apex domain's existing provider is unaffected.

To stop all outbound delivery immediately, set the worker's `UNBLOCK_SEND_ENABLED` environment variable to `false`. The agent continues to record requests and marks them explicitly as not delivered; nothing is sent silently. To stop inbound, disable the receipt rule or remove the subdomain's MX record; queued evidence already attached is unaffected.

Bounces and complaints are not yet routed to a notification topic. Until they are, check the SES account dashboard's reputation metrics before increasing volume, and keep the recipient allowlist narrow.

Messages held for review accumulate on the case and are capped at 40. A case that reaches the cap rejects further inbound evidence and needs a reviewer to intervene.

## Data and backups

S3 is private, encrypted and versioned. DynamoDB point-in-time recovery is enabled. Test restoration into separate resources and verify tenant boundaries before using restored data. Restore drills are not completed by the initial deployment.

Data buckets, the table and Cognito pool are retained on stack deletion. They continue to exist and may accrue storage charges. No automated customer-data expiration has been chosen; define an explicit retention and erasure policy with pilot customers. Never use stack deletion as a data-erasure procedure.

## Costs and limits

The deployment creates pay-per-use AWS services. Bedrock runs and document storage incur charges. Worker concurrency is capped at two, model turns/tokens are bounded, and HTTP API requests are throttled. These are operational limits, not an account-wide spending cap. Configure the owner's desired AWS Budget and alert destination before widening access.

## Credentials

`.local/owner-access.json` is private local bootstrap material, not source. Rotate the password in Cognito after initial handoff. Do not paste AWS credentials, ID tokens, documents or owner passwords into logs or issues. Runtime roles are provisioned by the stack; the local profile is used only for deployment/development.
