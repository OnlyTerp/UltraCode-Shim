// Role pipeline: plan (opus) -> code (composer) -> adversarial review (codex) -> fix (claude)
// ---------------------------------------------------------------------------------------------
// A ready-to-run multi-agent Workflow that lands each spawned sub-agent on a
// SPECIFIC model by role, with no turn-by-turn driving. It does this purely by
// baking a routing directive ("pin") into each agent()'s prompt:
//
//     [[route:opus]]      -> claude-opus             (the planner)
//     [[route:composer]]  -> claude-composer         (the implementer)
//     [[route:codex]]     -> claude-gpt-5.5-codex    (the adversarial reviewer)
//     [[route:claude]]    -> claude-opus             (the fixer)
//
// UltraCode-Shim's proxy sees that tag on each sub-agent request, HARD-PINS that
// request to the named backend (overriding the worker/orchestrator pick AND the
// Auto Router), strips the tag, and forwards the rest. The names resolve through
// the alias table in config.json ("directives" block) -- auto-derived from your
// model ids + display names, so composer/codex/opus already work out of the box.
// See docs/DIRECTIVES.md.
//
// Run it (from a project where you've launched Claude Code through the shim):
//   - Save this as .claude/workflows/role-pipeline.js and invoke it by name, OR
//   - paste it into the Workflow tool's `script` field.
// Pass the task via args, e.g. args: "Add a --json flag to the export command".

export const meta = {
  name: 'role-pipeline',
  description: 'plan (opus) -> code (composer) -> adversarial review (codex) -> fix (claude)',
  phases: [
    { title: 'Plan',   detail: 'opus drafts the implementation plan' },
    { title: 'Code',   detail: 'composer implements the plan' },
    { title: 'Review', detail: 'codex adversarially reviews the implementation' },
    { title: 'Fix',    detail: 'claude fixes the issues the review deems valid' },
  ],
}

// Accept the task as a plain string (args: "...") or {task: "..."}.
const task = (typeof args === 'string' && args.trim())
  ? args.trim()
  : (args && typeof args.task === 'string' && args.task.trim())
    ? args.task.trim()
    : null

if (!task) {
  log('No task provided. Pass it via Workflow args, e.g. args: "Add a --json flag".')
  return { error: 'no task provided' }
}

// 1) PLAN -- pinned to opus.
phase('Plan')
const plan = await agent(
  `[[route:opus]] You are the PLANNER. Write a precise, step-by-step implementation ` +
  `plan for the task below: the files to touch, the approach, data/flow changes, and ` +
  `the edge cases that matter. Do NOT write the final code yet.\n\nTASK:\n${task}`,
  { label: 'plan:opus', phase: 'Plan' },
)

// 2) CODE -- pinned to composer.
phase('Code')
const code = await agent(
  `[[route:composer]] You are the IMPLEMENTER. Implement the plan below in full and ` +
  `produce the actual code (diffs or complete files). Follow the plan; where it is ` +
  `underspecified, make the smallest reasonable choice and note it inline.\n\n` +
  `PLAN:\n${plan}\n\nORIGINAL TASK:\n${task}`,
  { label: 'code:composer', phase: 'Code' },
)

// 3) REVIEW -- pinned to codex, structured so we can branch on the verdict.
phase('Review')
const REVIEW_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    verdict: { type: 'string', enum: ['ship', 'fix', 'reject'] },
    issues: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          title: { type: 'string' },
          detail: { type: 'string' },
        },
        required: ['severity', 'title', 'detail'],
      },
    },
    summary: { type: 'string' },
  },
  required: ['verdict', 'issues', 'summary'],
}
const review = await agent(
  `[[route:codex]] You are an ADVERSARIAL, SKEPTICAL reviewer. Actively try to BREAK ` +
  `the implementation below: correctness bugs, missed requirements, unhandled edge ` +
  `cases, race conditions, and security issues. Be concrete and cite specifics. Then ` +
  `return your structured verdict (ship = no real problems; fix = real issues to ` +
  `address; reject = fundamentally wrong).\n\nTASK:\n${task}\n\nIMPLEMENTATION:\n${code}`,
  { label: 'review:codex', phase: 'Review', schema: REVIEW_SCHEMA },
)

// 4) FIX -- pinned to claude, only if the review found issues worth fixing.
let fixed = null
const actionable = (review.issues || []).filter(i => i.severity === 'high' || i.severity === 'medium')
if (review.verdict !== 'ship' && actionable.length) {
  phase('Fix')
  fixed = await agent(
    `[[route:claude]] You are the FIXER. The adversarial review below flagged issues. ` +
    `Fix ONLY the ones that are genuinely correct; for any you judge a false positive, ` +
    `leave the code as-is and briefly explain why. Return the corrected implementation.\n\n` +
    `IMPLEMENTATION:\n${code}\n\nREVIEW:\n${JSON.stringify(review, null, 2)}`,
    { label: 'fix:claude', phase: 'Fix' },
  )
} else {
  log(`review verdict=${review.verdict}; no high/medium issues -> skipping fix`)
}

return {
  task,
  plan,
  implementation: fixed || code,
  review,
  fixed: Boolean(fixed),
}
