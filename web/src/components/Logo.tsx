const ASCII_WORDMARK = "   _____ _______   ______________  _________________________\n  / ___// ____/ | / /_  __/  _/  |/  / ____/_  __/ ____/ __ \\\n  \\__ \\/ __/ /  |/ / / /  / // /|_/ / __/   / / / __/ / /_/ /\n ___/ / /___/ /|  / / / _/ // /  / / /___  / / / /___/ _, _/\n/____/_____/_/ |_/ /_/ /___/_/  /_/_____/ /_/ /_____/_/ |_|"

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
