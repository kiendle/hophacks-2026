/** Share actionable failure details with both written chat and the voice agent. */
export function failureMessage(error: unknown, lastStep = ''): string {
  const detail = error instanceof Error ? error.message : 'The workspace could not be reached.'
  if (/network|failed to fetch|load failed|connection ended|fetch failed/i.test(detail)) {
    const during = lastStep ? ` while ${lastStep.charAt(0).toLowerCase()}${lastStep.slice(1)}` : ''
    return `The connection to the workspace was interrupted${during}. The answer was not received. Please try again.`
  }
  return detail
}

export function voiceFailure(error: unknown): string {
  return JSON.stringify({ completed: false, error: failureMessage(error),
    instruction: 'Explain this specific error. Do not invent a cause or claim the request completed.' })
}
