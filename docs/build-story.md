# Agents for Humans: Unblock follows up on missing invoice evidence

An invoice can be delayed because a small piece of paperwork does not match: a purchase order reference, a supplier name or a signed delivery receipt. Unblock is a pilot invoice-evidence workspace built around that coordination task.

The workflow begins with an invoice case and its expected details. A Strands coordinator inspects the case, calls a document specialist to extract structured claims, and uses application tools to compare those claims with the case. If a delivery receipt belongs to another order, the system leaves that requirement unresolved. The agent can request a corrected document from the contact assigned to the case.

## Waiting is part of the job

After a request is sent, the worker stops. SES receives the supplier's response, stores the raw message privately and invokes an inbound handler. Accepted evidence creates new work through DynamoDB and SQS. The browser does not have to remain open.

The model is useful for interpreting imperfect documents and explaining the correction needed. Exact reference, currency and amount checks belong in ordinary application code. So do tenant permissions, reviewer rights and the rule that the agent cannot authorize payment.

## Making the boundaries real

We separated the public example from actual operational cases. The public walkthrough uses seeded synthetic documents and explicitly labels its simulated correspondence. It cannot launch jobs or send email, and incoming email cannot mutate it. Both the API and background workers enforce the boundary.

We also removed reply-routing capabilities from JSON projections and exports. Merely masking a token in the UI would have left it readable through the API and nested request objects.

Inbound acceptance requires passing spam/virus scans, a passing aligned DMARC result, at least one passing SPF or DKIM method, and a single From address matching the case contact. A matching displayed address alone is insufficient. The conservative policy holds unknown or ambiguous results for review and may also hold legitimate monitoring-only-domain mail.

## AWS implementation

The application uses Strands Agents SDK with Amazon Bedrock, a FastAPI API on Lambda, Cognito, private S3 document storage, DynamoDB, DynamoDB Streams, SQS FIFO and SES. Conditional transactions couple case changes, audit entries and new jobs. Tools are scoped to the assigned case; model text cannot select another tenant or arbitrarily write a payment decision.

Queue delivery remains at least once. Document hashes and request fingerprints reduce repeated work, while job records and version checks preserve progress. CloudFormation describes the infrastructure so the project can be deployed again from source.

## What we demonstrate, and what remains

The live test uses synthetic business documents with real Bedrock inference and actual mail transport between project-controlled addresses. It checks request receipt, an unexpected sender being held without attaching evidence, and an authenticated corrected reply restarting the agent. This is different from the intentionally simulated public walkthrough.

The project does not claim measured customer time savings, verified document authenticity or a completed production audit. Before a broader rollout it needs customer validation, load and restore testing, operational alert routing, better scanned-document support and further review of sending/retry behavior.

The goal is concrete: turn a blocked invoice's missing evidence into a reviewable packet, with the agent handling follow-up and a human retaining the decision.

---

Publication note: this is a draft for the project owner to review and publish under their AWS Builder identity. It is not itself a published AWS Builder post.
