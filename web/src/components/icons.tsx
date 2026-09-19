import type { SVGProps } from 'react'

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

export const PlayIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M7 4.5v15l12.5-7.5z" fill="currentColor" stroke="none" />
  </Icon>
)

export const PauseIcon = (p: IconProps) => (
  <Icon {...p}>
    <rect x="6" y="4.5" width="4" height="15" rx="1" fill="currentColor" stroke="none" />
    <rect x="14" y="4.5" width="4" height="15" rx="1" fill="currentColor" stroke="none" />
  </Icon>
)

export const SendIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 19V5M5.5 11.5 12 5l6.5 6.5" />
  </Icon>
)

export const CloseIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M6 6l12 12M18 6 6 18" />
  </Icon>
)

export const ReplyIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4 5.5h16v10.5H9.5L5 20v-4H4z" />
  </Icon>
)

export const RetweetIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M7 20V8h11M14.5 4.5 18 8l-3.5 3.5M17 4v12H6M9.5 19.5 6 16l3.5-3.5" />
  </Icon>
)

export const HeartIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 20s-7.5-4.6-7.5-10.2A4.3 4.3 0 0 1 12 7.2a4.3 4.3 0 0 1 7.5 2.6C19.5 15.4 12 20 12 20z" />
  </Icon>
)

export const LineChartIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M3.5 17.5 9 11l4 4 7.5-8.5" />
  </Icon>
)

export const BubblesIcon = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="8.5" cy="9" r="4.5" />
    <circle cx="17" cy="7" r="2.5" />
    <circle cx="15.5" cy="16" r="3.5" />
  </Icon>
)

export const PlusIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 5.5v13M5.5 12h13" />
  </Icon>
)

export const TractionIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4 17.5 10 11.5l3.5 3.5L20 8.5M14.5 8.5H20V14" />
  </Icon>
)

export const PostsIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M6 5.5h12M6 10h12M6 14.5h12M6 19h7" />
  </Icon>
)

export const ChevronLeftIcon = (p: IconProps) => (
  <Icon {...p}>
    <path d="M14.5 6 8.5 12l6 6" />
  </Icon>
)

export const InfoIcon = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5.5M12 7.6v.1" />
  </Icon>
)

export const StopIcon = (p: IconProps) => (
  <Icon {...p}>
    <rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor" stroke="none" />
  </Icon>
)
