/** Keep speech requests below the harness's 1,200-character limit. */
export function speechChunks(value: string, limit = 900): string[] {
  const text = value
    .replace(/\[([^\]]+)\]\(https?:\/\/[^)]+\)/g, '$1')
    .replace(/\b(?:https?:\/\/|www\.|at:\/\/)\S+/gi, '')
    .replace(/<\/?[A-Za-z][^<>]{0,200}>/g, ' ')
    .replace(/[*_`#>]+/g, '')
    .replace(/\s+/g, ' ').trim()
  const chunks: string[] = []
  let remaining = text
  while (remaining.length > limit) {
    const head = remaining.slice(0, limit + 1)
    const sentences = [...head.matchAll(/[.!?]\s/g)]
    const boundary = sentences.at(-1)?.index
    const space = head.lastIndexOf(' ')
    const cut = boundary !== undefined && boundary > limit / 2 ? boundary + 1 : space > 0 ? space : limit
    chunks.push(remaining.slice(0, cut).trim())
    remaining = remaining.slice(cut).trim()
  }
  if (remaining) chunks.push(remaining)
  return chunks
}

export async function voiceError(response: Response, fallback: string): Promise<Error> {
  try {
    const body = await response.json()
    return new Error(typeof body.error === 'string' ? body.error : body.error?.message || fallback)
  } catch { return new Error(fallback) }
}
