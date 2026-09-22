// Shared assertion helper: every check prints PASS/FAIL lines as it goes and
// carries a final verdict through its exit code (the runner aggregates them).
let failures = 0

export function check(label, cond, detail = '') {
  const ok = Boolean(cond)
  console.log(`  ${ok ? 'PASS' : 'FAIL'}: ${label}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failures += 1
  return ok
}

export const failureCount = () => failures