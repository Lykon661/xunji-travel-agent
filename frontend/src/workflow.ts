import type { Trip } from './types'

export type WorkflowTab = 'requirements' | 'guides' | 'interview' | 'routes' | 'itinerary' | 'resources'

export function workflowStamp(trip: Trip): string {
  return [trip.id,trip.active_run,trip.run?.status,trip.document.workflow_step,trip.document.pending?.kind,trip.document.pending?.round_id].join(':')
}

export function resolveWorkflowTab(trip: Trip): WorkflowTab {
  const doc = trip.document
  const pending: Record<string,WorkflowTab> = {requirements:'requirements',guides:'guides',sources:'guides',interview:'interview',interview_confirmation:'interview',decision:'interview',selection:'routes'}
  if (doc.pending && pending[doc.pending.kind]) return pending[doc.pending.kind]
  if (doc.workflow_step && ['requirements','guides','interview','routes','itinerary','resources'].includes(doc.workflow_step)) return doc.workflow_step as WorkflowTab
  if (doc.itinerary && !doc.itinerary_outdated) return 'itinerary'
  if (doc.candidates.length) return 'routes'
  if (doc.interview || ['discovering','parsing','interviewing'].includes(doc.progress_stage || doc.stage)) return 'interview'
  return trip.sources.length ? 'guides' : 'requirements'
}
