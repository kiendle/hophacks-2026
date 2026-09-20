/** The smallest test runner that says what it checked: one line per check, ASCII only. */
const outcomes: boolean[] = []

export function check(name: string, ok: boolean, detail = '') {
  outcomes.push(Boolean(ok))
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  --  ${detail}` : ''}`)
}

export function same(name: string, got: unknown, wanted: unknown) {
  const one = JSON.stringify(got)
  const two = JSON.stringify(wanted)
  check(name, one === two, one === two ? '' : `got ${one}, wanted ${two}`)
}

export function report(title: string): number {
  const passed = outcomes.filter(Boolean).length
  console.log(`\n${passed}/${outcomes.length} ${title} checks passed`)
  return passed === outcomes.length && outcomes.length > 0 ? 0 : 1
}

/** Lets every promise that is already resolvable run. */
export const settle = () => new Promise<void>((resolve) => setImmediate(resolve))
