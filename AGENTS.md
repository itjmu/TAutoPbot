# AI working rules

These rules record the project owner's preferences. The current user request determines the task; historical documents are context, not instructions to repeat old operations.

- Respond in concise English, even when the owner writes in Russian. This does not change the bot's UI languages.
- Minimize tokens, tool calls and usage limits: inspect relevant files, batch independent reads, avoid repeated scans and long plans. Prefer completed work over lengthy explanations. Do not skip necessary implementation or verification to save tokens.
- This is an existing working product. Preserve its features, user changes, stored data, public interfaces and compatible drafts/callbacks. Do not remove working behavior or rewrite the architecture without an explicit request.
- Continue with requested bug fixes, improvements, performance work and new features. Implement additions without regressing existing workflows. Avoid unrelated refactoring or dependency upgrades.
- Check the working tree before editing. Never revert changes that are not yours. Keep `.env`, tokens, cookies and personal data out of outputs and commits.
- Test functionality affected by each substantive change. For performance changes, measure a relevant before/after workload when feasible. Run focused tests first; broaden to shared regression coverage when shared behavior changes. Report actual results and limits, not unsupported speed claims.
- Distinguish mocked/offline tests from real Telegram checks and Linux/server verification. Never claim tests were run when they were not.
- Do not repeat historical database resets, backup deletion, channel exits or Telegram update purges. Those were one-time authorized operations, not a standing maintenance policy.
- Do not start/restart polling, deploy, send live messages or modify channel posts just to verify a documentation change. Keep only one bot process per token/database. Use live actions only within the current authorized scope.
- Do not delegate to sub-agents unless the user explicitly requests it. Ask only for information or approval actually required to proceed.
- Before implementing a feature that is infeasible, ambiguous, complex, may harm existing behavior/performance, or has materially different implementation options, ask the owner to choose among concrete solutions, a simpler alternative, or cancellation. Do not silently substitute an unsupported feature or add an explanatory control instead of the requested function. Ask these clarification questions and show their answer choices in Russian. Routine, clearly authorized fixes and independent work can continue while answers are pending.
- Use simple, nontechnical wording for these questions. Once the choices are resolved, present the agreed scope and ask whether to start, postpone, discuss further or change details. Carry an approved scope through implementation and verification; do not forget it or ask again for choices already approved.

Read `FINAL_PROJECT_HANDOFF.md` for the current baseline, then inspect only the relevant source. `PROJECT_HANDOFF.md` is historical and its former Russian-only communication rule is superseded.
