// Called by agent-review.yml via actions/github-script
// Env: ANTHROPIC_API_KEY, PR_DIFF, PR_TITLE, PR_BODY
// Context: github, context, core (injected by actions/github-script)

module.exports = async ({ github, context, core }) => {
  const diff = process.env.PR_DIFF || '';
  const prTitle = process.env.PR_TITLE || '';
  const prBody = process.env.PR_BODY || '';

  const prompt = [
    'You are reviewing a pull request for a Manifold Markets paper trading bot. Be concise and direct.',
    '',
    'PR Title: ' + prTitle,
    'PR Description: ' + prBody,
    '',
    'Diff (Python/config files only):',
    '```',
    diff || 'No Python/config changes.',
    '```',
    '',
    'Review for:',
    '1. Bugs or logic errors in trading/automation code',
    '2. Security issues (API keys, credentials, injection)',
    '3. Risk parameter changes (max_positions, min_confidence, trade_size)',
    '4. Breaking changes to state file schema or cron scripts',
    '',
    'Format your response as a brief markdown comment (3-8 bullet points max). Start with a one-line verdict: LGTM / NEEDS CHANGES / MINOR NOTES.',
  ].join('\n');

  const response = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': process.env.ANTHROPIC_API_KEY,
      'anthropic-version': '2023-06-01',
    },
    body: JSON.stringify({
      model: 'claude-sonnet-4-6',
      max_tokens: 1024,
      messages: [{ role: 'user', content: prompt }],
    }),
  });

  const data = await response.json();
  if (!response.ok) {
    core.setFailed('Anthropic API error: ' + (data.error && data.error.message));
    return;
  }

  const reviewText = data.content[0].text;
  const body = '### Agent Review\n\n' + reviewText + '\n\n---\n*Automated review by Claude. Human approval still required.*';

  await github.rest.issues.createComment({
    owner: context.repo.owner,
    repo: context.repo.repo,
    issue_number: context.issue.number,
    body,
  });
};
