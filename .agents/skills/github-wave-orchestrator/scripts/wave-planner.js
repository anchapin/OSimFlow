#!/usr/bin/env node
/**
 * Wave Planner for GitHub Wave Orchestrator
 *
 * Reads open issues from stdin (JSON), groups them into waves of max 3,
 * where each wave contains independent issues (no shared files).
 *
 * Output: wave plan as JSON to stdout
 */

const readline = require('readline');

const MAX_ISSUES_PER_WAVE = 3;

async function main() {
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    crlfDelay: Infinity,
  });

  let input = '';
  for await (const line of rl) {
    input += line;
  }

  let issues;
  try {
    issues = JSON.parse(input);
  } catch (e) {
    console.error('Failed to parse JSON input:', e.message);
    process.exit(1);
  }

  if (!Array.isArray(issues)) {
    console.error('Expected array of issues');
    process.exit(1);
  }

  // Filter out blocked, on-hold, or already PR-linked issues
  const BLOCKED_LABELS = ['blocked', 'on-hold', 'wontfix', 'duplicate'];
  const filtered = issues.filter((issue) => {
    // Skip if has blocked label
    if (issue.labels) {
      const labelNames = issue.labels.map((l) => (typeof l === 'string' ? l : l.name));
      if (labelNames.some((name) => BLOCKED_LABELS.includes(name.toLowerCase()))) {
        return false;
      }
    }
    // Skip if assigned to someone
    if (issue.assignees && issue.assignees.length > 0) {
      return false;
    }
    return true;
  });

  // Analyze file dependencies for each issue
  const issueFiles = new Map();
  for (const issue of filtered) {
    const files = extractFileDependencies(issue);
    issueFiles.set(issue.number, files);
  }

  // Pack issues into waves using first-fit algorithm
  const waves = packIntoWaves(filtered, issueFiles, MAX_ISSUES_PER_WAVE);

  const plan = {
    total_issues: filtered.length,
    total_waves: waves.length,
    waves: waves,
  };

  console.log(JSON.stringify(plan, null, 2));
}

/**
 * Extract file/directory dependencies from issue body and labels
 */
function extractFileDependencies(issue) {
  const files = new Set();
  const body = issue.body || '';
  const title = issue.title || '';

  // Combine body and title for searching
  const text = `${title}\n${body}`;

  // Pattern to match file paths like osimflow/foo.py, bin/*.py, tests/
  const filePatterns = [
    // osimflow/... paths
    /osimflow\/[a-zA-Z0-9_\-\/.]+/g,
    // bin/... paths
    /bin\/[a-zA-Z0-9_\-\/*.]+/g,
    // tests/... paths
    /tests\/[a-zA-Z0-9_\-\/*.]+/g,
    // infra/... paths
    /infra\/[a-zA-Z0-9_\-\/*.]+/g,
    // docs/... paths
    /docs\/[a-zA-Z0-9_\-\/*.]+/g,
    // user_scripts/... paths
    /user_scripts\/[a-zA-Z0-9_\-\/*.]+/g,
    // scripts/... paths
    /scripts\/[a-zA-Z0-9_\-\/*.]+/g,
    // Any path ending in .py, .yml, .yaml, .json, .md, .tf
    /[a-zA-Z0-9_\-\/]+\.(py|yml|yaml|json|md|tf|sh|rb|rs|go|ts|js)/g,
  ];

  for (const pattern of filePatterns) {
    const matches = text.match(pattern);
    if (matches) {
      for (const match of matches) {
        // Normalize: remove trailing slashes for directories
        const normalized = match.replace(/\/+$/, '');
        files.add(normalized);
      }
    }
  }

  // Labels are hints but not file-level dependencies
  // Only add specific files based on labels, not directories
  if (issue.labels) {
    const labelNames = issue.labels.map((l) => (typeof l === 'string' ? l : l.name));
    for (const label of labelNames) {
      const labelLower = label.toLowerCase();
      // Map specific labels to specific files (not directories)
      if (labelLower.includes('ops-')) {
        // OPS issues often relate to cache/monitoring
        files.add('osimflow/cache.py');
        files.add('osimflow/distributed_cache.py');
      } else if (labelLower.includes('sensitivity')) {
        files.add('osimflow/algorithms/morris.py');
        files.add('osimflow/algorithms/fast99.py');
      }
    }
  }

  return files;
}

/**
 * Check if two issues share any file dependencies
 * Only exact file matches are considered conflicts - directory overlap is not
 */
function issuesAreIndependent(issue1Files, issue2Files) {
  // If either has no detected files, they are independent (conservative)
  if (issue1Files.size === 0 || issue2Files.size === 0) {
    return true;
  }

  // Check for exact file matches only (not directory containment)
  for (const file1 of issue1Files) {
    // Skip directory entries for dependency check
    if (file1.endsWith('/')) continue;

    for (const file2 of issue2Files) {
      // Skip directory entries for dependency check
      if (file2.endsWith('/')) continue;

      // Exact match on specific file
      if (file1 === file2) {
        return false;
      }
    }
  }

  return true;
}

/**
 * First-fit decreasing algorithm to pack issues into waves
 * Issues are sorted by number ascending, then packed greedily
 */
function packIntoWaves(issues, issueFiles, maxPerWave) {
  // Sort issues by number ascending
  const sorted = [...issues].sort((a, b) => a.number - b.number);

  const waves = [];

  for (const issue of sorted) {
    const issueFileSet = issueFiles.get(issue.number) || new Set();
    let assigned = false;

    // Try to add to existing wave
    for (const wave of waves) {
      if (wave.issues.length >= maxPerWave) {
        continue;
      }

      // Check if issue is independent of all issues in this wave
      let independent = true;
      for (const existingIssueNum of wave.issues) {
        const existingFiles = issueFiles.get(existingIssueNum) || new Set();
        if (!issuesAreIndependent(issueFileSet, existingFiles)) {
          independent = false;
          break;
        }
      }

      if (independent) {
        wave.issues.push(issue.number);
        wave.titles.push(issue.title);
        assigned = true;
        break;
      }
    }

    // If not assigned, create a new wave
    if (!assigned) {
      waves.push({
        wave: waves.length + 1,
        issues: [issue.number],
        titles: [issue.title],
      });
    }
  }

  return waves;
}

main().catch((e) => {
  console.error('Wave planner error:', e);
  process.exit(1);
});
