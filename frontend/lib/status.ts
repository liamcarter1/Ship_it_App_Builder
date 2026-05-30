// Single source of truth for run-status -> Tailwind classes.
//
// Kept in sync with the orchestrator's status values
// (backend/app/orchestrator.py):
//   - in-flight:    running
//   - succeeded:    built, deployed
//   - reviewer red: failed_review
//   - failed pre-build:  failed_planner, failed_scaffold
//   - deploy red:        deploy_failed
//   - human gates (M3): rejected_at_{spec,code,deploy}   (user said no)
//                       expired_at_{spec,code,deploy}    (no decision in time)
//   - unknown:           errored
//
// Add a new orchestrator status here AND in backend/app/orchestrator.py.

export const STATUS_COLOUR: Record<string, string> = {
  running: 'text-cyan-300 animate-pulse',
  built: 'text-emerald-300',
  deployed: 'text-emerald-400',
  failed_review: 'text-rose-300',
  failed_planner: 'text-rose-300',
  failed_scaffold: 'text-rose-300',
  deploy_failed: 'text-amber-300',
  rejected_at_spec: 'text-rose-300',
  rejected_at_code: 'text-rose-300',
  rejected_at_deploy: 'text-rose-300',
  expired_at_spec: 'text-amber-300',
  expired_at_code: 'text-amber-300',
  expired_at_deploy: 'text-amber-300',
  errored: 'text-rose-400',
};

export function statusColour(status: string): string {
  return STATUS_COLOUR[status] ?? 'text-zinc-300';
}
