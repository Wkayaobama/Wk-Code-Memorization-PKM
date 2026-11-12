export const meta = {
  name: 'recurser-privacy-audit',
  description: 'Read generated project maps or card decks and list personal data, credentials and account identifiers that must not be published',
  phases: [{ title: 'Audit', detail: 'one read-only auditor per batch of files' }],
}

// args: { kind: 'map' | 'cards', dir: '<folder holding <slug>.json>', batches: [[slug, ...], ...],
//         visibility: { slug: 'public' | 'private' }, owner: '<github handle>', known: ['strings already in scrub.json'] }
const FINDINGS = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          slug: { type: 'string' },
          needle: { type: 'string', description: 'the exact substring as it appears in the file, as short as possible while still unique enough to replace safely' },
          category: { type: 'string', enum: ['person_name', 'email', 'phone', 'postal_address', 'customer_record', 'credential', 'account_identifier', 'private_url_or_host', 'other'] },
          field: { type: 'string', description: 'the JSON key that holds it, e.g. purpose, detail, answer, evidence_quote' },
          context: { type: 'string', description: 'about 100 characters around the needle, copied from the file' },
          suggestion: { type: 'string', description: 'neutral replacement text, e.g. "a colleague", "[id]", "a customer"' },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
          why: { type: 'string' },
        },
        required: ['slug', 'needle', 'category', 'field', 'context', 'suggestion', 'confidence'],
      },
    },
    clean: { type: 'array', items: { type: 'string' }, description: 'slugs in which nothing was found' },
    notes: { type: 'string' },
  },
  required: ['findings', 'clean'],
}

const batches = args.batches
const results = await parallel(batches.map((batch, i) => () => agent(
  `You are a privacy auditor. Read-only task: do not modify any file, do not run code from any repository.

Read these generated JSON files completely (they are ${args.kind === 'map' ? 'knowledge maps of software projects' : 'flashcard decks about software projects'} written by another agent from the owner's repositories):
${batch.map(s => `- ${args.dir}/${s}.json   (slug "${s}", repository visibility: ${args.visibility[s] || 'private'})`).join('\n')}

They are about to be published: files of PUBLIC repositories go into a public GitHub repository, files of PRIVATE repositories go to a private repository and a private web page. List every string that should not travel with them:

1. person_name: the name of a private individual (colleague, customer contact, the owner's own real name, a username that identifies a person). NOT a finding: the owner's GitHub handle${args.owner ? ' "' + args.owner + '"' : ''}, authors of public libraries or books, public figures, product or company names, fictional example names such as Jane Doe / Alice / Bob.
2. email, phone, postal_address of real people or organisations.
3. customer_record: a real customer, prospect, deal, ticket or contact row quoted from CRM data (company names of the owner's employer or of the systems being integrated, such as the product a pipeline is named after, are NOT findings; an individual customer of that business IS).
4. credential: token, API key, password, secret, connection string with credentials, signed URL, webhook URL carrying a secret. Environment variable NAMES are not findings; values are.
5. account_identifier: portal id, tenant id, subscription id, cloud project number or id, app id, form id, object or record id, pipeline or stage id, spreadsheet or Drive file id, HubDB table id, account or organisation numeric id. Plain constants are NOT findings (batch sizes, time spans in milliseconds, port numbers, version numbers, test fixture arithmetic, dates, epoch timestamps).
6. private_url_or_host: internal hostname, private IP address, intranet or share link, a URL of a private dashboard or document.
7. other: anything else you would not want copied out of a private codebase.

Rules:
- Report the needle exactly as written in the file so it can be found with a plain string search. Keep it minimal (the identifier or the name itself, not the sentence).
- Report each distinct needle once per slug, even when it occurs many times.
- Give the JSON key that holds it in "field"; say so in "why" when it sits inside quoted source code (keys evidence_quote, code_snippet, expected_output, input_example).
- When you are unsure whether a name is a real person, report it with confidence "low" and explain.
- A slug with nothing to report goes in "clean".
- Already handled, do not report again: ${JSON.stringify(args.known || [])}.

Return only the structured result.`,
  { label: `audit:${args.kind}:${i + 1}`, phase: 'Audit', schema: FINDINGS, effort: 'high' })))

const ok = results.filter(Boolean)
return {
  kind: args.kind,
  batchesRun: ok.length,
  batchesFailed: results.length - ok.length,
  findings: ok.flatMap(r => r.findings),
  clean: ok.flatMap(r => r.clean),
  notes: ok.map(r => r.notes).filter(Boolean),
}
