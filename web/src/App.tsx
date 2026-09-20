import { useState } from 'react'
import { Home } from './app/Home'
import { SetupPanel } from './app/SetupPanel'
import { Sidebar } from './app/Sidebar'
import { useRecents, type Session, type Stage } from './app/session'
import { Workspace } from './app/Workspace'
import { expandTerms, suggestSubtopics } from './data/terms'

/** Subtopics proposed before the user edits them. */
const SUGGESTED = 4

export default function App() {
  const { recents, remember } = useRecents()
  const [stage, setStage] = useState<Stage>('home')
  const [session, setSession] = useState<Session | null>(null)

  const start = (query: string) => {
    setSession({
      id: `${Date.now()}`,
      query,
      terms: expandTerms(query),
      subtopics: suggestSubtopics(query).slice(0, SUGGESTED),
      startedAt: Date.now(),
    })
    setStage('setup')
  }

  const open = (s: Session) => {
    setSession(s)
    setStage('setup')
  }

  const goHome = () => {
    setSession(null)
    setStage('home')
  }

  const confirm = () => {
    if (!session) return
    remember(session)
    setStage('live')
  }

  return (
    <div className={stage === 'setup' ? 'app app-setup' : 'app'}>
      <Sidebar
        recents={recents}
        activeId={session?.id ?? null}
        onNew={goHome}
        onHome={goHome}
        onOpen={open}
      />
      {stage === 'home' && <Home onSubmit={start} />}
      {stage === 'setup' && session && (
        <>
          <Workspace session={session} onSessionChange={setSession} preview />
          <div className="setup-overlay">
            <SetupPanel
              query={session.query}
              terms={session.terms}
              subtopics={session.subtopics}
              onTermsChange={(terms) => setSession({ ...session, terms })}
              onSubtopicsChange={(subtopics) => setSession({ ...session, subtopics })}
              onConfirm={confirm}
            />
          </div>
        </>
      )}
      {stage === 'live' && session && <Workspace session={session} onSessionChange={setSession} />}
    </div>
  )
}
