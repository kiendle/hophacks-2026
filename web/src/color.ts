import { rgb } from 'd3'

function luminance(color: string): number {
  const { r, g, b } = rgb(color)
  const lin = (c: number) => {
    const v = c / 255
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
}

/** White on dark fills, near-black on light ones. */
export const textOn = (fill: string) => (luminance(fill) > 0.4 ? '#1a1a1a' : '#fff')
