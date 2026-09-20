import { useEffect, useState } from 'react'
import { Home } from './app/Home'
import { SetupPanel } from './app/SetupPanel'
import { Sidebar } from './app/Sidebar'
import { readSharedSession, useRecents, type Session, type Stage } from './app/session'
import { Workspace } from './app/Workspace'
import { expandTerms, suggestSubtopics } from './data/terms'
import { AutomationSetup } from './app/AutomationSetup'
import type { TrackerCreated } from './data/automationSource'

/** Subtopics proposed before the user edits them. */
const SUGGESTED = 4

export default function App() {
  const { recents, remember, forget, edit } = useRecents()
  const [shared] = useState(() => readSharedSession(location.hash))
  const [stage, setStage] = useState<Stage>(shared?.dataMode === 'live' ? 'automation' : shared ? 'setup' : 'home')
  const [session, setSession] = useState<Session | null>(shared)
  const [automationQuestion, setAutomationQuestion] = useState(shared?.dataMode === 'live'
    ? `${shared.query}. Track these targets: ${shared.subtopics.join(', ')}. Proposed retrieval terms: ${shared.terms.join(', ')}.` : '')
  // Keep visited workspaces mounted: charts, view controls and analysis belong to
  // the session, not to the currently selected navigation item.
  const [workspaces, setWorkspaces] = useState<Session[]>([])
  const retain = (next: Session) => setWorkspaces(previous =>
    [...previous.filter(item => item.id !== next.id), next].slice(-20))
  const changeSession = (next: Session) => {
    setSession(next)
    remember(next)
    setWorkspaces(previous => previous.map(item => item.id === next.id ? next : item))
  }

  useEffect(() => {
    const created = (event: Event) => {
      const tracker = (event as CustomEvent<TrackerCreated>).detail
      const next: Session = { id: tracker.id, automationId: tracker.id, dataMode: 'live',
        query: tracker.title, title: tracker.title, startedAt: Date.now(),
        terms: tracker.config.keyword_filter.groups.flatMap(group => group.direct),
        subtopics: tracker.config.targets.map(target => target.label) }
      remember(next); retain(next); setSession(next); setStage('live')
    }
    window.addEventListener('sentimeter:tracker-created', created)
    return () => window.removeEventListener('sentimeter:tracker-created', created)
  }, [remember])

  const start = (query: string) => {
    setSession({
      id: `${Date.now()}`,
      query,
      terms: expandTerms(query),
      subtopics: suggestSubtopics(query).slice(0, SUGGESTED),
      startedAt: Date.now(),
      dataMode: 'historical',
    })
    setStage('setup')
  }

  const open = (s: Session) => {
    if (stage === 'live' && session?.id === s.id) return
    retain(s); setSession(s); setStage('live')
  }

  const goHome = () => {
    setSession(null); setStage('home')
  }

  const confirm = () => {
    if (!session) return
    remember(session)
    retain(session)
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
        onDelete={id => {
          const remove = () => {
            forget(id)
            setWorkspaces(previous => previous.filter(item => item.id !== id))
          }
          remove()
          if (session?.id === id) goHome()
        }}
        onEdit={(id, changes) => {
          edit(id, changes)
          setWorkspaces(previous => previous.map(item => item.id === id ? { ...item, ...changes } : item))
          if (session?.id === id) {
            if (changes.archived) goHome()
            else setSession({ ...session, ...changes })
          }
        }}
      />
      {stage === 'home' && <Home onSubmit={start} onCreateAutomation={question => {
        setSession(null)
        setAutomationQuestion(question)
        setStage('automation')
      }} />}
      {stage === 'automation' && <AutomationSetup question={automationQuestion} />}
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
      {workspaces.map(saved => {
        const active = stage === 'live' && session?.id === saved.id
        return <section className="retained-workspace" key={saved.id} hidden={!active} aria-label={saved.title || saved.query}>
          <Workspace session={saved} active={active} onSessionChange={changeSession} onClose={goHome} />
        </section>
      })}
    </div>
  )
}
