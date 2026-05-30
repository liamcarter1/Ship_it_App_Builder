import { NewRunForm } from '@/components/NewRunForm';
import { RunsList } from '@/components/RunsList';

export default function HomePage() {
  return (
    <div className="space-y-8">
      <section className="space-y-3">
        <h1 className="text-xl font-semibold text-zinc-100">Start a new run</h1>
        <NewRunForm />
        <p className="text-xs text-zinc-500">
          The orchestrator runs in the backend process. You&apos;ll be redirected to a live
          view once it accepts the run.
        </p>
      </section>
      <section className="space-y-3">
        <h2 className="text-xl font-semibold text-zinc-100">Recent runs</h2>
        <RunsList />
      </section>
    </div>
  );
}
