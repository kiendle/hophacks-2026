import { useCallback, useState } from 'react'
import { ChatSidebar } from '../components/ChatSidebar'
import type { AskContext } from '../ask'

export function AutomationSetup({ question }: { question: string }) {
  const [context] = useState<AskContext>(() => ({ purpose: 'automation_proposal', topic: question,
    series: [], hidden: new Set(), selection: { range: null, subtopics: [] }, mode: 'line',
    view: { start: 0, end: 1 }, now: 1 }))
  const getContext = useCallback(() => context, [context])
  return <main className="automation-setup">
    <header><h1>Build an automation</h1><p>Discuss what to track, refine the rules, and review your configuration.</p></header>
    <ChatSidebar selection={context.selection} series={context.series} onClearSelection={() => {}}
      getContext={getContext} initialQuestion={question} proposalMode />
  </main>
}
