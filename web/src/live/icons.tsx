import type { SVGProps } from 'react'

/**
 * Talk live's own two icons, drawn the way web/src/components/icons.tsx draws every other one: a
 * 24 box, no fill, current colour, 1.8 stroke, round caps and joins. They live here so the shared
 * icon file is not touched.
 */
type IconProps = SVGProps<SVGSVGElement> & { size?: number }

function Icon({ size = 16, children, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      {...rest}
    >
      {children}
    </svg>
  )
}

/** An audio waveform distinguishes live conversation from dictation. */
export const TalkLiveIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4 10v4M8 6v12M12 3v18M16 7v10M20 10v4" />
  </Icon>
)

/** A stop control ends the live conversation. */
export const EndLiveIcon = (p: IconProps) => (
  <Icon {...p}>
    <rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none" />
  </Icon>
)
