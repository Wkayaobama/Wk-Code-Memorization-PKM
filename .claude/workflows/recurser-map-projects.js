export const meta = {
  name: 'recurser-map-projects',
  description: 'One analyst per repository writes a validated project knowledge map',
  phases: [{ title: 'Map', detail: 'one analyst per repository' }],
}

const W = (args && args.work) || 'work'
const slugs = args.slugs

const SCHEMA = {
  type: 'object',
  properties: {
    slug: { type: 'string' },
    ok: { type: 'boolean' },
    title: { type: 'string' },
    one_liner: { type: 'string' },
    status: { type: 'string' },
    architecture_count: { type: 'number' },
    key_function_count: { type: 'number' },
    decision_count: { type: 'number' },
    confidence: { type: 'string' },
    validator_last_line: { type: 'string' },
    notes: { type: 'string' },
  },
  required: ['slug', 'ok', 'validator_last_line'],
}

function prompt(slug) {
  return `Read the brief at docs/briefs/MAP_BRIEF.md and follow it exactly.

Your project has the slug "${slug}". Find the object with that slug in ${W}/projects.json: it gives the repository name, the checkout directory ("dir"), the analysis branch, any alternate worktrees ("alts" - other lines of work in the same repository), a size class and a "focus" note written by the orchestrator. The same file lists the owner's other recent projects: use it for the "relations" field, and only relate two projects when you find real evidence.

Write the map to ${W}/maps/${slug}.json, then validate it exactly as the brief says, passing --repo for the checkout directory and one more --repo for each alternate worktree. Iterate until the validator's last line starts with OK.

Return the structured summary: ok = true only if the validator ended with OK; validator_last_line = that last line verbatim; notes = anything the orchestrator should know (uncertainties, lines of work you could not cover, signs that the repository is mostly empty or generated).`
}

phase('Map')
const results = await pipeline(slugs, slug =>
  agent(prompt(slug), { label: `map:${slug}`, phase: 'Map', schema: SCHEMA, effort: 'high' })
)
const done = results.filter(Boolean)
log(`${done.filter(r => r.ok).length}/${slugs.length} maps validated`)
return { results: done, missing: slugs.filter((s, i) => !results[i]) }
