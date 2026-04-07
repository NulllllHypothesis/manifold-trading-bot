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

// Validate Python syntax. Returns null on success, error string on failure.
function validatePython(filePath) {
  try {
    execSync('python3 -m py_compile "' + filePath + '"', { stdio: 'pipe' });
    return null;
  } catch (e) {
    return (e.stderr ? e.stderr.toString() : e.message).slice(0, 200);
  }
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
    await github.rest.issues.createComment({
      owner: context.repo.owner,
      repo: context.repo.repo,
      issue_number: context.issue.number,
      body: '### Agent Review\n\n' + reviewRaw + '\n\n---\n*Automated review by Claude. Human approval still required.*',
    });
    return;
  }

  // ── Step 2: Auto-fix using targeted find/replace patches ──────────────────
  //
  // IMPORTANT: We ask Claude for a {"find": "...", "replace": "..."} patch
  // rather than the whole file. Returning complete file contents blows the
  // output token budget on any file > ~200 lines and causes truncation, which
  // produces syntax errors that break the bot.
  //
  // Safety checks before any patch is committed:
  //   1. "find" must appear exactly once (uniqueness)
  //   2. python3 -m py_compile must pass on the patched file
  //   3. If either check fails, the original file is restored and the issue
  //      is reported as "skipped" in the PR comment — nothing is committed.
  //
  const fixableIssues = (review.issues || []).filter(
    i => i.file && i.fix && !i.file.startsWith('.github/workflows/')
  );
  const fixedFiles = [];
  const skippedFiles = [];

  for (const issue of fixableIssues) {
    const filePath = path.join(repoPath, issue.file);
    if (!fs.existsSync(filePath)) continue;

    const isPython = issue.file.endsWith('.py');
    const original = fs.readFileSync(filePath, 'utf8');

    const fixPrompt = [
      'You are fixing a specific bug in a file. Return ONLY a JSON object — no explanation, no markdown, no code fences.',
      '',
      'File: ' + issue.file,
      'Issue: ' + issue.description,
      'Fix needed: ' + issue.fix,
      '',
      'Return exactly this JSON format:',
      '{"find": "exact_string_to_find", "replace": "exact_replacement_string"}',
      '',
      'Rules:',
      '- "find" must match a unique section of the file (include 2-3 surrounding lines for context)',
      '- "replace" is the corrected version of that exact section only',
      '- Do NOT return the entire file — only the changed section',
      '- Preserve indentation exactly as it appears in the file',
      '- Keep the patch as small as possible',
      '',
      'File contents:',
      original,
    ].join('\n');

    try {
      const patchRaw = await callClaude([{ role: 'user', content: fixPrompt }], 2048);
      const patchMatch = patchRaw.match(/\{[\s\S]*\}/);
      if (!patchMatch) throw new Error('response contained no JSON object');

      const patch = JSON.parse(patchMatch[0]);
      if (!patch.find || patch.replace === undefined) throw new Error('JSON missing find or replace key');

      // Uniqueness check — a non-unique find string would silently patch the wrong place
      const occurrences = original.split(patch.find).length - 1;
      if (occurrences === 0) throw new Error('"find" string not found in file');
      if (occurrences > 1) throw new Error('"find" string matches ' + occurrences + ' locations — too ambiguous to apply safely');

      const patched = original.replace(patch.find, patch.replace);
      fs.writeFileSync(filePath, patched);

      // Syntax check — revert if the patch broke Python syntax
      if (isPython) {
        const syntaxError = validatePython(filePath);
        if (syntaxError) {
          fs.writeFileSync(filePath, original);  // revert
          skippedFiles.push({ file: issue.file, reason: 'patch produced syntax error — reverted. ' + syntaxError });
          console.log('Reverted ' + issue.file + ': patch caused syntax error');
          continue;
        }
      }

      fixedFiles.push(issue.file);
      console.log('Fixed: ' + issue.file + ' — ' + issue.description);
    } catch (e) {
      // Ensure original is restored if anything went wrong mid-write
      try { fs.writeFileSync(filePath, original); } catch (_) {}
      skippedFiles.push({ file: issue.file, reason: e.message });
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

  if (skippedFiles.length > 0) {
    body += '#### Skipped (unsafe to auto-fix — needs human)\n';
    for (const s of skippedFiles) {
      body += '- ⚠️ `' + s.file + '` — ' + s.reason + '\n';
    }
    body += '\n';
  }

  if (unresolved.length > 0) {
    body += '#### Needs human attention\n';
    for (const issue of unresolved) {
      body += '- ' + (issue.file ? '`' + issue.file + '`' : '') + ' — ' + issue.description + '\n';
    }
  }

  if (fixedFiles.length > 0 && unresolved.length === 0 && skippedFiles.length === 0) {
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
