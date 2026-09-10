# Demo guide

## Pitch

Unblock helps small suppliers and operations teams resolve missing paperwork behind an invoice. Its Strands agent checks the evidence, requests a correction, stops while waiting, and resumes when authenticated email supplies the missing document. A person still reviews the completed packet.

## Record a real run

Target duration: 2–4 minutes. Use synthetic catering documents and mailboxes controlled by this project. The public sample is simulated and must not be presented as a live run.

1. Open an invoice case for Ama Catering Ltd: INV-2041, PO-1042, GHS 4,800.00. Upload invoice, purchase order and wrong receipt from `examples/`.
2. Start the review. Show the order mismatch on the actual source receipt. The agent sends a request via SES and enters the waiting state.
3. Show the received request in the controlled supplier mailbox. The mailbox viewer is a recording/test harness, not an additional Unblock product feature.
4. Reply from a different authenticated address. Show the held message and that no evidence was attached.
5. Reply from the actual case contact with the corrected receipt. Do not click Review evidence again: show the automatically resumed job.
6. Show all three checks satisfied, the resolved request, the activity timeline and reviewer acceptance. State explicitly that no payment was made.
7. Finish with the architecture, Strands role and honest boundaries: pilot product, bounded documents/cases, no autonomous payment, conservative email authentication.

Do not show login passwords, AWS credentials, private reply-routing addresses or tokens. Recording assets must be checked before publication. Preserve a machine-readable live verification result separately from the public sample.

## Submission checklist

- Public MIT-licensed repository, README and reproducible deployment.
- Architecture diagram in `docs/architecture.md`.
- Actual working-project video under five minutes.
- Text description: problem, audience, implementation, limits and evidence of functionality.
- AWS Builder ID from the participant.
- Optional build story published by the participant before the deadline. A draft is not a published bonus entry.
