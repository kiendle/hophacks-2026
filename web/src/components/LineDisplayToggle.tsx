export type LineDisplay = 'both' | 'points' | 'trend'

export function LineDisplayToggle({ value, onChange }: {
  value: LineDisplay
  onChange: (value: LineDisplay) => void
}) {
  return (
    <div className="line-display" role="group" aria-label="Visible lines">
      {([['both', 'Both'], ['points', 'Points'], ['trend', '24h trend']] as const).map(([mode, label]) => (
        <button key={mode} type="button" aria-pressed={value === mode} onClick={() => onChange(mode)}>
          {label}
        </button>
      ))}
    </div>
  )
}
