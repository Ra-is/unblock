# Unblock — first public release

An invoice-evidence agent built with Strands Agents SDK, Amazon Bedrock and AWS serverless services.

The recorded workflow uses the actual deployed application, synthetic business documents and controlled mailboxes: an incorrect receipt is rejected, a genuine correction request arrives in the supplier inbox, an unexpected sender is held, an authenticated corrected reply wakes the worker, and a reviewer accepts the packet. The mailbox viewer is explicitly labeled as a recording/test harness.

Included release assets:

- `unblock-demo.mp4`: narrated recording of the working deployment.
- `unblock-demo.vtt`: narration captions.

Security changes include removal of nested reply-routing capabilities from JSON responses and exports, a sample-case guard at the API/inbox/worker/mail layers, and fail-closed email authentication. The public walkthrough is clearly labeled simulated and never sends mail or runs models.

Validation: 60 automated tests, passing GitHub CI, and successful live SES/Bedrock integration tests. No real invoices were paid and no unrelated recipients were contacted.

This is a pilot release. It is not a completed production audit, an authenticity guarantee, or a submitted Devpost entry. The AWS Builder article in the repository is a draft for the participant to publish under their own identity.
