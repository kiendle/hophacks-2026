// Rounded, shaded terminal lettering, built entirely from ASCII characters.
const GLYPHS: Record<string, string[]> = {
  S: [' .d8888b.', 'd88P  Y88b', 'Y88b.', ' "Y888b.', '    "Y88b.', '      "888', 'Y88b  d88P', ' "Y8888P"'],
  E: ['8888888888', '888', '888', '8888888', '888', '888', '888', '8888888888'],
  N: ['888b    888', '8888b   888', '88888b  888', '888Y88b 888', '888 Y88b888', '888  Y88888', '888   Y8888', '888    Y888'],
  T: ['88888888888', '    888', '    888', '    888', '    888', '    888', '    888', '    888'],
  I: ['8888888', '  888', '  888', '  888', '  888', '  888', '  888', '8888888'],
  M: ['888b     d888', '8888b   d8888', '88888b.d88888', '888Y88888P888', '888 Y888P 888', '888  Y8P  888', '888       888', '888       888'],
  R: ['8888888b.', '888   Y88b', '888    888', '888   d88P', '8888888P"', '888 T88b', '888  T88b', '888   T88b'],
}
const ASCII_WORDMARK = Array.from({ length: 8 }, (_, row) =>
  [...'SENTIMETER'].map(letter => {
    const glyph = GLYPHS[letter]
    return glyph[row].padEnd(Math.max(...glyph.map(line => line.length)))
  }).join('  '),
).join('\n')

/** A heartbeat cuts through a retro spectrum of sampled opinions. */
export function Logo({ variant = 'default' }: { variant?: 'default' | 'ascii' }) {
  return (
    <span className="sentimeter-logo">
      <img className="sentimeter-logo-mark" src={`${import.meta.env.BASE_URL}sentimeter.svg?v=vivid-pulse-3`} width="40" height="40" alt="" aria-hidden="true" />
      {variant === 'ascii'
        ? <span className="sentimeter-logo-ascii" role="img" aria-label="Sentimeter">{ASCII_WORDMARK}</span>
        : <span className="sentimeter-logo-name">Sentimeter</span>}
    </span>
  )
}
