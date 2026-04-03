// Called by agent-review.yml via actions/github-script
// Env: ANTHROPIC_API_KEY, PR_DIFF, PR_TITLE, PR_BODY, REPO_PATH
// Context: github, context, core (injected by actions/github-script)

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

async function callClaude(messages, maxTokens = 2048) {
  const response = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': process.env.ANTHROPIC_API_KEY,
      'anthropic-version': '2023-06-01',
    },
    body: JSON.stringify({
      model: 'claude-sonnet-4-6',
      max_tokens: maxTokens,
      messages,
    }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error('Anthropic API error: ' + (data.error && data.error.message));
  return data.content[0].text;
}

module.exports = async ({ github, context, core }) => {
  const diff = process.env.PR_DIFF || '';
  const prTitle = process.env.PR_TITLE || '';
  const prBody = process.env.PR_BODY || '';
  const repoPath = process.env.REPO_PATH || '.';

  // ── Step 1: Review and get structured issues ──────────────────────────────
  const reviewPrompt = [
    'You are reviewing a pull request for a Manifold Markets paper trading bot.',
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
    'Respond with a JSON object only — no markdown, no extra text:',
    '{',
    '  "verdict": "LGTM" | "NEEDS CHANGES" | "MINOR NOTES",',
    '  "summary": "one line summary",',
    '  "issues": [',
    '    {',
    '      "file": "relative/path/to/file.py or null if not file-specific",',
    '      "description": "what is wrong",',
    '      "fix": "exact code change needed, or null if unfixable without human"',
    '    }',
    '  ]',
    '}',
  ].join('\n');

  const reviewRaw = await callClaude([{ role: 'user', content: reviewPrompt }]);

  let review;
  try {
    const match = reviewRaw.match(/\{[\s\S]*\}/);
    review = JSON.parse(match ? match[0] : reviewRaw);
  } catch (e) {
    // Fallback: post raw review as comment
    await github.rest.issues.createComment({
      owner: context.repo.owner,
      repo: context.repo.repo,
      issue_number: context.issue.number,
      body: '### Agent Review\n\n' + reviewRaw + '\n\n---\n*Automated review by Claude. Human approval still required.*',
    });
    return;
  }

  // ── Step 2: Auto-fix file-specific issues ─────────────────────────────────
  // Never auto-fix workflow files — GitHub requires a separate `workflows` permission
  // to push changes to .github/workflows/, and granting it would be overpowered.
  const fixableIssues = (review.issues || []).filter(
    i => i.file && i.fix && !i.file.startsWith('.github/workflows/')
  );
  const fixedFiles = [];

  for (const issue of fixableIssues) {
    const filePath = path.join(repoPath, issue.file);
    if (!fs.existsSync(filePath)) continue;

    const original = fs.readFileSync(filePath, 'utf8');

    const fixPrompt = [
      'You are fixing a bug in this file. Return ONLY the complete fixed file contents — no explanation, no markdown, no code fences.',
      '',
      'File: ' + issue.file,
      'Issue: ' + issue.description,
      'Fix to apply: ' + issue.fix,
      '',
      'Current file contents:',
      original,
    ].join('\n');

    try {
      const fixed = await callClaude([{ role: 'user', content: fixPrompt }], 4096);
      // Strip accidental code fences if model added them
      const cleaned = fixed.replace(/^```[\w]*\n?/, '').replace(/\n?```$/, '').trim();
      fs.writeFileSync(filePath, cleaned + '\n');
      fixedFiles.push(issue.file);
      console.log('Fixed: ' + issue.file + ' — ' + issue.description);
    } catch (e) {
      console.log('Could not auto-fix ' + issue.file + ': ' + e.message);
    }
  }

  // ── Step 3: Commit and push fixes ─────────────────────────────────────────
  let commitSha = null;
  if (fixedFiles.length > 0) {
    try {
      execSync('git config user.name "claude-review-bot"', { cwd: repoPath });
      execSync('git config user.email "claude-review-bot@users.noreply.github.com"', { cwd: repoPath });
      execSync('git add ' + fixedFiles.map(f => '"' + f + '"').join(' '), { cwd: repoPath });
      execSync('git commit -m "fix: auto-fix issues from agent review\n\nFixed by Claude agent reviewer."', { cwd: repoPath });
      execSync('git push', { cwd: repoPath });
      commitSha = execSync('git rev-parse HEAD', { cwd: repoPath }).toString().trim();
    } catch (e) {
      console.log('Git push failed: ' + e.message);
    }
  }

  // ── Step 4: Post comment summarising what happened ────────────────────────
  const unresolved = (review.issues || []).filter(i => !fixedFiles.includes(i.file) || !i.fix);

  let body = '### Agent Review\n\n';
  body += '**' + review.verdict + '** — ' + (review.summary || '') + '\n\n';

  if (fixedFiles.length > 0) {
    body += '#### Auto-fixed (' + fixedFiles.length + ' file' + (fixedFiles.length > 1 ? 's' : '') + ')\n';
    for (const issue of fixableIssues.filter(i => fixedFiles.includes(i.file))) {
      body += '- ✅ `' + issue.file + '` — ' + issue.description + '\n';
    }
    if (commitSha) body += '\nCommit: `' + commitSha.slice(0, 7) + '`\n';
    body += '\n';
  }

  if (unresolved.length > 0) {
    body += '#### Needs human attention\n';
    for (const issue of unresolved) {
      body += '- ' + (issue.file ? '`' + issue.file + '`' : '') + ' ' + issue.description + '\n';
    }
  }

  if (fixedFiles.length > 0 && unresolved.length === 0) {
    body += '\n**All issues auto-fixed.** Re-review will run on the new commit.\n';
  }

  body += '\n---\n*Automated review by Claude. Human approval still required.*';

  await github.rest.issues.createComment({
    owner: context.repo.owner,
    repo: context.repo.repo,
    issue_number: context.issue.number,
    body,
  });
};
