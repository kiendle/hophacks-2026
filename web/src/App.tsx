import { useState } from 'react'
import { Sidebar } from './app/Sidebar'
import { Workspace } from './app/Workspace'
import type { Session } from './app/session'

const createSession = (): Session => ({
  id: crypto.randomUUID(),
  query: 'AI',
  terms: [],
  subtopics: ['OpenAI', 'Anthropic', 'Google / Google DeepMind', 'xAI', 'NVIDIA'],
  startedAt: Date.now(),
})

export default function App() {
  const [session, setSession] = useState(createSession)
  const restart = () => setSession(createSession())
  return (
    <div className="app">
      <Sidebar recents={[]} activeId={session.id} onNew={restart} onHome={restart}
        onOpen={setSession} onDelete={() => {}} />
      <Workspace key={session.id} session={session} onSessionChange={setSession} />
    </div>
  )
}
