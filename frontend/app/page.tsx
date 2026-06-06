import { NewRunForm } from '@/components/NewRunForm';
import { RunsList } from '@/components/RunsList';

export default function HomePage() {
  return (
    <div className="space-y-10">
      <section className="space-y-4">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-50">
            Start a new run
          </h1>
          <p className="text-sm text-zinc-500">
            One line in, a live Next.js app out. The orchestrator runs in your local
            backend — you&apos;ll be redirected to the live view as soon as it accepts.
          </p>
        </div>
        <NewRunForm />
      </section>

      <section className="space-y-4">
        <div className="flex items-baseline justify-between">
          <h2 className="text-2xl font-semibold tracking-tight text-zinc-50">
            Recent runs
          </h2>
          <span className="text-[11px] uppercase tracking-wider text-zinc-500">
            auto-refreshing
          </span>
        </div>
        <RunsList />
      </section>
    </div>
  );
}
