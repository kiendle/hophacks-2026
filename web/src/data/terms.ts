/**
 * Stand-in for the LLM that expands a query into search terms and suggests
 * subtopics. Replace with the backend call; the UI only needs the strings.
 */

const SYNONYMS: Record<string, string[]> = {
  ai: ['artificial intelligence', 'LLM', 'large language model', 'machine learning', 'genAI', 'chatbot', 'foundation model', 'AI model'],
  crypto: ['cryptocurrency', 'bitcoin', 'ethereum', 'blockchain', 'defi', 'stablecoin', 'onchain'],
  election: ['vote', 'ballot', 'polling', 'campaign', 'primary', 'turnout'],
  climate: ['global warming', 'emissions', 'carbon', 'net zero', 'heatwave', 'renewables'],
  apple: ['iphone', 'macbook', 'ios', 'app store', 'tim cook', 'vision pro'],
}

const SUBTOPICS: Record<string, string[]> = {
  ai: ['Anthropic', 'OpenAI', 'Google DeepMind', 'Nvidia', 'Meta AI', 'Microsoft', 'xAI', 'Mistral', 'Perplexity'],
  crypto: ['Bitcoin', 'Ethereum', 'Solana', 'Coinbase', 'Tether'],
  apple: ['iPhone', 'Mac', 'Vision Pro', 'App Store'],
}

const key = (query: string) => {
  const q = query.toLowerCase()
  return Object.keys(SYNONYMS).find((k) => q.includes(k))
}

/** Search terms a post must match to be kept. */
export function expandTerms(query: string): string[] {
  const q = query.trim()
  const base = key(q)
  const terms = [q, ...(base ? SYNONYMS[base] : [])]
  if (!base) terms.push(`${q} news`, `${q} update`, `#${q.replace(/\s+/g, '')}`)
  return [...new Set(terms.filter(Boolean))]
}

/** Subtopics worth tracking under the query. */
export function suggestSubtopics(query: string): string[] {
  const base = key(query)
  return base ? (SUBTOPICS[base] ?? []) : []
}
